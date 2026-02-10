"""Abstract utility curve that converts raw evaluation data into a concrete step-function
utility curve parameterized by RTT and latency SLO.

Given a set of (model, compression) configurations with their detection accuracies and
transport sizes, computes the required bandwidth for each configuration as:
  bandwidth = transport_size / (SLO - RTT - runtime)
Then constructs a monotonically non-decreasing step function (ConcreteUtilityCurve)
mapping bandwidth thresholds to the best achievable model and its utility.
"""

from utility_curve_stream.utility_curves.concrete_utility_curve import (
    ConcreteUtilityCurve,
)


class AbstractUtilityCurve:
    DEFAULT_SLO = 300  # Default latency SLO in milliseconds
    DEFAULT_RTT = 30  # Default round-trip time in milliseconds

    def __init__(
        self,
        df,
        service_name,
        size_col="Network Transport Size [Mb]",
        exec_col="Runtime [ms]",
        utility_col="Utility",
        model_name_col="Model",
    ):
        """
        Initialize the UtilityCurveGenerator with a DataFrame.

        Parameters:
        - df (pl.DataFrame): The input DataFrame.
        - size_col (str): The column name for input size.
        - exec_col (str): The column name for execution time.
        - utility_col (str): The column name for utility.
        """
        self.df = df
        self.service_name = service_name
        self.size_col = size_col
        self.exec_col = exec_col
        self.utility_col = utility_col
        self.model_name_col = model_name_col

    def req_bandwidth(self, input_size, t_exec, t_RTT, t_SLO):
        """
        Convert input size to the required bandwidth based on the latency SLO.

        Parameters:
        - input_size (float): The size of the input in Mb.
        - t_exec (float): The execution time in milliseconds.
        - t_RTT (float): The round-trip time in milliseconds.
        - t_SLO (float): The latency SLO in milliseconds.

        Returns:
        - bandwidth (float): The required bandwidth in Mbps.
        """
        available_time = t_SLO - t_RTT - t_exec

        if available_time <= 0:
            return float("inf") if input_size != 0 else 0

        bandwidth = input_size / (available_time / 1000)  # Convert to seconds
        return bandwidth

    def get_model_names(self):
        """
        Get the list of model names.

        Returns:
        - list: The list of model names, in same order as in table.
        """
        return self.df[self.model_name_col].unique(maintain_order=True).to_list()

    def get(self, t_RTT=DEFAULT_RTT, t_SLO=DEFAULT_SLO):
        """
        Generate utility curves for the specified SLO and RTT, ensuring it is monotonically non-decreasing.

        Parameters:
        - t_SLO (float): The latency SLO in milliseconds.
        - t_RTT (float): The round-trip time in milliseconds.

        Returns:
        - ConcreteUtilityCurve: The utility curve as a series of piecewise lines.
        """
        # Step 1: Compute bandwidths and create a list of (bandwidth, utility, model_name) tuples
        data = []
        for row in self.df.iter_rows(named=True):
            bandwidth = self.req_bandwidth(
                row[self.size_col], row[self.exec_col], t_RTT, t_SLO
            )
            if bandwidth != float("inf"):
                data.append(
                    (bandwidth, row[self.utility_col], row[self.model_name_col])
                )

        # Step 2: Sort the data by bandwidth
        data.sort(key=lambda x: x[0])

        # Step 3: Build the utility curve
        utility_curve = {}
        model_names = []
        max_utility = float("-inf")
        prev_bandwidth = 0

        for bandwidth, utility, model_name in data:
            if utility > max_utility:
                if not utility_curve:
                    # Create the first entry, starting from 0 bandwidth instead of the actual bandwidth.
                    utility_curve[(0, float("inf"))] = utility
                    max_utility = utility
                    model_names.append(model_name)
                    continue

                # Update the end of the previous segment
                (prev_start, _), prev_utility = utility_curve.popitem()
                utility_curve[(prev_start, bandwidth)] = prev_utility

                # Add new segment
                utility_curve[(bandwidth, float("inf"))] = utility
                max_utility = utility
                model_names.append(model_name)

            prev_bandwidth = bandwidth

        return ConcreteUtilityCurve(
            service_name=self.service_name,
            utility_curve=utility_curve,
            model_names=model_names,
        )
