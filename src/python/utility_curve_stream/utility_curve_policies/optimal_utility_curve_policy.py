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


class OptimalUtilityCurvePolicy(UtilityCurvePolicy):
    """Oracle policy that generates per-frame optimal utility curves.

    Creates one utility curve per camera per timestamp using the exact evaluation data
    for that specific frame. This represents the best possible curve if the system had
    perfect knowledge of each frame's content — used as an upper-bound baseline.
    """

    def generate_utility_curve(self, df, scenario_id, window_start_idx):
        """
        Generate a set of utility curves for the particular window in the scenario.
        """

        frame_dict = {}
        for camera in CameraName:
            if camera == CameraName.UNKNOWN or camera == CameraName.MOTION_PREDICTION:
                continue
            utility_df = extract_obj_detect_utilities(
                df,
                scenario=scenario_id,
                camera=camera,
                window_start_idx=window_start_idx,
            )
            frame_dict[camera] = AbstractUtilityCurve(
                utility_df, service_name=camera.name
            )

        # TODO(hwei)
        # if INCLUDE_MOTION_PREDICTION_CURVE:
        #   frame_dict[CameraName.MOTION_PREDICTION] = MOTION_PREDICTION_CURVE

        return frame_dict

    def generate_utility_curves(self, df, scenario_id, frame_limit=None):
        """
        Generate optimal utility curves for each timestamp in the scenario.
        """
        curves_dict = {}
        unique_timestamps = df["window_start_idx"].unique(maintain_order=True)
        # Remove None values
        unique_timestamps = [
            timestamp for timestamp in unique_timestamps if timestamp is not None
        ]
        if frame_limit is not None:
            unique_timestamps = unique_timestamps[:frame_limit]

        for timestamp in tqdm(
            unique_timestamps, desc="Generating Optimal Utility Curves"
        ):
            curves_dict[timestamp] = self.generate_utility_curve(
                df, scenario_id, timestamp
            )
        return curves_dict

    def __str__(self):
        return "OptimalDynamicPerFrame"
