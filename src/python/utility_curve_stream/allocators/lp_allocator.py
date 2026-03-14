"""Linear programming bandwidth allocator using PuLP.

Formulates bandwidth allocation as an LP: maximize total utility across all services
subject to the constraint that the sum of per-service bandwidth allocations does not
exceed total available bandwidth. Each service selects exactly one step (model
configuration) from its utility curve. This is the primary allocator used at runtime
by the BandwidthAllocator.
"""

from abc import ABC, abstractmethod
from random import random

from matplotlib import pyplot as plt
import numpy as np
from polars import mean
import pulp
from matplotlib.backends.backend_pdf import PdfPages
from tqdm import tqdm

from utility_curve_stream.allocators.allocator_fn import AllocatorFn
from utility_curve_stream.utility_curve_utils import CameraName


class LPAllocator(AllocatorFn):
    def allocate(self, total_bandwidth, concrete_step_functions):
        """Solve bandwidth allocation as an LP to maximize total detection utility.

        LP formulation:
          Decision variables: y[i][j] ∈ {0,1} — whether service i selects model config j
          Objective:   maximize Σ_i Σ_j  y[i][j] * utility[i][j]
          Constraints:
            (1) Σ_i Σ_j  y[i][j] * bw_needed[i][j]  ≤  total_bandwidth
            (2) For each service i:  Σ_j y[i][j] = 1   (exactly one model config per service)

        Each service's step function maps bandwidth intervals → utility values.
        The LP picks the best model config per service such that total bandwidth is feasible.
        """
        max_intervals = max(
            csf.num_models() for _, csf in concrete_step_functions.items()
        )

        step_functions = [csf.to_dict() for _, csf in concrete_step_functions.items()]
        service_names = [name for name, _ in concrete_step_functions.items()]
        num_services = len(step_functions)

        # Create the LP problem variable
        prob = pulp.LpProblem("Maximize_Utility", pulp.LpMaximize)

        # Create decision variables that say whether the interval is selected or not.
        y = pulp.LpVariable.dicts(
            "y", (range(num_services), range(max_intervals)), cat="Binary"
        )

        # Objective function to maximize, which is the sum of utilities for intervals selected.
        prob += pulp.lpSum(
            y[i][j] * utility
            for i, f in enumerate(step_functions)
            for j, ((_, _), utility) in enumerate(f.items())
        )

        # Bandwidth constraint — for the selected interval, take the min of the range.
        prob += (
            pulp.lpSum(
                y[i][j] * start
                for i, f in enumerate(step_functions)
                for j, ((start, end), _) in enumerate(f.items())
            )
            <= total_bandwidth
        )

        # Ensure each service gets exactly one allocation
        for i in range(num_services):
            prob += pulp.lpSum(y[i][j] for j in range(len(step_functions[i]))) == 1

        # Solve the problem
        prob.solve(pulp.PULP_CBC_CMD(msg=False))

        # Extract the results
        allocation = {}
        for i in range(num_services):
            for j in range(len(step_functions[i])):
                if pulp.value(y[i][j]) == 1:
                    start, end = list(step_functions[i].keys())[j]
                    utility = step_functions[i][(start, end)]
                    allocation[service_names[i]] = {
                        "bandwidth_allocated": start,  # We allocate the minimum amount of bandwidth needed
                        # "bandwidth_allocated": (start, min(end, total_bandwidth)), # Old line of code...why would end be needed though?
                        "expected_utility": utility,  # the predicted amount of utility from giving this much bandwidth
                    }
                    break

        return allocation

    def __str__(self):
        return "LinearProgram"
