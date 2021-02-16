#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from __future__ import annotations

from functools import partial
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    MutableMapping,
    Optional,
    Tuple,
    Union,
)

import numpy as np
import torch
from ax.core.batch_trial import BatchTrial
from ax.core.experiment import Experiment
from ax.core.objective import MultiObjective, Objective, ScalarizedObjective
from ax.core.observation import ObservationData, ObservationFeatures
from ax.core.optimization_config import MultiObjectiveOptimizationConfig, TRefPoint
from ax.core.outcome_constraint import ComparisonOp, OutcomeConstraint
from ax.core.parameter import ParameterType, RangeParameter
from ax.core.parameter_constraint import ParameterConstraint
from ax.core.search_space import SearchSpace
from ax.core.trial import Trial
from ax.core.types import TBounds, TCandidateMetadata
from ax.modelbridge.transforms.base import Transform
from ax.models.torch.frontier_utils import (
    get_weighted_mc_objective_and_objective_thresholds,
    get_default_frontier_evaluator,
)
from ax.utils.common.logger import get_logger
from ax.utils.common.typeutils import (
    checked_cast,
    checked_cast_optional,
    not_none,
    checked_cast_to_tuple,
)
from botorch.utils.multi_objective.hypervolume import Hypervolume
from torch import Tensor


logger = get_logger(__name__)


if TYPE_CHECKING:
    # import as module to make sphinx-autodoc-typehints happy
    from ax import modelbridge as modelbridge_module  # noqa F401  # pragma: no cover


def extract_parameter_constraints(
    parameter_constraints: List[ParameterConstraint], param_names: List[str]
) -> Optional[TBounds]:
    """Extract parameter constraints."""
    if len(parameter_constraints) > 0:
        A = np.zeros((len(parameter_constraints), len(param_names)))
        b = np.zeros((len(parameter_constraints), 1))
        for i, c in enumerate(parameter_constraints):
            b[i, 0] = c.bound
            for name, val in c.constraint_dict.items():
                A[i, param_names.index(name)] = val
        linear_constraints: TBounds = (A, b)
    else:
        linear_constraints = None
    return linear_constraints


def get_bounds_and_task(
    search_space: SearchSpace, param_names: List[str]
) -> Tuple[List[Tuple[float, float]], List[int], Dict[int, float]]:
    """Extract box bounds from a search space in the usual Scipy format.
    Identify integer parameters as task features.
    """
    bounds: List[Tuple[float, float]] = []
    task_features: List[int] = []
    target_fidelities: Dict[int, float] = {}
    for i, p_name in enumerate(param_names):
        p = search_space.parameters[p_name]
        # Validation
        if not isinstance(p, RangeParameter):
            raise ValueError(f"{p} not RangeParameter")
        elif p.log_scale:
            raise ValueError(f"{p} is log scale")
        # Set value
        bounds.append((p.lower, p.upper))
        if p.parameter_type == ParameterType.INT:
            task_features.append(i)
        if p.is_fidelity:
            if not isinstance(not_none(p.target_value), (int, float)):
                raise NotImplementedError(
                    "Only numerical target values currently supported."
                )
            target_fidelities[i] = float(
                checked_cast_to_tuple((int, float), p.target_value)
            )

    return bounds, task_features, target_fidelities


def extract_objective_thresholds(
    objective_thresholds: TRefPoint, outcomes: List[str]
) -> np.ndarray:
    """Extracts objective thresholds' values, in the order of `outcomes`.

    The extracted array will be no greater than the number of values in the
    objective_thresholds, typically the same as number of objectives being
    optimized.

    Args:
        objective_thresholds: Reference Point to extract values from.
        outcomes: n-length list of names of metrics.

    Returns:
        len(objective_thresholds)-length list of reference point coordinates
    """
    objective_threshold_dict = {ot.metric.name: ot.bound for ot in objective_thresholds}
    return np.array(
        [
            objective_threshold_dict[name]
            for name in outcomes
            if name in objective_threshold_dict
        ]
    )


def extract_objective_weights(objective: Objective, outcomes: List[str]) -> np.ndarray:
    """Extract a weights for objectives.

    Weights are for a maximization problem.

    Give an objective weight to each modeled outcome. Outcomes that are modeled
    but not part of the objective get weight 0.

    In the single metric case, the objective is given either +/- 1, depending
    on the minimize flag.

    In the multiple metric case, each objective is given the input weight,
    multiplied by the minimize flag.

    Args:
        objective: Objective to extract weights from.
        outcomes: n-length list of names of metrics.

    Returns:
        n-length list of weights.

    """
    s = -1.0 if objective.minimize else 1.0
    objective_weights = np.zeros(len(outcomes))
    if isinstance(objective, ScalarizedObjective):
        for obj_metric, obj_weight in objective.metric_weights:
            objective_weights[outcomes.index(obj_metric.name)] = obj_weight * s
    elif isinstance(objective, MultiObjective):
        for obj_metric, obj_weight in objective.metric_weights:
            # Rely on previously extracted lower_is_better weights not objective.
            objective_weights[outcomes.index(obj_metric.name)] = obj_weight or s
    else:
        objective_weights[outcomes.index(objective.metric.name)] = s
    return objective_weights


def extract_outcome_constraints(
    outcome_constraints: List[OutcomeConstraint], outcomes: List[str]
) -> TBounds:
    # Extract outcome constraints
    if len(outcome_constraints) > 0:
        A = np.zeros((len(outcome_constraints), len(outcomes)))
        b = np.zeros((len(outcome_constraints), 1))
        for i, c in enumerate(outcome_constraints):
            s = 1 if c.op == ComparisonOp.LEQ else -1
            j = outcomes.index(c.metric.name)
            A[i, j] = s
            b[i, 0] = s * c.bound
        outcome_constraint_bounds: TBounds = (A, b)
    else:
        outcome_constraint_bounds = None
    return outcome_constraint_bounds


def validate_and_apply_final_transform(
    objective_weights: np.ndarray,
    outcome_constraints: Optional[Tuple[np.ndarray, np.ndarray]],
    linear_constraints: Optional[Tuple[np.ndarray, np.ndarray]],
    pending_observations: Optional[List[np.ndarray]],
    final_transform: Callable[[np.ndarray], Tensor] = torch.tensor,
) -> Tuple[
    Tensor,
    Optional[Tuple[Tensor, Tensor]],
    Optional[Tuple[Tensor, Tensor]],
    Optional[List[Tensor]],
]:
    # pyre-fixme[35]: Target cannot be annotated.
    objective_weights: Tensor = final_transform(objective_weights)
    if outcome_constraints is not None:  # pragma: no cover
        # pyre-fixme[35]: Target cannot be annotated.
        outcome_constraints: Tuple[Tensor, Tensor] = (
            final_transform(outcome_constraints[0]),
            final_transform(outcome_constraints[1]),
        )
    if linear_constraints is not None:  # pragma: no cover
        # pyre-fixme[35]: Target cannot be annotated.
        linear_constraints: Tuple[Tensor, Tensor] = (
            final_transform(linear_constraints[0]),
            final_transform(linear_constraints[1]),
        )
    if pending_observations is not None:  # pragma: no cover
        # pyre-fixme[35]: Target cannot be annotated.
        pending_observations: List[Tensor] = [
            final_transform(pending_obs) for pending_obs in pending_observations
        ]
    return (
        objective_weights,
        outcome_constraints,
        linear_constraints,
        pending_observations,
    )


def get_fixed_features(
    fixed_features: ObservationFeatures, param_names: List[str]
) -> Optional[Dict[int, float]]:
    """Reformat a set of fixed_features."""
    fixed_features_dict = {}
    for p_name, val in fixed_features.parameters.items():
        # These all need to be floats at this point.
        # pyre-ignore[6]: All float here.
        val_ = float(val)
        fixed_features_dict[param_names.index(p_name)] = val_
    fixed_features_dict = fixed_features_dict if len(fixed_features_dict) > 0 else None
    return fixed_features_dict


def pending_observations_as_array(
    pending_observations: Dict[str, List[ObservationFeatures]],
    outcome_names: List[str],
    param_names: List[str],
) -> Optional[List[np.ndarray]]:
    """Re-format pending observations.

    Args:
        pending_observations: List of raw numpy pending observations.
        outcome_names: List of outcome names.
        param_names: List fitted param names.

    Returns:
        Filtered pending observations data, by outcome and param names.
    """
    if len(pending_observations) == 0:
        pending_array: Optional[List[np.ndarray]] = None
    else:
        pending_array = [np.array([]) for _ in outcome_names]
        for metric_name, po_list in pending_observations.items():
            # It is possible that some metrics attached to the experiment should
            # not be included in pending features for a given model. For example,
            # if a model is fit to the initial data that is missing some of the
            # metrics on the experiment or if a model just should not be fit for
            # some of the metrics attached to the experiment, so metrics that
            # appear in pending_observations (drawn from an experiment) but not
            # in outcome_names (metrics, expected for the model) are filtered out.ß
            if metric_name not in outcome_names:
                continue
            pending_array[outcome_names.index(metric_name)] = np.array(
                [[po.parameters[p] for p in param_names] for po in po_list]
            )
    return pending_array


def parse_observation_features(
    X: np.ndarray,
    param_names: List[str],
    candidate_metadata: Optional[List[TCandidateMetadata]] = None,
) -> List[ObservationFeatures]:
    """Re-format raw model-generated candidates into ObservationFeatures.

    Args:
        param_names: List of param names.
        X: Raw np.ndarray of candidate values.
        candidate_metadata: Model's metadata for candidates it produced.

    Returns:
        List of candidates, represented as ObservationFeatures.
    """
    if candidate_metadata and len(candidate_metadata) != len(X):
        raise ValueError(  # pragma: no cover
            "Observations metadata list provided is not of "
            "the same size as the number of candidates."
        )
    observation_features = []
    for i, x in enumerate(X):
        observation_features.append(
            ObservationFeatures(
                parameters=dict(zip(param_names, x)),
                metadata=candidate_metadata[i] if candidate_metadata else None,
            )
        )
    return observation_features


def transform_callback(
    param_names: List[str], transforms: MutableMapping[str, Transform]
) -> Callable[[np.ndarray], np.ndarray]:
    """A closure for performing the `round trip` transformations.

    The function round points by de-transforming points back into
    the original space (done by applying transforms in reverse), and then
    re-transforming them.
    This function is specifically for points which are formatted as numpy
    arrays. This function is passed to _model_gen.

    Args:
        param_names: Names of parameters to transform.
        transforms: Ordered set of transforms which were applied to the points.

    Returns:
        a function with for performing the roundtrip transform.
    """

    def _roundtrip_transform(x: np.ndarray) -> np.ndarray:
        """Inner function for performing aforementioned functionality.

        Args:
            x: points in the transformed space (e.g. all transforms have been applied
                to them)

        Returns:
            points in the transformed space, but rounded via the original space.
        """
        # apply reverse terminal transform to turn array to ObservationFeatures
        observation_features = [
            ObservationFeatures(
                parameters={p: float(x[i]) for i, p in enumerate(param_names)}
            )
        ]
        # reverse loop through the transforms and do untransform
        for t in reversed(transforms.values()):
            observation_features = t.untransform_observation_features(
                observation_features
            )
        # forward loop through the transforms and do transform
        for t in transforms.values():
            observation_features = t.transform_observation_features(
                observation_features
            )
        # parameters are guaranteed to be float compatible here, but pyre doesn't know
        new_x: List[float] = [
            # pyre-fixme[6]: Expected `Union[_SupportsIndex, bytearray, bytes, str,
            #  typing.SupportsFloat]` for 1st param but got `Union[None, bool, float,
            #  int, str]`.
            float(observation_features[0].parameters[p])
            for p in param_names
        ]
        # turn it back into an array
        return np.array(new_x)

    return _roundtrip_transform


def get_pending_observation_features(
    experiment: Experiment, include_failed_as_pending: bool = False
) -> Optional[Dict[str, List[ObservationFeatures]]]:
    """Computes a list of pending observation features (corresponding to arms that
    have been generated and deployed in the course of the experiment, but have not
    been completed with data).

    Args:
        experiment: Experiment, pending features on which we seek to compute.
        include_failed_as_pending: Whether to include failed trials as pending
            (for example, to avoid the model suggesting them again).

    Returns:
        An optional mapping from metric names to a list of observation features,
        pending for that metric (i.e. do not have evaluation data for that metric).
        If there are no pending features for any of the metrics, return is None.
    """
    pending_features = {}
    # Note that this assumes that if a metric appears in fetched data, the trial is
    # not pending for the metric. Where only the most recent data matters, this will
    # work, but may need to add logic to check previously added data objects, too.
    for trial_index, trial in experiment.trials.items():
        dat = trial.fetch_data()
        for metric_name in experiment.metrics:
            if metric_name not in pending_features:
                pending_features[metric_name] = []
            include_since_failed = include_failed_as_pending and trial.status.is_failed
            if isinstance(trial, BatchTrial):
                if trial.status.is_abandoned or (
                    (trial.status.is_deployed or include_since_failed)
                    and metric_name not in dat.df.metric_name.values
                    and trial.arms is not None
                ):
                    for arm in trial.arms:
                        not_none(pending_features.get(metric_name)).append(
                            ObservationFeatures.from_arm(
                                arm=arm, trial_index=np.int64(trial_index)
                            )
                        )
            if isinstance(trial, Trial):
                if trial.status.is_abandoned or (
                    (trial.status.is_deployed or include_since_failed)
                    and metric_name not in dat.df.metric_name.values
                    and trial.arm is not None
                ):
                    not_none(pending_features.get(metric_name)).append(
                        ObservationFeatures.from_arm(
                            arm=not_none(trial.arm), trial_index=np.int64(trial_index)
                        )
                    )
    return pending_features if any(x for x in pending_features.values()) else None


def clamp_observation_features(
    observation_features: List[ObservationFeatures], search_space: SearchSpace
) -> List[ObservationFeatures]:
    range_parameters = [
        p for p in search_space.parameters.values() if isinstance(p, RangeParameter)
    ]
    for obsf in observation_features:
        for p in range_parameters:
            if p.name not in obsf.parameters:
                continue
            if p.parameter_type == ParameterType.FLOAT:
                val = checked_cast(float, obsf.parameters[p.name])
            else:
                val = checked_cast(int, obsf.parameters[p.name])
            if val < p.lower:
                logger.info(
                    f"Untransformed parameter {val} "
                    f"less than lower bound {p.lower}, clamping"
                )
                obsf.parameters[p.name] = p.lower
            elif val > p.upper:
                logger.info(
                    f"Untransformed parameter {val} "
                    f"greater than upper bound {p.upper}, clamping"
                )
                obsf.parameters[p.name] = p.upper
    return observation_features


def pareto_frontier(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    observation_features: Optional[List[ObservationFeatures]] = None,
    observation_data: Optional[List[ObservationData]] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> List[ObservationData]:
    """Helper that applies transforms and calls frontier_evaluator."""
    array_to_tensor = partial(_array_to_tensor, modelbridge=modelbridge)
    X = (
        modelbridge.transform_observation_features(observation_features)
        if observation_features
        else None
    )
    X = array_to_tensor(X) if X is not None else None
    Y, Yvar = (None, None)
    if observation_data:
        Y, Yvar = modelbridge.transform_observation_data(observation_data)
    if Y is not None and Yvar is not None:
        Y, Yvar = (array_to_tensor(Y), array_to_tensor(Yvar))

    # Optimization_config
    mooc = optimization_config or checked_cast_optional(
        MultiObjectiveOptimizationConfig, modelbridge._optimization_config
    )
    if not mooc:
        raise ValueError(
            (
                "Experiment must have an existing optimization_config "
                "of type `MultiObjectiveOptimizationConfig` "
                "or `optimization_config` must be passed as an argument."
            )
        )
    if not isinstance(mooc, MultiObjectiveOptimizationConfig):
        mooc = not_none(MultiObjectiveOptimizationConfig.from_opt_conf(mooc))
    if objective_thresholds:
        mooc = mooc.clone_with_args(objective_thresholds=objective_thresholds)

    optimization_config = mooc

    # Transform OptimizationConfig.
    optimization_config = modelbridge.transform_optimization_config(
        optimization_config=optimization_config,
        fixed_features=ObservationFeatures(parameters={}),
    )
    # Extract weights, constraints, and objective_thresholds
    objective_weights = extract_objective_weights(
        objective=optimization_config.objective, outcomes=modelbridge.outcomes
    )
    outcome_constraints = extract_outcome_constraints(
        outcome_constraints=optimization_config.outcome_constraints,
        outcomes=modelbridge.outcomes,
    )
    objective_thresholds_arr = extract_objective_thresholds(
        objective_thresholds=optimization_config.objective_thresholds,
        outcomes=modelbridge.outcomes,
    )
    # Transform to tensors.
    obj_w, oc_c, _, _ = validate_and_apply_final_transform(
        objective_weights=objective_weights,
        outcome_constraints=outcome_constraints,
        linear_constraints=None,
        pending_observations=None,
        final_transform=array_to_tensor,
    )
    obj_t = array_to_tensor(objective_thresholds_arr)
    frontier_evaluator = get_default_frontier_evaluator()
    # pyre-ignore[28]: Unexpected keyword `modelbridge` to anonymous call
    f, cov = frontier_evaluator(
        model=modelbridge.model,
        X=X,
        Y=Y,
        Yvar=Yvar,
        objective_thresholds=obj_t,
        objective_weights=obj_w,
        outcome_constraints=oc_c,
    )
    f, cov = f.detach().cpu().clone().numpy(), cov.detach().cpu().clone().numpy()
    frontier_observation_data = array_to_observation_data(
        f=f, cov=cov, outcomes=not_none(modelbridge.outcomes)
    )
    # Untransform observations
    for t in reversed(modelbridge.transforms.values()):  # noqa T484
        frontier_observation_data = t.untransform_observation_data(
            frontier_observation_data, []
        )
    return frontier_observation_data


def predicted_pareto_frontier(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    observation_features: Optional[List[ObservationFeatures]] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> List[ObservationData]:
    """Generate a pareto frontier based on the posterior means of given
    observation features.

    Given a model and features to evaluate use the model to predict which points
    lie on the pareto frontier.

    Args:
        modelbridge: Modelbridge used to predict metrics outcomes.
        objective_thresholds: metric values bounding the region of interest in
            the objective outcome space.
        observation_features: observation features to predict. Model's training
            data used by default if unspecified.
        optimization_config: Optimization config

    Returns:
        Data representing points on the pareto frontier.
    """
    observation_features = (
        observation_features
        if observation_features is not None
        else [obs.features for obs in modelbridge.get_training_data()]
    )
    if not observation_features:
        raise ValueError(
            "Must receive observation_features as input or the model must "
            "have training data."
        )

    return pareto_frontier(
        modelbridge=modelbridge,
        objective_thresholds=objective_thresholds,
        observation_features=observation_features,
        optimization_config=optimization_config,
    )


def observed_pareto_frontier(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> List[ObservationData]:
    """Generate a pareto frontier based on observed data.

    Given observed data, return those outcomes in the pareto frontier.

    Args:
        modelbridge: Modelbridge that holds previous training data.
        objective_thresholds: metric values bounding the region of interest in
            the objective outcome space.
        optimization_config: Optimization config

    Returns:
        Data representing points on the pareto frontier.
    """
    # Get observation_data from current training data
    observation_data = [obs.data for obs in modelbridge.get_training_data()]

    return pareto_frontier(
        modelbridge=modelbridge,
        objective_thresholds=objective_thresholds,
        observation_data=observation_data,
        optimization_config=optimization_config,
    )


def hypervolume(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    observation_features: Optional[List[ObservationFeatures]] = None,
    observation_data: Optional[List[ObservationData]] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> float:
    """Helper function that computes hypervolume of a given list of outcomes."""
    array_to_tensor = partial(_array_to_tensor, modelbridge=modelbridge)

    # Extract a tensor of outcome means from observation data.
    observation_data = pareto_frontier(
        modelbridge=modelbridge,
        objective_thresholds=objective_thresholds,
        observation_features=observation_features,
        observation_data=observation_data,
        optimization_config=optimization_config,
    )
    if not observation_data:
        # The hypervolume of an empty set is always 0.
        return 0
    means, _ = modelbridge._transform_observation_data(observation_data)

    # Extract objective_weights and objective_thresholds
    if optimization_config is None:
        optimization_config = (
            # pyre-ignore[16]: Optional has undefined attr `_optimization_config`
            modelbridge._optimization_config.clone()
            if modelbridge._optimization_config is not None
            else None
        )
    else:
        # pyre-ignore[9]: declared as type `Optional[...]` but used as type `...`
        optimization_config = optimization_config.clone()

    if objective_thresholds is not None:
        optimization_config = optimization_config.clone_with_args(
            objective_thresholds=objective_thresholds
        )

    obj_w = extract_objective_weights(
        objective=optimization_config.objective, outcomes=modelbridge.outcomes
    )
    obj_w = array_to_tensor(obj_w)
    objective_thresholds_arr = extract_objective_thresholds(
        objective_thresholds=optimization_config.objective_thresholds,
        outcomes=modelbridge.outcomes,
    )
    obj_t = array_to_tensor(objective_thresholds_arr)
    obj, obj_t = get_weighted_mc_objective_and_objective_thresholds(
        objective_weights=obj_w, objective_thresholds=obj_t
    )
    means = obj(means)
    hv = Hypervolume(ref_point=obj_t)
    return hv.compute(means)


def predicted_hypervolume(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    observation_features: Optional[List[ObservationFeatures]] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> float:
    """Calculate hypervolume of a pareto frontier based on the posterior means of
    given observation features.

    Given a model and features to evaluate calculate the hypervolume of the pareto
    frontier formed from their predicted outcomes.

    Args:
        modelbridge: Modelbridge used to predict metrics outcomes.
        objective_thresholds: point defining the origin of hyperrectangles that
            can contribute to hypervolume.
        observation_features: observation features to predict. Model's training
            data used by default if unspecified.
        optimization_config: Optimization config

    Returns:
        calculated hypervolume.
    """
    observation_features = (
        observation_features
        if observation_features is not None
        else [obs.features for obs in modelbridge.get_training_data()]
    )
    if not observation_features:
        raise ValueError(
            "Must receive observation_features as input or the model must "
            "have training data."
        )

    return hypervolume(
        modelbridge=modelbridge,
        objective_thresholds=objective_thresholds,
        observation_features=observation_features,
        optimization_config=optimization_config,
    )


def observed_hypervolume(
    modelbridge: modelbridge_module.array.ArrayModelBridge,
    objective_thresholds: Optional[TRefPoint] = None,
    optimization_config: Optional[MultiObjectiveOptimizationConfig] = None,
) -> float:
    """Calculate hypervolume of a pareto frontier based on observed data.

    Given observed data, return the hypervolume of the pareto frontier formed from
    those outcomes.

    Args:
        modelbridge: Modelbridge that holds previous training data.
        objective_thresholds: point defining the origin of hyperrectangles that
            can contribute to hypervolume.
        observation_features: observation features to predict. Model's training
            data used by default if unspecified.
        optimization_config: Optimization config

    Returns:
        (float) calculated hypervolume.
    """
    # Get observation_data from current training data.
    observation_data = [obs.data for obs in modelbridge.get_training_data()]

    return hypervolume(
        modelbridge=modelbridge,
        objective_thresholds=objective_thresholds,
        observation_data=observation_data,
        optimization_config=optimization_config,
    )


def array_to_observation_data(
    f: np.ndarray, cov: np.ndarray, outcomes: List[str]
) -> List[ObservationData]:
    """Convert arrays of model predictions to a list of ObservationData.

    Args:
        f: An (n x m) array
        cov: An (n x m x m) array
        outcomes: A list of d outcome names

    Returns: A list of n ObservationData
    """
    observation_data = []
    for i in range(f.shape[0]):
        observation_data.append(
            ObservationData(
                metric_names=list(outcomes),
                means=f[i, :].copy(),
                covariance=cov[i, :, :].copy(),
            )
        )
    return observation_data


def observation_data_to_array(
    observation_data: List[ObservationData],
) -> Tuple[np.ndarray, np.ndarray]:
    """Convert a list of Observation data to arrays.

    Args:
        observation_data: A list of n ObservationData

    Returns:
        An array of n ObservationData, each containing
            - f: An (n x m) array
            - cov: An (n x m x m) array
    """
    means = np.array([obsd.means for obsd in observation_data])
    covs = np.array([obsd.covariance for obsd in observation_data])
    return means, covs


def observation_features_to_array(
    parameters: List[str], obsf: List[ObservationFeatures]
) -> np.ndarray:
    """Convert a list of Observation features to arrays."""
    return np.array([[of.parameters[p] for p in parameters] for of in obsf])


def _array_to_tensor(
    array: Union[np.ndarray, List[float]],
    modelbridge: Optional[modelbridge_module.base.ModelBridge] = None,
) -> Tensor:
    if modelbridge and hasattr(modelbridge, "_array_to_tensor"):
        # pyre-ignore[16]: modelbridge does not have attribute `_array_to_tensor`
        return modelbridge._array_to_tensor(array)
    else:
        return torch.tensor(array)
