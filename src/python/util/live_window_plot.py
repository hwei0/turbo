"""Base class for time-windowed live matplotlib plots.

LiveWindowPlot provides infrastructure for sliding-window time-series visualization.
It manages LineData objects (x/y arrays with automatic pruning of old data outside the
window) and abstract methods for updating data and refreshing the plot. Subclasses
implement specific plot types (bandwidth allocation, service status, utilization).
"""

from typing import Dict, List
from pydantic import BaseModel
import numpy as np


class LiveWindowPlotConfig(BaseModel):
    window_size_x: float


class LineData(BaseModel):
    # ASSUMPTION: x_arr is monotonic increasing
    x_arr: list = []
    y_arr: list = []

    def __len__(self):
        return len(self.x_arr)

    def prune_unused_data(self, cutoff_x: float):
        idx_first = 0
        while idx_first < len(self.x_arr):
            if self.x_arr[idx_first] >= cutoff_x:
                break
            else:
                idx_first += 1

        if idx_first >= 2:
            self.x_arr = self.x_arr[idx_first - 2 :]
            self.y_arr = self.y_arr[idx_first - 2 :]

    def append(self, x, y):
        self.x_arr.append(x)
        self.y_arr.append(y)

    def get_latest_value(self, default_val=0):
        return self.y_arr[-1] if len(self.y_arr) > 0 else default_val


class LiveWindowPlot:
    def __init__(self, config: LiveWindowPlotConfig):
        self.WINDOW_SIZE_X = config.window_size_x
        self.plot_object_map: Dict[str, LineData] = (
            {}
        )  # this is a list of LineData objects

    # MUST OVERRIDE; to override, must update self.plot_object_list, and call this via super AT THE TAIL OF THE OVERRIDE
    def update_data(self, curr_tx, *args, **kwargs):
        for line_data in self.plot_object_map.values():
            line_data.prune_unused_data(curr_tx - self.WINDOW_SIZE_X)
        # self.refresh_plot()

    # MUST OVERRIDE
    def refresh_plot(self):
        pass
