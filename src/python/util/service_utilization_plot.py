"""Live per-service network utilization visualization plot.

ServiceUtilizationPlot extends LiveWindowPlot to display send rate, receive rate,
and the allocated bandwidth limit for a given perception service over time. The plot
dynamically scales its y-axis based on the maximum bandwidth limit observed.

Updated in real-time from network utilization messages emitted by the QUIC transport layer.
"""

from multiprocessing import Lock
import time
from types import NoneType
from matplotlib import pyplot as plt
from matplotlib.ticker import MultipleLocator
from pydantic import BaseModel
from util.live_window_plot import LineData, LiveWindowPlot, LiveWindowPlotConfig


class ServiceUtilizationPlotConfig(LiveWindowPlotConfig):
    service_id: int
    min_y: float | NoneType = -10
    max_y: float | NoneType = None

    x_major_loc: float = 15
    x_minor_loc: float = 5
    y_major_loc: float = 100
    y_minor_loc: float = 20


class ServiceUtilizationUpdateStruct(BaseModel):
    service_id: int
    timestamp: float
    max_limit: float
    snd_rate: float
    recv_rate: float


class ServiceUtilizationPlot(LiveWindowPlot):
    def __init__(self, config: ServiceUtilizationPlotConfig):
        super().__init__(config)

        self.lock = Lock()
        self.service_id = config.service_id

        self.plot_object_map = {
            "max_limit": LineData(),
            "snd_rate": LineData(),
            "recv_rate": LineData(),
        }

        self.fig, self.ax = plt.subplots(figsize=(10, 6))

        self.figure_lines = {
            k: self.ax.plot([], [], label=f"{k}")[0]
            for k in self.plot_object_map.keys()
        }

        self.ax.grid(True)
        self.ax.xaxis.set_major_locator(MultipleLocator(config.x_major_loc))
        self.ax.xaxis.set_minor_locator(MultipleLocator(config.x_minor_loc))
        self.ax.yaxis.set_major_locator(MultipleLocator(config.y_major_loc))
        self.ax.yaxis.set_minor_locator(MultipleLocator(config.y_minor_loc))

        def refresh():
            self.ax.set_xlim(
                left=time.time() - config.window_size_x, right=time.time(), auto=True
            )
            self.ax.set_ylim(
                bottom=-10,
                top=1.5 * max([10] + self.plot_object_map["max_limit"].y_arr),
                auto=True,
            )

        self.scale_refresh = refresh

        self.ax.set_xlim(auto=True)
        self.ax.set_ylim(bottom=config.min_y, top=config.max_y, auto=True)

        self.ax.set_title(f"Network Utilization: Service {self.service_id}")
        self.ax.set_ylabel("Bandwidth")
        self.ax.set_xlabel("Experiment Timestamp")

    def update_data(
        self, curr_tx: float, update_struct: ServiceUtilizationUpdateStruct
    ):
        assert update_struct.service_id == self.service_id
        with self.lock:
            self.plot_object_map["max_limit"].append(
                update_struct.timestamp, update_struct.max_limit
            )
            # self.plot_object_map['snd_rate'].append(update_struct.timestamp, update_struct.snd_rate if update_struct.snd_rate is not None else (self.plot_object_map['snd_rate'][-1] if len(self.plot_object_map['snd_rate']) > 0 else 0))
            # self.plot_object_map['recv_rate'].append(update_struct.timestamp, update_struct.recv_rate if update_struct.recv_rate is not None else (self.plot_object_map['recv_rate'][-1] if len(self.plot_object_map['recv_rate']) > 0 else 0))

            self.plot_object_map["snd_rate"].append(
                update_struct.timestamp,
                update_struct.snd_rate if update_struct.snd_rate is not None else 0,
            )
            self.plot_object_map["recv_rate"].append(
                update_struct.timestamp,
                update_struct.recv_rate if update_struct.recv_rate is not None else 0,
            )

            super().update_data(curr_tx)

    def refresh_plot(self):
        with self.lock:
            for k, figure_line in self.figure_lines.items():
                plt_object = self.plot_object_map[k]
                figure_line.set_data(plt_object.x_arr, plt_object.y_arr)
                figure_line.set_label(
                    f"{k}: {'None' if len(plt_object) == 0 else f'{plt_object.y_arr[-1]:.4f}'}"
                )
            self.scale_refresh()
            self.ax.legend()
            self.fig.canvas.draw()
            self.fig.canvas.flush_events()
