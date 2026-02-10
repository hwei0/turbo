"""Abstract base class for bandwidth allocation strategies.

Defines the AllocatorFn interface: given total available bandwidth and a dict of
per-service ConcreteUtilityCurve step functions, allocate() returns the optimal
per-service bandwidth allocation, model configuration name, and expected utility.
"""

from abc import ABC, abstractmethod
from random import random

from matplotlib import pyplot as plt
import numpy as np
from polars import mean
import pulp
from matplotlib.backends.backend_pdf import PdfPages
from tqdm import tqdm

from utility_curve_stream.utility_curve_utils import CameraName


class AllocatorFn(ABC):

    @abstractmethod
    def allocate(self, total_bandwidth, concrete_step_functions):
        """
        Allocate the mentioned bandwidth given the mentioned step functions.

        Parameters:
        - total_bandwidth (float): The total available bandwidth to be allocated among all services.
        - concrete_step_functions (dict): A dictionary mapping each service to its corresponding
          `ConcreteUtilityCurve` object, which contains a step function in the form of a dictionary where
          keys are tuples representing bandwidth intervals (start, end) and values are the utility values
          for those intervals.

        Returns:
        - allocation (dict): A dictionary where keys are service indices and values are dictionaries with
          the following keys:
          - "bandwidth_allocated" (float): the amount of bandwidth allocated
          - "expected_utility" (float): the expected amount of utility from this allocation
        """
        pass
