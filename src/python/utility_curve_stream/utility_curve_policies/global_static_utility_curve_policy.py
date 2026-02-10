# Utility Curve Policies
from abc import ABC, abstractmethod
import matplotlib.pyplot as plt
from tqdm import tqdm
from utility_curve_stream.utility_curve_policies.utility_curve_policy import (
    UtilityCurvePolicy,
)
from utility_curve_stream.utility_curves.abstract_utility_curve import (
    AbstractUtilityCurve,
)
from utility_curve_stream.utility_curve_utils import (
    CameraName,
    extract_obj_detect_utilities,
)
import polars as pl


class GlobalStaticUtilityCurvePolicy(UtilityCurvePolicy):
    """Static global policy that uses a single utility curve aggregated across all
    scenarios and timestamps.

    Generates one curve per camera by aggregating evaluation data from the entire dataset
    regardless of scenario or timestamp. Returns the same curve for every query. This is
    the policy used at runtime by the BandwidthAllocator, as it requires no scenario-
    specific knowledge.
    """

    """
    A policy that computes a single static utility curve for all timestamps
    based on the global data for each camera across all scenarios.
    """

    def generate_utility_curve(self, df, scenario_id=None, window_start_idx=None):
        """
        Generate a set of utility curves for the particular window in the scenario.

        Scenario ID and Window start idx is ignored in this policy, as the curves as the same for all windows in all scenarios.
        """
        frame_dict = {}
        existing_cameras = set(df["camera_id"])
        for camera in CameraName:
            if (
                camera == CameraName.UNKNOWN
                or camera == CameraName.MOTION_PREDICTION
                or camera.value not in existing_cameras
            ):
                continue
            utility_df = extract_obj_detect_utilities(df, camera=camera)
            frame_dict[camera] = AbstractUtilityCurve(
                utility_df, service_name=camera.name
            )
        # TODO(hwei)
        # if INCLUDE_MOTION_PREDICTION_CURVE:
        #   frame_dict[CameraName.MOTION_PREDICTION] = MOTION_PREDICTION_CURVE
        return frame_dict

    def generate_utility_curves(self, df, scenario_id=None, frame_limit=None):
        """
        Generate a single static utility curve for the entire dataset across all scenarios.

        Parameters:
        - df (pd.DataFrame): The raw utility DataFrame.
        - scenario_id (str, optional): Not used in this policy since it is scenario-independent.
        - frame_limit (int, optional): The maximum number of frames to evaluate. Not used in this policy.

        Returns:
        - dict: A dictionary where the key is the timestamp and the value is another
                dictionary mapping camera names to AbstractUtilityCurve instances.
        """
        unique_timestamps = df["window_start_idx"].unique(maintain_order=True)
        unique_timestamps = [
            timestamp for timestamp in unique_timestamps if timestamp is not None
        ]
        if frame_limit is not None:
            unique_timestamps = unique_timestamps[:frame_limit]

        # Generate the curve for a single scenario id and window start idx (doesn't matter which, all the same)
        single_frame_dict = self.generate_utility_curve(df)
        return {timestamp: single_frame_dict for timestamp in unique_timestamps}

    def __str__(self):
        return "GlobalStatic"
