"""Live bandwidth allocation and utility visualization plot.

BandwidthAllocationPlot extends LiveWindowPlot to display a dual-axis time-series plot:
  - Left axis (stacked area): per-service bandwidth allocation and total available BW
  - Right axis (lines): expected utility from offloading vs. local-only baseline utility
  - Also tracks and displays RTT on a secondary scale

Updated in real-time as the BandwidthAllocator broadcasts allocation decisions.
"""

from multiprocessing import Lock
import time
from types import NoneType
from typing import Dict
from matplotlib import pyplot as plt
from matplotlib.ticker import MultipleLocator
from pydantic import BaseModel
from util.live_window_plot import LineData, LiveWindowPlot, LiveWindowPlotConfig


class BandwidthAllocationPlotConfig(LiveWindowPlotConfig):
    service_id_list: list[int]

    bw_min_y: float | NoneType = -10
    bw_max_y: float | NoneType = None

    utility_min_y: float | NoneType = -10
    utility_max_y: float | NoneType = None

    bw_x_major_loc: float = 15
    bw_x_minor_loc: float = 5
    bw_y_major_loc: float = 100
    bw_y_minor_loc: float = 20

    utility_x_major_loc: float = 15
    utility_x_minor_loc: float = 5
    utility_y_major_loc: float = 100
    utility_y_minor_loc: float = 20


class BandwidthAllocationUpdateStruct(BaseModel):
    timestamp: float
    expected_utility: float
    local_only_utility: float
    service_allocation_map: Dict[int, float]
    available_bw: float
    rtt: float


class BandwidthAllocationPlot(LiveWindowPlot):
    def __init__(self, config: BandwidthAllocationPlotConfig):
        super().__init__(config)

        self.lock = Lock()
        self.service_id_list = config.service_id_list
        self.plot_object_map = {
            k: LineData()
            for k in self.service_id_list
            + ["expected_utility", "local_only_utility", "available_bw", "rtt"]
        }

        self.fig, self.bw_ax = plt.subplots(figsize=(10, 6))

        self.utility_ax = self.bw_ax.twinx()

        self.bw_ax.grid(True)
        self.bw_ax.xaxis.set_major_locator(MultipleLocator(config.bw_x_major_loc))
        self.bw_ax.xaxis.set_minor_locator(MultipleLocator(config.bw_x_minor_loc))
        self.bw_ax.yaxis.set_major_locator(MultipleLocator(config.bw_y_major_loc))
        self.bw_ax.yaxis.set_minor_locator(MultipleLocator(config.bw_y_minor_loc))

        self.utility_ax.xaxis.set_major_locator(
            MultipleLocator(config.utility_x_major_loc)
        )
        self.utility_ax.xaxis.set_minor_locator(
            MultipleLocator(config.utility_x_minor_loc)
        )
        self.utility_ax.yaxis.set_major_locator(
            MultipleLocator(config.utility_y_major_loc)
        )
        self.utility_ax.yaxis.set_minor_locator(
            MultipleLocator(config.utility_y_minor_loc)
        )

        def refresh():
            self.utility_ax.set_xlim(
                left=time.time() - config.window_size_x, right=time.time(), auto=True
            )
            self.bw_ax.set_xlim(
                left=time.time() - config.window_size_x, right=time.time(), auto=True
            )

            # self.bw_ax.set_ylim(bottom=config.bw_min_y, top=config.bw_max_y, auto=True)
            self.bw_ax.set_ylim(
                bottom=config.bw_min_y,
                top=max([10] + self.plot_object_map["available_bw"].y_arr),
                auto=True,
            )
            self.utility_ax.set_ylim(
                bottom=config.utility_min_y, top=config.utility_max_y, auto=True
            )

        self.scale_refresh = refresh

        self.bw_ax.set_title(f"Bandwidth Allocation + Utility Plot (All Services)")
        self.bw_ax.set_ylabel("Bandwidth Allocated")
        self.bw_ax.set_xlabel("Experiment Timestamp")

        self.utility_ax.set_ylabel("Utility (Expected)")

    def update_data(
        self, curr_tx: float, update_struct: BandwidthAllocationUpdateStruct
    ):
        with self.lock:
            for service in self.service_id_list:
                self.plot_object_map[service].append(
                    update_struct.timestamp,
                    update_struct.service_allocation_map[service],
                )

            self.plot_object_map["expected_utility"].append(
                update_struct.timestamp, update_struct.expected_utility
            )
            self.plot_object_map["local_only_utility"].append(
                update_struct.timestamp, update_struct.local_only_utility
            )
            self.plot_object_map["available_bw"].append(
                update_struct.timestamp, update_struct.available_bw
            )
            self.plot_object_map["rtt"].append(
                update_struct.timestamp, update_struct.rtt
            )

            super().update_data(curr_tx)

    def refresh_plot(self):
        with self.lock:
            timestamp_arr = self.plot_object_map.get(self.service_id_list[0]).x_arr

            # TODO: does clearing this, also clear its mirror utility_ax?
            self.bw_ax.clear()

            # if bw_ax, utility_ax independent, can just clear bw_ax and update utility_ax's lines like what is done in the serve utilization plots
            self.utility_ax.clear()

            # TODO: de-hardcode the alpha value
            self.bw_ax.stackplot(
                timestamp_arr,
                [
                    plt_obj.y_arr
                    for plt_obj in [
                        self.plot_object_map[sid] for sid in self.service_id_list
                    ]
                ],
                labels=[
                    f"service {sid}: {self.plot_object_map[sid].get_latest_value(0):.4f}"
                    for sid in self.service_id_list
                ],
                alpha=0.7,
            )

            self.utility_ax.plot(
                self.plot_object_map["expected_utility"].x_arr,
                self.plot_object_map["expected_utility"].y_arr,
                "-k",
                label=f"expected_utility: {self.plot_object_map['expected_utility'].get_latest_value(0):.4f}",
            )
            self.utility_ax.plot(
                self.plot_object_map["local_only_utility"].x_arr,
                self.plot_object_map["local_only_utility"].y_arr,
                "-r",
                label=f"local_only_utility: {self.plot_object_map['local_only_utility'].get_latest_value(0):.4f}",
            )
            self.utility_ax.plot(
                self.plot_object_map["rtt"].x_arr,
                self.plot_object_map["rtt"].y_arr,
                "-g",
                label=f"rtt: {self.plot_object_map['rtt'].get_latest_value(1):.4f}",
            )

            self.bw_ax.plot(
                self.plot_object_map["available_bw"].x_arr,
                self.plot_object_map["available_bw"].y_arr,
                label=f"available_bw: {self.plot_object_map['available_bw'].get_latest_value(0):.4f}",
            )

            self.scale_refresh()
            self.bw_ax.legend()
            self.utility_ax.legend()
            self.fig.canvas.draw()
            self.fig.canvas.flush_events()
