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
        """
        Solve the bandwidth allocation problem using linear programming with PuLP.

        This function uses the PuLP library to formulate and solve a linear programming problem that
        maximizes the total utility of bandwidth allocation across multiple services. Each service has
        a step function representing the utility values over different bandwidth intervals. The solver
        determines the optimal bandwidth allocation for each service to maximize the overall utility,
        subject to the total bandwidth constraint.
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
