#!/usr/bin/env python3
"""Startup script for the web dashboard.

Parses command-line arguments (port, host, config file, refresh rate, plotting sleep),
configures environment variables, and launches the Flask + SocketIO web server that
serves real-time diagnostic plots from the experiment at http://localhost:5000.
"""

import sys
import argparse
from web_frontend import main


def parse_args():
    parser = argparse.ArgumentParser(
        description="Start the ML Inference Offloading Web Dashboard"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to run the web server on (default: 5000)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind the server to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "-c", "--config", type=str, help="Path to configuration file (optional)"
    )
    parser.add_argument(
        "--refresh-rate",
        type=float,
        help="Refresh rate in seconds (overrides config file)",
    )
    parser.add_argument(
        "--plotting-sleep",
        type=float,
        help="Plotting loop sleep time in seconds (overrides config file)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    print("=" * 60)
    print("ML Inference Offloading Web Dashboard")
    print("=" * 60)
    print(f"Starting web server on http://{args.host}:{args.port}")
    if args.refresh_rate:
        print(f"Using custom refresh rate: {args.refresh_rate} seconds")
    if args.plotting_sleep:
        print(f"Using custom plotting sleep: {args.plotting_sleep} seconds")
    print("Press Ctrl+C to stop the server")
    print("=" * 60)

    # Set command line overrides as environment variables for the main function to use
    if args.refresh_rate:
        import os

        os.environ["WEB_DASHBOARD_REFRESH_RATE"] = str(args.refresh_rate)
    if args.plotting_sleep:
        import os

        os.environ["WEB_DASHBOARD_PLOTTING_SLEEP"] = str(args.plotting_sleep)

    try:
        main(args.config)
    except KeyboardInterrupt:
        print("\nShutting down web server...")
        sys.exit(0)
    except Exception as e:
        print(f"Error starting web server: {e}")
        sys.exit(1)
