"""Live per-service request success/failure rate visualization plot.

ServiceStatusPlot extends LiveWindowPlot to display a dual-axis time-series plot:
  - Left axis: cumulative counts of total requests, remote requests attempted, and
    successful remote requests for a given perception service
  - Right axis: success rate percentage (successful / attempted remote requests)

Updated in real-time as each Client emits diagnostic status messages.
"""

from multiprocessing import Lock
import time
from types import NoneType

from matplotlib import pyplot as plt
from matplotlib.ticker import MultipleLocator
from pydantic import BaseModel
from util.live_window_plot import LineData, LiveWindowPlot, LiveWindowPlotConfig


class ServiceStatusPlotConfig(LiveWindowPlotConfig):
    service_id: int
    cnt_min_y: float | NoneType = -3
    cnt_max_y: float | NoneType = None

    rate_min_y: float | NoneType = -0.5
    rate_max_y: float | NoneType = 1.05

    cnt_x_major_loc: float = 15
    cnt_x_minor_loc: float = 5
    cnt_y_major_loc: float = 30
    cnt_y_minor_loc: float = 10

    rate_x_major_loc: float = 15
    rate_x_minor_loc: float = 5
    rate_y_major_loc: float = 0.2
    rate_y_minor_loc: float = 0.05


class ServiceStatusUpdateStruct(BaseModel):
    service_id: int
    timestamp: float
    remote_request_made: bool
    remote_request_successful: bool


class ServiceStatusPlot(LiveWindowPlot):
    def __init__(self, config: ServiceStatusPlotConfig):
        super().__init__(config)

        self.lock = Lock()
        self.service_id = config.service_id

        self.plot_object_map = {
            "successful_remote_request_bool": LineData(),
            "attempted_remote_request_bool": LineData(),
            "total_request_bool": LineData(),
            "successful_remote_request_cnt": LineData(),
            "attempted_remote_request_cnt": LineData(),
            "total_request_cnt": LineData(),
            "success_rate": LineData(),
        }

        self.fig, self.cnt_ax = plt.subplots(figsize=(10, 6))
        self.rate_ax = self.cnt_ax.twinx()

        self.rate_ax.grid(True)
        self.rate_ax.yaxis.set_major_locator(MultipleLocator(config.rate_y_major_loc))
        self.rate_ax.yaxis.set_minor_locator(MultipleLocator(config.rate_y_minor_loc))
        self.rate_ax.xaxis.set_major_locator(MultipleLocator(config.rate_x_major_loc))
        self.rate_ax.xaxis.set_minor_locator(MultipleLocator(config.rate_x_minor_loc))

        self.cnt_ax.yaxis.set_major_locator(MultipleLocator(config.cnt_y_major_loc))
        self.cnt_ax.yaxis.set_minor_locator(MultipleLocator(config.cnt_y_minor_loc))
        self.cnt_ax.xaxis.set_major_locator(MultipleLocator(config.cnt_x_major_loc))
        self.cnt_ax.xaxis.set_minor_locator(MultipleLocator(config.cnt_x_minor_loc))

        self.rate_ax.set_xlim(auto=True)
        self.cnt_ax.set_xlim(auto=True)

        def refresh():
            self.rate_ax.set_xlim(
                left=time.time() - config.window_size_x, right=time.time(), auto=True
            )
            self.cnt_ax.set_xlim(
                left=time.time() - config.window_size_x, right=time.time(), auto=True
            )

            self.rate_ax.autoscale(enable=True, axis="y")
            self.cnt_ax.autoscale(enable=True, axis="y")

            self.rate_ax.set_ylim(
                bottom=config.rate_min_y, top=config.rate_max_y, auto=True
            )
            # self.cnt_ax.set_ylim(bottom=config.cnt_min_y, top=config.cnt_max_y, auto=True)
            self.cnt_ax.set_ylim(
                bottom=config.cnt_min_y,
                top=max([5] + list(self.plot_object_map["total_request_cnt"].y_arr)),
                auto=True,
            )

        self.scale_refresh = refresh

        self.cnt_ax.set_title(
            f"Service {self.service_id} Request Success/Fail Counts + Percentages"
        )
        self.cnt_ax.set_ylabel("Raw Counts")
        self.cnt_ax.set_xlabel("Experiment Timestamp")

        self.rate_ax.set_ylabel("Success Rate")

        self.remote_success_cnt_figure_line = self.cnt_ax.plot(
            [], [], label="Successful Remote Request Count"
        )[0]
        self.remote_request_cnt_figure_line = self.cnt_ax.plot(
            [], [], label="All Remote Request Count"
        )[0]
        self.total_request_cnt_figure_line = self.cnt_ax.plot(
            [], [], label="Total Request Count"
        )[0]

        self.success_rate_figure_line = self.rate_ax.plot(
            [], [], label="Remote Success Rate"
        )[0]

    def update_data(self, curr_tx: float, update_struct: ServiceStatusUpdateStruct):
        print("updating")
        with self.lock:
            assert update_struct.service_id == self.service_id
            self.plot_object_map["total_request_bool"].append(curr_tx, 1)
            # print("updating a")

            if update_struct.remote_request_made:
                self.plot_object_map["attempted_remote_request_bool"].append(curr_tx, 1)

            if update_struct.remote_request_successful:
                self.plot_object_map["successful_remote_request_bool"].append(
                    curr_tx, 1
                )

            self.plot_object_map["successful_remote_request_cnt"].append(
                curr_tx,
                sum(self.plot_object_map["successful_remote_request_bool"].y_arr),
            )

            self.plot_object_map["attempted_remote_request_cnt"].append(
                curr_tx,
                sum(self.plot_object_map["attempted_remote_request_bool"].y_arr),
            )

            self.plot_object_map["total_request_cnt"].append(
                curr_tx, sum(self.plot_object_map["total_request_bool"].y_arr)
            )

            self.plot_object_map["success_rate"].append(
                curr_tx,
                self.plot_object_map["successful_remote_request_cnt"].get_latest_value(
                    0
                )
                / (
                    1
                    if self.plot_object_map[
                        "attempted_remote_request_cnt"
                    ].get_latest_value(0)
                    == 0
                    else self.plot_object_map[
                        "attempted_remote_request_cnt"
                    ].get_latest_value(0)
                ),
            )
            # print("updating b")
            super().update_data(curr_tx)
            # print("updating c")

    def refresh_plot(self):
        # print("refreshing plot d")
        # print(self.plot_object_map)
        with self.lock:
            self.remote_success_cnt_figure_line.set_data(
                self.plot_object_map["successful_remote_request_cnt"].x_arr,
                self.plot_object_map["successful_remote_request_cnt"].y_arr,
            )

            self.remote_request_cnt_figure_line.set_data(
                self.plot_object_map["attempted_remote_request_cnt"].x_arr,
                self.plot_object_map["attempted_remote_request_cnt"].y_arr,
            )

            # print(self.plot_object_map['total_request_cnt'].x_arr, self.plot_object_map['total_request_cnt'].y_arr)
            self.total_request_cnt_figure_line.set_data(
                self.plot_object_map["total_request_cnt"].x_arr,
                self.plot_object_map["total_request_cnt"].y_arr,
            )

            self.remote_success_cnt_figure_line.set_label(
                f"Successful Remote Requests: {self.plot_object_map['successful_remote_request_cnt'].get_latest_value(0):.4f}"
            )
            self.remote_request_cnt_figure_line.set_label(
                f"Total Remote Requests: {self.plot_object_map['attempted_remote_request_cnt'].get_latest_value(0):.4f}"
            )
            self.total_request_cnt_figure_line.set_label(
                f"Total Requests: {self.plot_object_map['total_request_cnt'].get_latest_value(0):.4f}"
            )
            self.success_rate_figure_line.set_label(
                f"Success Rate: {self.plot_object_map['success_rate'].get_latest_value(0):.4f}"
            )

            self.scale_refresh()
            self.cnt_ax.legend()
            self.rate_ax.legend()
            self.fig.canvas.draw()
            self.fig.canvas.flush_events()
