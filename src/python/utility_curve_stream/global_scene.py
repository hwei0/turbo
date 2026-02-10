"""Scenario-independent evaluation harness for utility curves and bandwidth allocation.

GlobalScene is similar to Scene but works with scenario-independent utility curve policies
(e.g., GlobalStaticUtilityCurvePolicy) that aggregate data across all scenarios. This is
the harness used at runtime by the BandwidthAllocator, as it does not require knowledge of
which specific driving scenario is active. It evaluates allocations against a single set of
global utility curves derived from all training data.
"""

from utility_curve_stream.base_scene import BaseScene
from utility_curve_stream.allocators.lp_allocator import LPAllocator
from utility_curve_stream.utility_curve_policies.global_static_utility_curve_policy import (
    GlobalStaticUtilityCurvePolicy,
)
from utility_curve_stream.utility_curve_policies.utility_curve_policy import (
    UtilityCurvePolicy,
)


class GlobalScene(BaseScene):
    def __init__(
        self,
        df,
        true_curve_policy: UtilityCurvePolicy = GlobalStaticUtilityCurvePolicy(),
        baseline_allocator=LPAllocator(),
        frame_limit=None,
    ):
        """
        Initialize the GlobalScene with a utility curve policy.

        Parameters:
        - df (pd.DataFrame): The raw utility DataFrame.
        - true_curve_policy (UtilityCurvePolicy): The policy to use for generating utility curves.
        - baseline_allocator: The allocator to use for bandwidth allocation.
        - frame_limit (int, optional): The maximum number of frames to evaluate.
        """
        self.df = df
        self.true_curve_policy = true_curve_policy
        self.frame_limit = frame_limit
        self.baseline_allocator = baseline_allocator

        self.true_utility_curves = self.true_curve_policy.generate_utility_curves(
            self.df, scenario_id=None, frame_limit=self.frame_limit
        )

    def get_local_model_utility_timestamp(
        self, local_model, target_timestamp: int, t_SLO
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
