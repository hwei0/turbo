"""Concrete step-function utility curve mapping bandwidth to detection accuracy.

A ConcreteUtilityCurve represents a finalized, non-decreasing step function where each
step corresponds to a (model, compression) configuration. Given a bandwidth value,
lookup() returns the best model name and its utility that can be served within that
bandwidth budget. Supports visualization as a step plot and serialization to dict/string.
"""

from matplotlib import pyplot as plt


class ConcreteUtilityCurve:
    """
    A concrete instance of a utility curve based on bandwidth intervals.

    This class wraps around a dictionary representing utility values for different bandwidth intervals
    and provides methods for utility lookup and visualization.
    """

    def __init__(self, service_name, utility_curve, model_names):
        """
        Initialize the ConcreteUtilityCurve with a dictionary of bandwidth intervals.

        Parameters:
        - utility_curve (dict): A dictionary where keys are tuples representing bandwidth intervals (start, end)
                                and values are the utility values for those intervals.
        - model_names (list): A list of model names corresponding to the keys in the utility_curve.
        """
        self.service_name = service_name
        self.utility_curve = utility_curve
        self.model_names = model_names

    def lookup(self, bandwidth):
        """
        Get the utility value for a given amount of bandwidth.

        Parameters:
        - bandwidth (float): The amount of bandwidth in Mbps.

        Returns:
        - (model_name (str), utility (float)): The model name and utility value corresponding to the given bandwidth.
        """
        index = 0
        for (start, end), utility in self.utility_curve.items():
            if start <= bandwidth < end:
                return (self.model_names[index], utility)
            index += 1
        return (
            "unknown",
            0,
        )  # Return 0 utility if bandwidth is beyond all defined intervals

    def to_dict(self):
        return self.utility_curve

    def to_string(self):
        """
        Convert the utility curve to a formatted string.

        Returns:
        - str: The formatted string representation of the utility curve.
        """
        output = f"{self.service_name}\n"
        for index, ((start, end), utility) in enumerate(self.utility_curve.items()):
            model_name = self.model_names[index]
            output += f"  ({start}, {end}): {utility} – {model_name}\n"
        return output

    def num_models(self):
        """
        Get the number of models in the utility curve.

        Returns:
        - int: The number of models in the utility curve.
        """
        assert len(self.model_names) == len(
            self.utility_curve
        ), "Expect to have same number of names and curves"
        return len(self.model_names)

    def get_service_name(self):
        """
        Get the service name associated with the utility curve.

        Returns:
        - str: The service name.
        """
        return self.service_name

    def get_model_names(self):
        """
        Get the list of model names associated with the utility curve.

        Returns:
        - list: The list of model names.
        """
        return self.model_names

    def visualize(self, xlim=None, ylim=None):
        """
        Plot the utility curve step function and return the figure object.

        Returns:
        - fig (matplotlib.figure.Figure): The figure object containing the plot.
        - xlim: Optional, limits for the x-axis.
        - ylim: Optional, limits for the y-axis.
        """
        # Initialize lists to store the x (bandwidth) and y (utility) values
        x_vals = []
        y_vals = []

        for (start, end), utility in self.utility_curve.items():
            # Append start point (x, y)
            x_vals.append(start)
            y_vals.append(utility)

            # Append end point (x, y) to create a step
            x_vals.append(end)
            y_vals.append(utility)

        # Create the figure and axis objects
        fig, ax = plt.subplots()

        # Ensure the last point correctly handles infinity
        if x_vals[-1] == float("inf"):
            x_vals[-1] = x_vals[-2] + 10  # Adjust to a finite value for plotting
            ax.set_xlim(0, x_vals[-1] * 1.1)  # Set x-axis limit to show the last step

        # Plotting the step function
        ax.step(x_vals, y_vals, where="post", color="blue", linewidth=2)
        ax.set_xlabel("Bandwidth (Mbps)")
        ax.set_ylabel("Utility")
        ax.set_title(f"{self.service_name} Utility Curve")
        ax.grid(True)

        if xlim is not None:
            ax.set_xlim(xlim)

        if ylim is not None:
            ax.set_ylim(ylim)

        # Force the figure to render
        fig.canvas.draw()

        # Close the figure to avoid displaying it during function execution
        plt.close(fig)

        # Return the figure object
        return fig
