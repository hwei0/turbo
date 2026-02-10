"""Per-scenario evaluation harness for utility curves and bandwidth allocation.

Scene manages utility curve generation and allocation evaluation for a specific driving
scenario. It caches computed utility curves per (scenario, timestamp, RTT, SLO) to avoid
recomputation, and provides methods to evaluate allocations, sweep bandwidth ranges, and
visualize results. Used primarily for offline analysis comparing different allocation
strategies against ground-truth utility curves.
"""

from utility_curve_stream.base_scene import BaseScene
from utility_curve_stream.allocators.lp_allocator import LPAllocator
from utility_curve_stream.utility_curve_policies.optimal_utility_curve_policy import (
    OptimalUtilityCurvePolicy,
)
from utility_curve_stream.utility_curve_policies.utility_curve_policy import (
    UtilityCurvePolicy,
)


class Scene(BaseScene):
    def __init__(
        self,
        scenario_id,
        df,
        true_curve_policy: UtilityCurvePolicy = OptimalUtilityCurvePolicy(),
        baseline_allocator=LPAllocator(),
        frame_limit=None,
        cache=None,
    ):
        """
        Initialize the Scene with a specific scenario ID and utility curve policy.

        Parameters:
        - scenario_id (str): The ID of the scenario to evaluate.
        - df (pd.DataFrame): The raw utility DataFrame.
        - baseline_allocator: The allocator to use for bandwidth allocation.
        - true_curve_policy (UtilityCurvePolicy): The policy to use for generating utility curves.
        - frame_limit (int, optional): The maximum number of frames to evaluate.
        - cache (dict, optional): A dictionary to store pre-computed values for faster evaluation.
        """
        self.df = df
        self.scenario_id = scenario_id
        self.true_curve_policy = true_curve_policy
        self.frame_limit = frame_limit
        self.baseline_allocator = baseline_allocator
        self.cache = cache

        if cache is not None and scenario_id in cache:
            cache_dict = cache[scenario_id]
            if "true_utility_curves" in cache_dict:
                self.true_utility_curves = cache_dict["true_utility_curves"]
        else:
            # Compute the true/best baseline utilities and allocations
            self.true_utility_curves = self.true_curve_policy.generate_utility_curves(
                self.df, self.scenario_id, frame_limit=self.frame_limit
            )

            # If cache is not None, store in the cache
            if cache is not None:
                cache[scenario_id] = {
                    "true_utility_curves": self.true_utility_curves,
                }

    def get_local_model_utility_timestamp(
        self, local_model, target_timestamp: int, t_RTT, t_SLO
    ):
        # Find the closest timestamp at or before target_timestamp
        timestamp = -1e9
        camera_curves = None
        for ts, cc in self.true_utility_curves.items():
            if (
                abs(ts - target_timestamp) < abs(timestamp - target_timestamp)
                and ts <= target_timestamp
            ):
                timestamp = ts
                camera_curves = cc

        concrete_curves = {
            camera: curve.get(t_RTT=10 * t_SLO, t_SLO=t_SLO)
            for camera, curve in camera_curves.items()
        }

        allocs_over_time_for_timestamp = {}
        for camera in concrete_curves.keys():
            relevant_curve = concrete_curves[camera]
            model_name, model_utility = relevant_curve.lookup(0)
            allocs_over_time_for_timestamp[camera.value] = (
                0,
                model_name,
                model_utility,
            )
        return timestamp, allocs_over_time_for_timestamp
