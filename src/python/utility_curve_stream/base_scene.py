"""Base class for Scene and GlobalScene with shared evaluation methods.

Provides the common methods for evaluating bandwidth allocations against utility
curves, visualizing curves, and sweeping bandwidth ranges. Subclasses must set
``self.true_utility_curves``, ``self.baseline_allocator``, and
``self.true_curve_policy`` in their ``__init__``.
"""

from utility_curve_stream.frame import Frame, display_plots_in_grid


class BaseScene:
    def eval_utilities_and_allocations_timestamp(
        self, bandwidth, target_timestamp: int, t_RTT, t_SLO
    ):
        """Evaluate optimal bandwidth allocation at a single timestamp.

        Steps:
        1. Find the closest utility curve timestamp at or before target_timestamp
        2. Instantiate concrete step functions by fixing RTT and SLO on each camera's
           utility curve (each step function maps bandwidth → expected detection utility
           for a given model config)
        3. Run the LP allocator to select the best model config per service
        4. Return average expected utility and per-service allocation details
           (bandwidth_allocated, model_config_name, expected_utility)

        Parameters:
        - bandwidth: Total bandwidth budget (Mbps).
        - target_timestamp: The target timestamp to evaluate.
        - t_RTT: Round-trip time in milliseconds.
        - t_SLO: Latency SLO in milliseconds.
        """
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

        # Fix RTT and SLO to produce concrete step functions (bandwidth → utility).
        # Each step corresponds to a model config with its required bandwidth and expected utility.
        concrete_curves = {
            camera: curve.get(t_RTT=t_RTT, t_SLO=t_SLO)
            for camera, curve in camera_curves.items()
        }

        # Run LP solver to find optimal per-service model config selection
        allocation = self.baseline_allocator.allocate(bandwidth, concrete_curves)

        average_expected_utility = 0
        for camera, alloc_info in allocation.items():
            average_expected_utility += alloc_info["expected_utility"]

        if len(allocation) == 0:
            print(
                f"Detected empty allocation. Debug info: Allocation: {allocation}, "
                f"Concrete Curves: {concrete_curves}, Timestamp: {timestamp}"
            )
            return None, None, None

        average_expected_utility /= len(allocation)

        allocs_over_time_for_timestamp = {}
        for camera, alloc_info in allocation.items():
            relevant_curve = concrete_curves[camera]
            bandwidth_allocated = alloc_info["bandwidth_allocated"]
            model_name, model_utility = relevant_curve.lookup(bandwidth_allocated)
            assert (
                alloc_info["expected_utility"] == model_utility
            ), f'Model utility mismatch: {alloc_info["expected_utility"]} != {model_utility}'
            allocs_over_time_for_timestamp[camera.value] = (
                bandwidth_allocated,
                model_name,
                model_utility,
            )
        return timestamp, average_expected_utility, allocs_over_time_for_timestamp

    def visualize_true_curves(self, timestamp=None):
        """
        Visualize the true utility curves for a given timestamp.
        """
        aucs = self.true_utility_curves[timestamp]
        utility_curves_dict = {}
        for camera, curve in aucs.items():
            utility_curves_dict[camera] = curve.get(self.t_RTT, self.t_SLO).visualize()

        display_plots_in_grid(utility_curves_dict.values())

    def visualize_compare_curves(
        self, window_start_idx, eval_utility_policy, t_RTT=20, t_SLO=150
    ):
        """
        Visualize both true and eval utility curves for a particular window_start_idx.
        """

        def visualize_curve_dict(curve_dict):
            graphs = []
            for camera, curve in curve_dict.items():
                graphs.append(curve.get(t_RTT=t_RTT, t_SLO=t_SLO).visualize())
            return graphs

        example_true_utility_curve_dict = self.true_utility_curves[window_start_idx]
        example_eval_utility_curve_dict = eval_utility_policy.generate_utility_curve(
            self.df, self.scenario_id, window_start_idx=window_start_idx
        )

        graphs = []
        graphs.extend(visualize_curve_dict(example_true_utility_curve_dict))
        num_cols = len(graphs)
        graphs.extend(visualize_curve_dict(example_eval_utility_curve_dict))

        display_plots_in_grid(
            graphs,
            grid_shape=(2, num_cols),
            title=f"Top: True Utility Curves, Bottom: Eval ({str(eval_utility_policy)}) Utility Curves",
        )

    def get_frame(self, window_start_idx, allocator, eval_utility_policy):
        """
        Get a Frame object for a particular window_start_idx.
        """
        eval_utility_curves = eval_utility_policy.generate_utility_curve(
            self.df, self.scenario_id, window_start_idx=window_start_idx
        )
        frame = Frame(
            self.scenario_id,
            window_start_idx,
            self.true_utility_curves[window_start_idx],
            eval_utility_curves,
            allocator,
        )
        return frame

    def eval_sweep_frame_bw(
        self,
        window_start_idx,
        allocator,
        eval_utility_policy,
        smooth_order=False,
        show_model_index=False,
        bandwidth_range=None,
    ):
        """
        Evaluate the utility curves over a range of bandwidths for a single frame.
        """
        if bandwidth_range is None:
            bandwidth_range = range(0, self.bandwidth + 1)
        frame = self.get_frame(window_start_idx, allocator, eval_utility_policy)
        return frame.eval_range(
            t_RTT=self.t_RTT,
            t_SLO=self.t_SLO,
            bandwidth_range=bandwidth_range,
            show_model_index=show_model_index,
            smooth_order=smooth_order,
        )
