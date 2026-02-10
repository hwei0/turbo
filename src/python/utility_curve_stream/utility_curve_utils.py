"""Core utilities for the utility curve framework: model naming, data loading, and
visualization helpers.

Provides the standard model naming schema (e.g., "edd2-imgcomp50-inpcompNone"), functions
to parse and generate model configuration names, CameraName enums mapping service IDs to
camera positions (FRONT, FRONT_LEFT, etc.), and data loading routines that read pre-computed
detection accuracy results from Parquet files and join them with model transport size and
runtime metadata from the experiment_model_info.csv file.

The loaded utility data is the foundation for constructing per-service utility curves that
map available bandwidth to achievable detection accuracy.
"""

import numpy as np
import pprint
import plotly.graph_objects as go
import itertools
import random
import time
import matplotlib
import matplotlib.pyplot as plt
import pulp
import polars as pl
import pandas as pd
from typing import Optional
import enum
import os
from statistics import mean
from IPython.display import display
from typing import List
from exceptions import ModelError, ConfigurationError
import io
from tqdm import tqdm
from matplotlib.backends.backend_pdf import PdfPages


# Define this so we can avoid needing to import the Waymo dataset.
class CameraName(enum.Enum):
    """Name of camera."""

    UNKNOWN = 0
    FRONT = 1
    FRONT_LEFT = 2
    FRONT_RIGHT = 3
    SIDE_LEFT = 4
    SIDE_RIGHT = 5
    MOTION_PREDICTION = (
        6  # hack to allow for quickly getting planning results integrated
    )


EDD_MODELS = {
    "edd1": "efficientdet-d1",
    "edd2": "efficientdet-d2",
    "edd4": "efficientdet-d4",
    "edd6": "efficientdet-d6",
    "edd7x": "efficientdet-d7x",
}


def read_motion_prediction():
    # Note: this data is at the 99th percentile (for the transport size)
    # actual runtime for the cnn is 241.77ms, but I mark it as 1ms so it can always run (on-car)
    data = """
    Model,Network Transport Size [Mb],Runtime [ms],Utility
    cnn-on-car, 0, 1, 0.2213
    transformer-cloud, 0.1462, 45.86, 0.3995"""
    schema = {
        "Model": pl.Utf8,
        "Network Transport Size [Mb]": pl.Float64,
        "Runtime [ms]": pl.Float64,
        "Utility": pl.Float32,
    }
    # Convert the data to a DataFrame
    result_df = pl.read_csv(io.StringIO(data), schema=schema)
    # Rename columns to remove any leading/trailing whitespace
    result_df = result_df.rename({col: col.strip() for col in result_df.columns})
    # Strip whitespace from values in the 'Model' column
    result_df = result_df.with_columns(pl.col("Model").str.strip_chars())
    return result_df


def __read_model_information_csv(model_info_csvpath):
    model_csv = pd.read_csv(model_info_csvpath).set_index("Model")

    return model_csv


def generate_model_name(model, img_comp=None, inp_comp=None):
    assert (
        img_comp is None or inp_comp is None
    ), "At least one of the two should be None as they cannot both be set together."
    img_comp_str = f"imgcomp{img_comp if img_comp is not None else 'None'}"
    inp_comp_str = f"inpcomp{inp_comp if inp_comp is not None else 'None'}"
    # only one will ever be true
    return f"{model}-{img_comp_str}-{inp_comp_str}"


def parse_model_name(model_name):
    parts = model_name.split("-")
    if len(parts) < 2 or len(parts) > 3:
        raise ModelError(f"Invalid model name format: {model_name}")

    base_model = parts[0]
    img_comp = None
    inp_comp = None

    if len(parts) == 3:
        comp_type, comp_value = parts[1], parts[2]
        if comp_type == "image":
            img_comp = comp_value
        elif comp_type == "input":
            inp_comp = comp_value
        else:
            raise ModelError(f"Invalid compression type: {comp_type}")
    elif len(parts) == 2:
        comp_type = parts[1]
        if comp_type != "image" and comp_type != "input":
            raise ModelError(f"Invalid compression type: {comp_type}")

    return (
        (
            f"efficientdet-{base_model.upper()}"
            if base_model.startswith("ed")
            else base_model
        ),
        img_comp,
        inp_comp,
    )


# @title Final Dataset Reading


def generate_filenames(base_dir, models, window_size=20) -> List[str]:
    filenames = {}
    compression_levels = ["50", "75", "90", "95", "PNG", None]

    for shortname, model in models.items():
        model_dir = os.path.join(base_dir, model)

        # Base model file
        # filenames.append(os.path.join(model_dir, f"{model}.parquet")). # not sure what this one is for

        if model == "efficientdet-d1":
            # No compression data for ED1, as everything happens locally.
            # key = f'{shortname}_WS={window_size}' # old key
            key = generate_model_name(shortname, img_comp=None, inp_comp=None)
            filenames[key] = os.path.join(
                model_dir, f"{model}-window_size={window_size}.parquet"
            )
            continue

        # Compressed image variations
        for level in compression_levels:
            key = generate_model_name(shortname, img_comp=level, inp_comp=None)
            filename = (
                f"{model}-compress-image-{level}-window_size={window_size}.parquet"
            )
            if level is None:
                filename = f"{model}-window_size={window_size}.parquet"
            filenames[key] = os.path.join(model_dir, filename)

        # Compressed inputs variations
        for level in compression_levels:
            key = generate_model_name(shortname, img_comp=None, inp_comp=level)
            filename = (
                f"{model}-compress-inputs-{level}-window_size={window_size}.parquet"
            )
            if level is None:
                filename = f"{model}-window_size={window_size}.parquet"
            filenames[key] = os.path.join(model_dir, filename)

    return filenames


def read_parquet_data(
    base_directory, model_info_csvpath, window_size=20, fill_none=True
):
    valid_window_sizes = ["1", "10", "20", "30", "40", "50"]

    if str(window_size) not in valid_window_sizes:
        raise ConfigurationError(f"Invalid window size. Must be one of {valid_window_sizes}.")

    max_start_idx = 199 - window_size  # There are 198 unique timestamps per scenario.

    columns = [
        "context",
        "camera_id",
        "window_start_idx",
        "window_end_idx",
        "window_start_us",
        "window_end_us",
        "mean_average_precision",
    ]

    paths = generate_filenames(base_directory, EDD_MODELS, window_size=window_size)

    def load_df(path):
        try:
            df = pl.read_parquet(path, columns=columns)
            df = df.rename(
                {"mean_average_precision": "Utility"}
            )  # TODO(akrentsel): mean_average_precision for new dataset
            # replace Null utility values with 1.0. This is because in the underlying
            # data, Null represents the case of *no* prediction when that is correct.
            df = df.with_columns(
                pl.col("Utility").cast(pl.Float32).fill_null(1.0).alias("Utility")
            )
            return df
        except FileNotFoundError as e:
            print("NOT FOUND")
            raise e
            return None

    dfs = {k: load_df(v) for k, v in paths.items()}
    del_keys = []
    for k in dfs:
        if dfs[k] is None:
            del_keys.append(k)
    for k in del_keys:
        del dfs[k]
    raw_accuracies_df = pl.concat(
        v.with_columns(pl.lit(k).alias("Model")) for k, v in dfs.items()
    )

    # This file is committed to the repository.
    raw_accuracies_df = raw_accuracies_df.join(
        pl.from_pandas(
            __read_model_information_csv(model_info_csvpath), include_index=True
        ),
        on="Model",
        how="left",
    )

    return raw_accuracies_df


def extract_obj_detect_utilities(
    df: pl.DataFrame,
    scenario: Optional[str] = None,
    camera: Optional[object] = None,
    window_start_idx: Optional[int] = None,
    max_latency: Optional[
        int
    ] = 150,  # set default max latency to be 150ms, since that's the SLO we're going with
) -> pl.DataFrame:
    """Get utilities at a specific point from the concatenated dataframes."""
    if scenario is None:
        query = pl.col("context").is_null()
    else:
        query = pl.col("context") == scenario
    if camera is None:
        query &= pl.col("camera_id").is_null()
    else:
        query &= pl.col("camera_id") == camera.value
    if window_start_idx is None:
        query &= pl.col("window_start_idx").is_null()
    else:
        query &= pl.col("window_start_idx") == window_start_idx

    if max_latency is not None:
        # Add filter to drop rows where Runtime [ms] is greater than max_latency
        query &= pl.col("Runtime [ms]") <= max_latency

    res = df.filter(query).select(
        # "Model", "Input size [Mb]", "Jetson Orin [ms]", "H100 [ms]", "Utility"
        "Model",
        "Network Transport Size [Mb]",
        "Runtime [ms]",
        "Utility",
    )

    # Manually set the runtime for ed1-on-vehcile to be 1ms, super fast. To make
    # sure it is runnable.
    res = res.with_columns(
        pl.when(
            (pl.col("Model") == "ed1-on-vehicle").or_(
                pl.col("Model") == "edd1-imgcompNone-inpcompNone"
            )
        )
        .then(1)
        .otherwise(pl.col("Runtime [ms]"))
        .alias("Runtime [ms]")
    )

    return res


# @title `display_plots_in_grid` for visualization
def display_plots_in_grid(
    plots, title=None, grid_shape=None, figsize=None, path=None, dpi=300, spacing=0.1
):
    """
    Displays a list of matplotlib figure objects in a grid layout and optionally saves the output to a PDF with higher resolution.

    Parameters:
    - plots (list): List of matplotlib figure objects to be displayed.
    - title (str): Optional, title for the entire figure.
    - grid_shape (tuple): Optional, shape of the grid as (rows, cols). If None, it will be auto-calculated.
    - figsize (tuple): Size of the entire figure containing the grid.
    - path (str): Optional, path to save the output as a PDF. If None, the PDF is not saved.
    - dpi (int): Dots per inch for the output PDF, controlling the resolution. Defaults to 300.
    - spacing (float): Spacing between subplots in inches. Defaults to 0.1.
    """

    if grid_shape is None:
        # Calculate the grid shape automatically if not provided
        n = len(plots)
        cols = int(np.ceil(np.sqrt(n)))
        rows = int(np.ceil(n / cols))
    else:
        rows, cols = grid_shape

    if figsize is None:
        max_width = max(plot.get_figwidth() for plot in plots)
        max_height = max(plot.get_figheight() for plot in plots)
        figsize = (max_width * cols * (1 + spacing), max_height * rows * (1 + spacing))

    # Create subplots with tighter spacing
    fig, axes = plt.subplots(
        rows, cols, figsize=figsize, gridspec_kw={"wspace": spacing, "hspace": spacing}
    )

    # If only one plot, we need to handle it as a single subplot
    if rows == 1 and cols == 1:
        axes = [[axes]]

    # Flatten axes array for easy iteration if it's multi-dimensional
    axes = np.array(axes).reshape(-1)

    for ax, plot in zip(axes, plots):
        # Copy the plot to the axes
        plot_canvas = plot.canvas
        ax.imshow(plot_canvas.renderer.buffer_rgba())
        ax.axis("off")  # Hide axis labels

    # Hide any remaining empty subplots
    for i in range(len(plots), len(axes)):
        axes[i].axis("off")

    # Add title to the entire plt
    if title:
        plt.suptitle(title, fontsize=16)

    plt.tight_layout()

    if path:
        with PdfPages(path) as pdf:
            pdf.savefig(
                fig, dpi=dpi, bbox_inches="tight"
            )  # Save the current figure into the PDF with specified DPI
            plt.close(
                fig
            )  # Close the figure after saving to avoid display during execution
        print(f"Plots saved to {path}")
    else:
        plt.show()
