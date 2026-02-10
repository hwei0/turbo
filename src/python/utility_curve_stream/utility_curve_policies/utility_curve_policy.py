"""Abstract base class for utility curve generation policies.

A UtilityCurvePolicy defines the strategy for generating utility curves from offline
evaluation data. Different policies represent different assumptions about what information
is available at runtime (e.g., per-frame oracle, static global aggregate, windowed samples).
Subclasses implement generate_utility_curve() to produce a dict mapping camera IDs to
ConcreteUtilityCurve objects for a given (timestamp, RTT, SLO) configuration.
"""

from abc import ABC, abstractmethod
import matplotlib.pyplot as plt
from tqdm import tqdm
from utility_curve_stream.utility_curve_utils import (
    CameraName,
    extract_obj_detect_utilities,
)
import polars as pl


class UtilityCurvePolicy(ABC):
    """
    Abstract base class for utility curve generation policies.
    """

    @abstractmethod
    def generate_utility_curve(self, df, scenario_id, window_start_idx):
        """
        Generate a set of utility curves for the particular window in the scenario.

        Parameters:
        - df (pd.DataFrame): The raw utility DataFrame.
        - scenario_id (str): The ID of the scenario to evaluate.
        - window_start_idx (int): The index of the window to evaluate.

        Returns:
        - AbstractUtilityCurve: The generated utility curve.
        """
        pass

    @abstractmethod
    def generate_utility_curves(self, df, scenario_id, frame_limit=None):
        """
        Generate utility curves for all windows in the scenario.

        Parameters:
        - df (pd.DataFrame): The raw utility DataFrame.
        - scenario_id (str): The ID of the scenario to evaluate.
        - frame_limit (int, optional): The maximum number of frames to evaluate.

        Returns:
        - dict: A dictionary of utility curves.
        """
        pass


# TODO(hwei)
# MOTION_PREDICTION_CURVE = AbstractUtilityCurve(motion_prediction_utility_df, "motion_prediction")
# INCLUDE_MOTION_PREDICTION_CURVE = True
