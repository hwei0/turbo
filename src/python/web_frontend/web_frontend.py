#!/usr/bin/env python3
"""Real-time web frontend adapter for displaying matplotlib figures via Flask + SocketIO.

WebPlotterAdapter wraps the MainPlotter to convert matplotlib figures into base64-encoded
PNG images and serve them to web clients. Processes ZMQ messages for bandwidth allocation,
service status, and network utilization updates. Connected web browsers receive plot
updates via WebSocket for live monitoring of the experiment.
"""

import io
import base64
import json
import time
import threading
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
import matplotlib

matplotlib.use("Agg")  # Use non-interactive backend for web
import matplotlib.pyplot as plt
from typing import Dict, Any
import logging

LOGGER = logging.getLogger(__name__)

# Import your existing plotting classes
from util.plotting_main import MainPlotter, MainPlotterConfig


class WebPlotterAdapter:
    """Adapter that wraps the MainPlotter to provide web-friendly image output"""

    def __init__(
        self, config: MainPlotterConfig, plotting_loop_sleep_seconds: float = 1.0
    ):
        # Force matplotlib to use Agg backend before creating plots
        matplotlib.use("Agg", force=True)

        self.main_plotter = MainPlotter(config)
        self.plot_images = {}
        self.lock = threading.Lock()
        self.plotting_loop_sleep_seconds = plotting_loop_sleep_seconds

        # Disable GUI-related matplotlib methods that cause threading issues
        self._disable_gui_methods()

        # Set figure backgrounds to white for web display
        for plot in [self.main_plotter.bandwidth_allocation_plot]:
            plot.fig.set_facecolor("white")

        for plot_map in [
            self.main_plotter.service_status_plot_map,
            self.main_plotter.service_utilization_plot_map,
        ]:
            for plot in plot_map.values():
                plot.fig.set_facecolor("white")

    def _disable_gui_methods(self):
        """Disable GUI-related matplotlib methods that cause threading issues"""
        # Disable canvas draw and flush_events methods that require GUI thread
        all_plots = [self.main_plotter.bandwidth_allocation_plot]
        all_plots.extend(self.main_plotter.service_status_plot_map.values())
        all_plots.extend(self.main_plotter.service_utilization_plot_map.values())

        for plot in all_plots:
            if hasattr(plot.fig, "canvas"):
                # Replace problematic methods with no-ops
                plot.fig.canvas.draw = lambda: None
                plot.fig.canvas.flush_events = lambda: None

                # Also disable any cursor-related methods
                if hasattr(plot.fig.canvas, "set_cursor"):
                    plot.fig.canvas.set_cursor = lambda cursor: None

        # Override plt.show() and plt.pause() globally to prevent GUI calls
        import matplotlib.pyplot as plt

        plt.show = lambda *args, **kwargs: None
        plt.pause = lambda *args, **kwargs: None
        plt.ion = lambda: None  # Disable interactive mode

    def get_plot_as_base64(self, fig) -> str:
        """Convert matplotlib figure to base64 encoded image"""
        # Add a timestamp text to the plot to force visual changes
        import matplotlib.pyplot as plt

        # Add timestamp text to the figure
        current_time = time.strftime("%H:%M:%S", time.localtime())
        fig.text(0.02, 0.02, f"Updated: {current_time}", fontsize=8, alpha=0.7)

        img_buffer = io.BytesIO()
        fig.savefig(img_buffer, format="png", dpi=100, bbox_inches="tight")
        img_buffer.seek(0)
        img_base64 = base64.b64encode(img_buffer.getvalue()).decode()
        img_buffer.close()

        # Clear the timestamp text for next update
        fig.texts = [t for t in fig.texts if not t.get_text().startswith("Updated:")]

        return f"data:image/png;base64,{img_base64}"

    def update_plot_images(self):
        """Update all plot images for web display"""
        with self.lock:
            LOGGER.debug("Updating plot images at %s", time.time())

            # Force matplotlib to draw the figures before converting to base64
            try:
                self.main_plotter.bandwidth_allocation_plot.fig.canvas.draw()
                for plot in self.main_plotter.service_status_plot_map.values():
                    plot.fig.canvas.draw()
                for plot in self.main_plotter.service_utilization_plot_map.values():
                    plot.fig.canvas.draw()
            except Exception as e:
                LOGGER.warning("Canvas draw error: %s", e)

            # Generate new base64 images
            bw_image = self.get_plot_as_base64(
                self.main_plotter.bandwidth_allocation_plot.fig
            )
            LOGGER.debug("BW image length: %d, hash: %d", len(bw_image), hash(bw_image))

            # Bandwidth allocation plot
            self.plot_images["bandwidth_allocation"] = {
                "title": "Bandwidth Allocation & Utility",
                "image": bw_image,
                "timestamp": time.time(),
            }

            # Service status plots
            for service_id, plot in self.main_plotter.service_status_plot_map.items():
                self.plot_images[f"service_status_{service_id}"] = {
                    "title": f"Service {service_id} Status",
                    "image": self.get_plot_as_base64(plot.fig),
                    "timestamp": time.time(),
                }

            # Service utilization plots
            for (
                service_id,
                plot,
            ) in self.main_plotter.service_utilization_plot_map.items():
                self.plot_images[f"service_utilization_{service_id}"] = {
                    "title": f"Service {service_id} Utilization",
                    "image": self.get_plot_as_base64(plot.fig),
                    "timestamp": time.time(),
                }

            LOGGER.debug("Updated %d plot images", len(self.plot_images))

    def get_all_plot_images(self) -> Dict[str, Any]:
        """Get all current plot images"""
        with self.lock:
            return self.plot_images.copy()

    def cleanup(self):
        """Clean up resources"""
        self.main_plotter.terminated = True
        if hasattr(self.main_plotter, "zmq_incoming_diagnostic_socket"):
            self.main_plotter.zmq_incoming_diagnostic_socket.close()
        if hasattr(self.main_plotter, "context"):
            self.main_plotter.context.term()

    def run_plotting_loop(self):
        """Run the main plotting loop in a separate thread"""

        def plotting_thread():
            while not self.main_plotter.terminated:
                # Process ZMQ messages and update plots
                while self.main_plotter.zmq_incoming_diagnostic_socket.poll(timeout=10):
                    recv_json = json.loads(
                        self.main_plotter.zmq_incoming_diagnostic_socket.recv_string()
                    )

                    if recv_json["plot_id"] == -1:  # KILL_SIGNAL
                        self.main_plotter.terminated = True
                        return
                    elif recv_json["plot_id"] == 1:  # CLIENT_STATUS_UPDATE
                        from util.service_status_plot import ServiceStatusUpdateStruct

                        self.main_plotter.service_status_plot_map[
                            recv_json["service_id"]
                        ].update_data(
                            curr_tx=recv_json["timestamp"],
                            update_struct=ServiceStatusUpdateStruct(
                                service_id=recv_json["service_id"],
                                timestamp=recv_json["timestamp"],
                                remote_request_made=recv_json["remote_request_made"],
                                remote_request_successful=recv_json[
                                    "remote_request_successful"
                                ],
                            ),
                        )
                    elif recv_json["plot_id"] == 2:  # BANDWIDTH_ALLOCATION_UPDATE
                        from util.bandwidth_allocation_plot import (
                            BandwidthAllocationUpdateStruct,
                        )

                        self.main_plotter.bandwidth_allocation_plot.update_data(
                            curr_tx=recv_json["timestamp"],
                            update_struct=BandwidthAllocationUpdateStruct(
                                timestamp=recv_json["timestamp"],
                                expected_utility=recv_json["expected_utility"],
                                local_only_utility=recv_json["local_utility"],
                                service_allocation_map=recv_json["allocation_map"],
                                available_bw=recv_json["available_bw"],
                                rtt=recv_json["rtt"],
                            ),
                        )
                    elif recv_json["plot_id"] == 3:  # NETWORK_UTILIZATION_UPDATE
                        from util.service_utilization_plot import (
                            ServiceUtilizationUpdateStruct,
                        )

                        self.main_plotter.service_utilization_plot_map[
                            recv_json["service_id"]
                        ].update_data(
                            curr_tx=recv_json["timestamp"],
                            update_struct=ServiceUtilizationUpdateStruct(
                                service_id=recv_json["service_id"],
                                timestamp=recv_json["timestamp"],
                                max_limit=recv_json["max_limit"],
                                snd_rate=recv_json["snd_rate"],
                                recv_rate=recv_json["recv_rate"],
                            ),
                        )

                # Refresh all plots (GUI methods are now disabled)
                try:
                    self.main_plotter.bandwidth_allocation_plot.refresh_plot()
                    for plot in self.main_plotter.service_status_plot_map.values():
                        plot.refresh_plot()
                    for plot in self.main_plotter.service_utilization_plot_map.values():
                        plot.refresh_plot()
                except Exception as e:
                    LOGGER.warning("Plot refresh error (expected with web backend): %s", e)

                # Always update web images (even without new data, time axis should update)
                self.update_plot_images()

                time.sleep(self.plotting_loop_sleep_seconds)

        thread = threading.Thread(target=plotting_thread, daemon=True)
        thread.start()
        return thread


# Flask app setup
app = Flask(__name__)
app.config["SECRET_KEY"] = "your-secret-key-here"
socketio = SocketIO(app, cors_allowed_origins="*")

# Global plotter instance
web_plotter = None
connected_clients = 0


@app.route("/")
def index():
    """Main dashboard page"""
    return render_template("dashboard.html")


@app.route("/api/plots")
def get_plots():
    """API endpoint to get all current plot images"""
    if web_plotter:
        return jsonify(web_plotter.get_all_plot_images())
    return jsonify({})


@app.route("/api/config")
def get_config():
    """API endpoint to get dashboard configuration"""
    from web_config import get_web_dashboard_config

    web_config = get_web_dashboard_config()
    return jsonify(
        {
            "refresh_rate_seconds": web_config.refresh_rate_seconds,
            "plotting_loop_sleep_seconds": web_config.plotting_loop_sleep_seconds,
        }
    )


@app.route("/api/test")
def test_plots():
    """Test endpoint to check if plots are being generated"""
    if web_plotter:
        plot_data = web_plotter.get_all_plot_images()
        return jsonify(
            {
                "plot_count": len(plot_data),
                "plot_keys": list(plot_data.keys()),
                "sample_image_length": (
                    len(plot_data.get("bandwidth_allocation", {}).get("image", ""))
                    if plot_data
                    else 0
                ),
                "timestamp": time.time(),
            }
        )
    return jsonify({"error": "No web_plotter available"})


@socketio.on("connect")
def handle_connect():
    """Handle client connection"""
    global connected_clients
    connected_clients += 1
    LOGGER.info("Client connected (total: %d)", connected_clients)
    if web_plotter:
        plot_data = web_plotter.get_all_plot_images()
        LOGGER.info("Sending initial %d plots to new client", len(plot_data))
        emit("plots_update", plot_data)


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection"""
    global connected_clients
    connected_clients = max(0, connected_clients - 1)
    LOGGER.info("Client disconnected (total: %d)", connected_clients)


@socketio.on("request_plots")
def handle_request_plots():
    """Handle client request for plot updates"""
    LOGGER.debug("Received manual plot update request")
    if web_plotter:
        plot_data = web_plotter.get_all_plot_images()
        LOGGER.debug("Sending %d plots to client", len(plot_data))
        emit("plots_update", plot_data)
    else:
        LOGGER.warning("No web_plotter available")


def main(config: str):
    """Main function to start the web server"""
    global web_plotter

    # Load configuration from your existing config files
    from web_config import get_config_for_web, get_web_dashboard_config

    try:
        config = get_config_for_web(config)
        web_config = get_web_dashboard_config()

        LOGGER.info("Loaded config with ZMQ address: %s", config.zmq_incoming_diagnostic_name)
        LOGGER.info("Service IDs: %s", config.bandwidth_allocation_plot_config.service_id_list)
        LOGGER.info("Refresh rate: %s seconds", web_config.refresh_rate_seconds)
        LOGGER.info("Plotting loop sleep: %s seconds", web_config.plotting_loop_sleep_seconds)

        web_plotter = WebPlotterAdapter(config, web_config.plotting_loop_sleep_seconds)

        # Start the plotting loop
        web_plotter.run_plotting_loop()

        LOGGER.info("Starting web server on http://localhost:5000")
        LOGGER.info("Press Ctrl+C to stop")
        socketio.run(app, host="0.0.0.0", port=5000, debug=False)

    except KeyboardInterrupt:
        LOGGER.info("Shutting down...")
        if web_plotter:
            web_plotter.cleanup()
    except Exception as e:
        LOGGER.error("Error starting web server: %s", e)
        import traceback

        traceback.print_exc()
        if web_plotter:
            web_plotter.cleanup()
    finally:
        LOGGER.info("Web dashboard stopped")


if __name__ == "__main__":
    main()
