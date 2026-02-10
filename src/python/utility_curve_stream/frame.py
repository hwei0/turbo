"""Single-timestamp evaluation frame for comparing predicted vs. actual utility.

Frame evaluates bandwidth allocation at a single timestamp using both predicted utility
curves (from the curve policy) and true utility curves (ground truth). It computes
predicted, actual, and optimal utility values, and can sweep across a range of bandwidth
values to produce comparative visualizations (stacked bar charts and utility plots) for
offline analysis of allocation quality.
"""

from matplotlib import pyplot as plt
import numpy as np
from polars import mean
from tqdm import tqdm
from utility_curve_stream.allocators.allocator_fn import AllocatorFn
from utility_curve_stream.utility_curve_utils import CameraName, display_plots_in_grid


class Frame:
    def __init__(
        self,
        scenario_id,
        timestamp,
        true_utility_curve_dict,
        eval_utility_curve_dict,
        allocator: AllocatorFn,
    ):
        """
        Initialize a Frame with the given parameters.

        Parameters:
        - scenario_id (str): The ID of the scenario to evaluate.
        - timestamp (int): The specific timestamp for this frame.
        - true_utility_curve_dict (dict): The true abstract utility curves for the services at this timestamp.
        - eval_utility_curve_dict (dict): The evaluated abstract utility curves for the services at this timestamp.
        - allocator (AllocatorFn): The allocation function that takes concrete utility curves and bandwidth
                                   and produces an allocation.
        """
        self.scenario_id = scenario_id
        self.timestamp = timestamp
        self.true_utility_curve_dict = true_utility_curve_dict
        self.eval_utility_curve_dict = eval_utility_curve_dict
        self.allocator = allocator

    def eval(self, t_RTT, t_SLO, bandwidth):
        """
        Evaluate the utility curves for the given RTT, SLO, and bandwidth.

        Parameters:
        - t_RTT (float): The round-trip time in milliseconds.
        - t_SLO (float): The latency SLO in milliseconds.
        - bandwidth (float): The total bandwidth to be allocated.

        Returns:
        - dict: A dictionary containing the eval allocation, predicted utility, actual utility, and max possible utility.
        """
        concrete_true_curves = {
            camera: curve.get(t_RTT, t_SLO)
            for camera, curve in self.true_utility_curve_dict.items()
        }
        concrete_eval_curves = {
            camera: curve.get(t_RTT, t_SLO)
            for camera, curve in self.eval_utility_curve_dict.items()
        }

        # Allocate bandwidth using the evaluated utility curves
        eval_allocation = self.allocator.allocate(bandwidth, concrete_eval_curves)

        # Compute utilities
        predicted_utility = mean(
            info["expected_utility"] for info in eval_allocation.values()
        )
        actual_utility = mean(
            concrete_true_curves[camera].lookup(info["bandwidth_allocated"])[1]
            for camera, info in eval_allocation.items()
        )

        # Allocate bandwidth using the true utility curves for max possible utility
        max_allocation = self.allocator.allocate(bandwidth, concrete_true_curves)
        max_possible_utility = mean(
            info["expected_utility"] for info in max_allocation.values()
        )

        # Store the bandwidth allocated and thus selected model name for each camera
        alloc_info_per_camera = {}
        for camera, info in eval_allocation.items():
            bw_allocated = info["bandwidth_allocated"]
            model_name = concrete_eval_curves[camera].lookup(bw_allocated)[0]
            alloc_info_per_camera[camera] = {
                "bandwidth_allocated": bw_allocated,
                "model_name": model_name,
            }
        return {
            "eval_allocation": alloc_info_per_camera,
            "predicted_utility": predicted_utility,
            "actual_utility": actual_utility,
            "max_possible_utility": max_possible_utility,
        }

    def eval_range(
        self,
        t_RTT,
        t_SLO,
        bandwidth_range,
        show_model_index=False,
        show_model_index_legend=False,
        smooth_order=False,
        display_utilities=["actual", "optimal", "predicted"],
        y_axis_range=None,
        custom_title=None,
    ):
        """
        Evaluate the utility curves over a range of bandwidths.

        Parameters:
        - t_RTT (float): The round-trip time in milliseconds.
        - t_SLO (float): The latency SLO in milliseconds.
        - bandwidth_range (range): The range of bandwidth values to evaluate.
        - show_model_index (bool): Whether to show the model index as labels on the bars.
        - show_model_index_legend (bool): Whether to show a legend for the model index.
        - smooth_order (bool): Whether to order the bars by the frequency of allocation.
        - display_utilities (list): The types of utilities to display. Must be a subset of ['actual', 'optimal', 'predicted'].
        - y_axis_range (tuple): The range of values to display on the y-axis.

        Returns:
        - matplotlib.figure.Figure: A plot of allocated bandwidth vs available bandwidth.
        - metadata (dict): A dictionary containing the average actual, predicted, and max utility.
        """
        results = []
        # for bandwidth in tqdm(bandwidth_range, "Allocating BW", leave=False):  # removed, too chatty
        for bandwidth in tqdm(bandwidth_range, "Computing frame allocations"):
            eval_result = self.eval(t_RTT, t_SLO, bandwidth)
            results.append((bandwidth, eval_result))

        # Determine the order of services (e.g., cameras)
        camera_names = list(results[0][1]["eval_allocation"].keys())

        # A klutzy fix to move the motion_prediction allocation to the bottom, as it is basically always present...
        # Check if 'MOTION_PREDICTION' is in the list
        if CameraName.MOTION_PREDICTION in camera_names:
            # Remove 'MOTION_PREDICTION' from its current position
            camera_names.remove(CameraName.MOTION_PREDICTION)
            # Append 'MOTION_PREDICTION' to the end of the list
            camera_names.insert(0, CameraName.MOTION_PREDICTION)
        else:
            pass  # MOTION_PREDICTION not present in this configuration

        if smooth_order:
            # Count frequency of allocation for each camera
            camera_freq = {camera: 0 for camera in camera_names}
            for _, result in results:
                for camera, eval_info_per_camera in result["eval_allocation"].items():
                    if eval_info_per_camera["bandwidth_allocated"] > 0:
                        camera_freq[camera] += 1

            # Sort cameras by frequency in descending order
            camera_names = sorted(
                camera_names, key=lambda c: camera_freq[c], reverse=True
            )

        # Prepare the plot
        fig, ax1 = plt.subplots(figsize=(10, 6), dpi=120)

        # X axis is the bandwidth values
        x = np.array(bandwidth_range)
        bar_width = (
            1.0  # Set bar width to 1.0 to remove padding and ensure bars are touching
        )
        if len(x) > 1:
            bar_width = x[1] - x[0]

        # Prepare data for stacked bar plot
        bottom = np.zeros(len(x))

        all_model_name_lists = [
            val.get_model_names() for val in self.eval_utility_curve_dict.values()
        ]
        possible_model_names = sorted(
            list(set(item for sublist in all_model_name_lists for item in sublist))
        )

        for camera in camera_names:
            bandwidth_allocations = [
                result["eval_allocation"][camera]["bandwidth_allocated"]
                for _, result in results
            ]
            model_names = [
                result["eval_allocation"][camera]["model_name"] for _, result in results
            ]
            bars = ax1.bar(
                x,
                bandwidth_allocations,
                width=bar_width,
                bottom=bottom,
                label=camera.name,
            )

            # Add text labels for model index if the flag is enabled
            if show_model_index:
                last_model_name = None
                for j, bar in enumerate(bars):
                    current_model_name = model_names[j]
                    if current_model_name != last_model_name and bar.get_height() > 0:
                        current_model_index = possible_model_names.index(
                            current_model_name
                        )
                        ax1.text(
                            bar.get_x() + bar.get_width() / 2,
                            bar.get_height() / 2 + bar.get_y(),
                            f"{current_model_index}",
                            ha="center",
                            va="center",
                            color="black",
                        )
                        last_model_name = current_model_name

            if show_model_index_legend:
                # Add the legend
                model_legend_text = "\n".join(
                    [f"{idx}: {name}" for idx, name in enumerate(possible_model_names)]
                )
                plt.figtext(
                    1,
                    0.9,
                    model_legend_text,
                    verticalalignment="top",
                    horizontalalignment="left",
                    fontsize=8,
                    bbox=dict(
                        facecolor="white", edgecolor="black", boxstyle="round,pad=0.3"
                    ),
                )
                plt.figtext(
                    1.07,
                    0.915,
                    "Legend",
                    verticalalignment="bottom",
                    horizontalalignment="center",
                    fontsize=10,
                )

            bottom += bandwidth_allocations

        ax1.set_xlabel("Available Bandwidth (Mbps)")
        ax1.set_ylabel("Allocated Bandwidth (Mbps)")
        # ax1.set_xticks(x)  # Only show ticks and labels at evaluated bandwidth points
        # ax1.set_xticklabels([str(bw) for bw in x])
        if custom_title is not None:
            ax1.set_title(custom_title)
        else:
            ax1.set_title(
                f"Frame: BW Sweep for Scen. {self.scenario_id[:4]}...{self.scenario_id[-4:]}, windex {self.timestamp}. Alloc strategy: {str(self.allocator)}"
            )

        # adjust the left Y axis range to match the x axis range
        x_min, x_max = ax1.get_xlim()
        ax1.set_ylim(x_min, x_max)

        ax1.legend(loc="upper left")

        # Plotting utilities over time on the secondary axis
        if len(display_utilities) > 0:
            ax2 = ax1.twinx()
            predicted_utilities = [r[1]["predicted_utility"] for r in results]
            actual_utilities = [r[1]["actual_utility"] for r in results]
            max_utilities = [r[1]["max_possible_utility"] for r in results]
            if "actual" in display_utilities:
                ax2.plot(
                    x,
                    actual_utilities,
                    color="cyan",
                    marker="s",
                    linestyle="-",
                    label=f"Actual Utility [{np.mean(actual_utilities):.3f}]",
                )
            if "optimal" in display_utilities:
                ax2.plot(
                    x,
                    max_utilities,
                    color="blue",
                    marker="x",
                    linestyle="-.",
                    label=f"Optimal Utility [{np.mean(max_utilities):.3f}]",
                )
            if "predicted" in display_utilities:
                ax2.plot(
                    x,
                    predicted_utilities,
                    color="red",
                    marker="o",
                    linestyle="--",
                    label=f"Predicted Utility [{np.mean(predicted_utilities):.3f}]",
                )

            ax2.set_ylabel("Average Utility")
            if y_axis_range is not None:
                ax2.set_ylim(y_axis_range)

            ax2.legend(loc="upper right")

        plt.tight_layout()

        # Force the figure to render
        fig.canvas.draw()

        # Close the figure to avoid displaying it during function execution
        plt.close(fig)

        avg_actual_utility = np.mean(actual_utilities)
        avg_predicted_utility = np.mean(predicted_utilities)
        avg_max_utility = np.mean(max_utilities)

        metadata = {
            "actual_utilities": dict(zip(x.astype(int).tolist(), actual_utilities)),
            "predicted_utilities": dict(
                zip(x.astype(int).tolist(), predicted_utilities)
            ),
            "max_utilities": dict(zip(x.astype(int).tolist(), max_utilities)),
        }

        return fig, metadata

    def visualize_compare_curves(self, t_RTT=20, t_SLO=150):
        """
        Visualize both true and eval utility curves for a particular widnow_start_idx.
        """

        def visualize_curve_dict(curve_dict):
            graphs = []
            for camera, curve in curve_dict.items():
                graphs.append(curve.get(t_RTT=t_RTT, t_SLO=t_SLO).visualize())
            return graphs

        graphs = []
        graphs.extend(visualize_curve_dict(self.true_utility_curve_dict))
        num_cols = len(graphs)
        graphs.extend(visualize_curve_dict(self.eval_utility_curve_dict))

        display_plots_in_grid(
            graphs,
            grid_shape=(2, num_cols),
            title=f"Top: True Utility Curves, Bottom: Eval Utility Curves",
        )
