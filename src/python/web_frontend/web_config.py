#!/usr/bin/env python3
"""Configuration loader for the web dashboard.

Loads MainPlotterConfig from YAML configuration files, converts IPC socket addresses
to TCP for web accessibility, and provides WebDashboardConfig with refresh rate and
plotting sleep settings. Supports environment variable overrides and provides fallback
default configurations for standalone testing.
"""

import yaml
from util.plotting_main import MainPlotterConfig
from util.bandwidth_allocation_plot import BandwidthAllocationPlotConfig
from util.service_status_plot import ServiceStatusPlotConfig
from util.service_utilization_plot import ServiceUtilizationPlotConfig


def load_config_from_yaml(config_file: str = "client_config.yaml") -> MainPlotterConfig:
    """
    Load configuration from your existing YAML config file.
    """
    try:
        with open(config_file, "r") as f:
            config_data = yaml.safe_load(f)

        # Extract the main_plotter_config section from your YAML
        if "main_plotter_config" in config_data:
            plotter_config = config_data["main_plotter_config"]

            zmq_name = plotter_config["zmq_incoming_diagnostic_name"]
            
            # Build the configuration using your actual structure
            plotting_config = {
                "zmq_incoming_diagnostic_name": zmq_name,
                "bandwidth_allocation_plot_config": plotter_config[
                    "bandwidth_allocation_plot_config"
                ],
                "service_status_plot_config": plotter_config[
                    "service_status_plot_config"
                ],
                "service_utilization_plot_config": plotter_config[
                    "service_utilization_plot_config"
                ],
                "plotting_loop_sleep_seconds": plotter_config[
                    "plotting_loop_sleep_seconds"
                ],
            }

            return MainPlotterConfig(**plotting_config)
        else:
            print(f"No main_plotter_config found in {config_file}, using defaults")
            return get_default_config()

        return MainPlotterConfig(**plotting_config)

    except FileNotFoundError:
        print(f"Config file {config_file} not found, using default configuration")
        return get_default_config()
    except Exception as e:
        print(f"Error loading config: {e}, using default configuration")
        return get_default_config()


class WebDashboardConfig:
    """Configuration for web dashboard specific settings"""

    def __init__(
        self,
        refresh_rate_seconds: float = 3.0,
        plotting_loop_sleep_seconds: float = 1.0,
    ):
        self.refresh_rate_seconds = refresh_rate_seconds
        self.plotting_loop_sleep_seconds = plotting_loop_sleep_seconds


def get_default_config() -> MainPlotterConfig:
    """Get default configuration for testing"""
    return MainPlotterConfig(
        zmq_incoming_diagnostic_name="tcp://*:5555",
        bandwidth_allocation_plot_config=BandwidthAllocationPlotConfig(
            window_size_x=300, service_id_list=[1, 2, 3]
        ),
        service_status_plot_config=[
            ServiceStatusPlotConfig(service_id=1, window_size_x=300),
            ServiceStatusPlotConfig(service_id=2, window_size_x=300),
            ServiceStatusPlotConfig(service_id=3, window_size_x=300),
        ],
        service_utilization_plot_config=[
            ServiceUtilizationPlotConfig(service_id=1, window_size_x=300),
            ServiceUtilizationPlotConfig(service_id=2, window_size_x=300),
            ServiceUtilizationPlotConfig(service_id=3, window_size_x=300),
        ],
    )


def get_web_dashboard_config(config_file: str = None) -> WebDashboardConfig:
    """Get web dashboard specific configuration"""
    import os

    # Default values
    refresh_rate = 3.0
    plotting_sleep = 1.0

    try:
        if config_file:
            with open(config_file, "r") as f:
                config_data = yaml.safe_load(f)
        else:
            # Try to load from existing config files
            config_files = [
                "client_config.yaml",
                "client_config_debug_1cam.yaml",
                "server_config_gcloud.yaml",
            ]
            config_data = None
            for cf in config_files:
                try:
                    with open(cf, "r") as f:
                        config_data = yaml.safe_load(f)
                        break
                except:
                    continue

        if config_data and "web_dashboard_config" in config_data:
            web_config = config_data["web_dashboard_config"]
            refresh_rate = web_config.get("refresh_rate_seconds", 3.0)
            plotting_sleep = web_config.get("plotting_loop_sleep_seconds", 1.0)
    except Exception as e:
        print(f"Could not load web dashboard config: {e}")

    # Check for environment variable overrides (from command line args)
    if "WEB_DASHBOARD_REFRESH_RATE" in os.environ:
        try:
            refresh_rate = float(os.environ["WEB_DASHBOARD_REFRESH_RATE"])
        except ValueError:
            print(
                f"Invalid refresh rate in environment: {os.environ['WEB_DASHBOARD_REFRESH_RATE']}"
            )

    if "WEB_DASHBOARD_PLOTTING_SLEEP" in os.environ:
        try:
            plotting_sleep = float(os.environ["WEB_DASHBOARD_PLOTTING_SLEEP"])
        except ValueError:
            print(
                f"Invalid plotting sleep in environment: {os.environ['WEB_DASHBOARD_PLOTTING_SLEEP']}"
            )

    return WebDashboardConfig(
        refresh_rate_seconds=refresh_rate, plotting_loop_sleep_seconds=plotting_sleep
    )


def get_config_for_web(config_file: str = None) -> MainPlotterConfig:
    """
    Get configuration specifically for web frontend.
    This function tries to load from your config file or uses defaults.
    """
    if config_file:
        return load_config_from_yaml(config_file)

    # Try common config file names
    config_files = [
        "../../../config/client_config.yaml",
    ]

    for config_file in config_files:
        try:
            return load_config_from_yaml(config_file)
        except:
            continue

    print("No config file found, using default configuration")
    return get_default_config()
