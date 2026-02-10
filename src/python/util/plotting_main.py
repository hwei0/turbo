"""Flask + SocketIO web dashboard for real-time experiment monitoring.

MainPlotter subscribes to a ZMQ PUB socket that aggregates diagnostic messages from
the BandwidthAllocator, Client instances, and QUIC transport layer. It maintains three
types of live matplotlib plots:
  - BandwidthAllocationPlot: per-service bandwidth allocation and expected utility
  - ServiceStatusPlot: per-service request success/failure rates
  - ServiceUtilizationPlot: per-service network send/receive rates vs. allocated limits

Plots are rendered as base64-encoded PNG images and pushed to connected web clients via
WebSocket (SocketIO). The Flask app serves a dashboard page and provides REST API
endpoints for plot retrieval and configuration.
"""

import json
from typing import List
from pydantic import BaseModel
import zmq
import logging


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

from util.bandwidth_allocation_plot import (
    BandwidthAllocationPlotConfig,
    BandwidthAllocationPlot,
    BandwidthAllocationUpdateStruct,
)
from util.service_status_plot import (
    ServiceStatusPlot,
    ServiceStatusPlotConfig,
    ServiceStatusUpdateStruct,
)
from util.service_utilization_plot import (
    ServiceUtilizationPlot,
    ServiceUtilizationPlotConfig,
    ServiceUtilizationUpdateStruct,
)


class MainPlotterConfig(BaseModel):
    zmq_incoming_diagnostic_name: str
    bandwidth_allocation_plot_config: BandwidthAllocationPlotConfig
    service_status_plot_config: List[ServiceStatusPlotConfig]
    service_utilization_plot_config: List[ServiceUtilizationPlotConfig]
    plotting_loop_sleep_seconds: int


KILL_SIGNAL = -1
CLIENT_STATUS_UPDATE = 1
BANDWIDTH_ALLOCATION_UPDATE = 2
NETWORK_UTILIZATION_UPDATE = 3

LOGGER = logging.getLogger("main_plotter")


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
    print(f"Client connected (total: {connected_clients})")
    if web_plotter:
        plot_data = web_plotter.get_all_plot_images()
        print(f"Sending initial {len(plot_data)} plots to new client")
        emit("plots_update", plot_data)


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection"""
    global connected_clients
    connected_clients = max(0, connected_clients - 1)
    print(f"Client disconnected (total: {connected_clients})")


@socketio.on("request_plots")
def handle_request_plots():
    """Handle client request for plot updates"""
    print("Received manual plot update request")
    if web_plotter:
        plot_data = web_plotter.get_all_plot_images()
        print(f"Sending {len(plot_data)} plots to client")
        emit("plots_update", plot_data)
    else:
        print("No web_plotter available")


class MainPlotter:
    def __init__(self, config: MainPlotterConfig):

        # Force matplotlib to use Agg backend before creating plots
        matplotlib.use("Agg", force=True)

        self.plot_images = {}
        self.lock = threading.Lock()
        self.plotting_loop_sleep_seconds = config.plotting_loop_sleep_seconds

        self.context = (
            zmq.Context()
        )  # IMPORTANT: must store zmq.Context in self; a local variable would be garbage-collected and destroy the sockets
        self.zmq_incoming_diagnostic_socket = self.context.socket(zmq.SUB)
        self.zmq_incoming_diagnostic_socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self.zmq_incoming_diagnostic_socket.bind(config.zmq_incoming_diagnostic_name)
        LOGGER.info("Bound diagnostic ZMQ socket: %s", config.zmq_incoming_diagnostic_name)

        self.terminated = False

        self.bandwidth_allocation_plot = BandwidthAllocationPlot(
            config.bandwidth_allocation_plot_config
        )

        self.service_status_plot_map = {
            service_status_config.service_id: ServiceStatusPlot(service_status_config)
            for service_status_config in config.service_status_plot_config
        }

        self.service_utilization_plot_map = {
            service_utilization_config.service_id: ServiceUtilizationPlot(
                service_utilization_config
            )
            for service_utilization_config in config.service_utilization_plot_config
        }

        # Set figure backgrounds to white for web display
        for plot in [self.bandwidth_allocation_plot]:
            plot.fig.set_facecolor("white")

        for plot_map in [
            self.service_status_plot_map,
            self.service_utilization_plot_map,
        ]:
            for plot in plot_map.values():
                plot.fig.set_facecolor("white")

        # Disable GUI-related matplotlib methods that cause threading issues
        self._disable_gui_methods()

    def _disable_gui_methods(self):
        """Disable GUI-related matplotlib methods that cause threading issues"""
        # Disable canvas draw and flush_events methods that require GUI thread
        all_plots = [self.bandwidth_allocation_plot]
        all_plots.extend(self.service_status_plot_map.values())
        all_plots.extend(self.service_utilization_plot_map.values())

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
            print(f"Updating plot images at {time.time()}")

            # Force matplotlib to draw the figures before converting to base64
            try:
                self.bandwidth_allocation_plot.fig.canvas.draw()
                for plot in self.service_status_plot_map.values():
                    plot.fig.canvas.draw()
                for plot in self.service_utilization_plot_map.values():
                    plot.fig.canvas.draw()
            except Exception as e:
                print(f"Canvas draw error: {e}")

            # Generate new base64 images
            bw_image = self.get_plot_as_base64(self.bandwidth_allocation_plot.fig)
            print(f"BW image length: {len(bw_image)}, hash: {hash(bw_image)}")

            # Bandwidth allocation plot
            self.plot_images["bandwidth_allocation"] = {
                "title": "Bandwidth Allocation & Utility",
                "image": bw_image,
                "timestamp": time.time(),
            }

            # Service status plots
            for service_id, plot in self.service_status_plot_map.items():
                self.plot_images[f"service_status_{service_id}"] = {
                    "title": f"Service {service_id} Status",
                    "image": self.get_plot_as_base64(plot.fig),
                    "timestamp": time.time(),
                }

            # Service utilization plots
            for service_id, plot in self.service_utilization_plot_map.items():
                self.plot_images[f"service_utilization_{service_id}"] = {
                    "title": f"Service {service_id} Utilization",
                    "image": self.get_plot_as_base64(plot.fig),
                    "timestamp": time.time(),
                }

            print(f"Updated {len(self.plot_images)} plot images")

    def get_all_plot_images(self) -> Dict[str, Any]:
        """Get all current plot images"""
        with self.lock:
            return self.plot_images.copy()

    def cleanup(self):
        """Clean up resources"""
        self.terminated = True
        if hasattr(self, "zmq_incoming_diagnostic_socket"):
            self.zmq_incoming_diagnostic_socket.close()
        if hasattr(self, "context"):
            self.context.term()

    def run_plotting_loop(self):
        def plotting_thread():
            LOGGER.info("Starting plotting loop")
            while not self.terminated:
                while self.zmq_incoming_diagnostic_socket.poll(timeout=10):
                    LOGGER.info(f"Main-Plotter detecting input")
                    recv_json = json.loads(
                        self.zmq_incoming_diagnostic_socket.recv_string()
                    )
                    LOGGER.info(f"Main-Plotter received data update: {recv_json}")
                    if recv_json["plot_id"] == KILL_SIGNAL:
                        LOGGER.info("Received kill signal, shutting down plotter")
                        self.terminated = True
                        return
                    elif recv_json["plot_id"] == CLIENT_STATUS_UPDATE:
                        self.service_status_plot_map[
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

                    # TODO: make this show the current model?
                    elif recv_json["plot_id"] == BANDWIDTH_ALLOCATION_UPDATE:
                        self.bandwidth_allocation_plot.update_data(
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

                    elif recv_json["plot_id"] == NETWORK_UTILIZATION_UPDATE:
                        self.service_utilization_plot_map[
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
                    self.bandwidth_allocation_plot.refresh_plot()
                    for plot in self.service_status_plot_map.values():
                        plot.refresh_plot()
                    for plot in self.service_utilization_plot_map.values():
                        plot.refresh_plot()
                except Exception as e:
                    print(
                        f"Warning: Plot refresh error (expected with web backend): {e}"
                    )

                # Always update web images (even without new data, time axis should update)
                self.update_plot_images()

                time.sleep(self.plotting_loop_sleep_seconds)

        thread = threading.Thread(target=plotting_thread, daemon=True)
        thread.start()
        return thread

    def main_loop(self):
        """Main function to start the web server"""
        global web_plotter

        # Load configuration from your existing config files
        from web_config import get_config_for_web, get_web_dashboard_config

        try:
            config = get_config_for_web()
            web_config = get_web_dashboard_config()

            print(
                f"Loaded config with ZMQ address: {config.zmq_incoming_diagnostic_name}"
            )
            print(
                f"Service IDs: {config.bandwidth_allocation_plot_config.service_id_list}"
            )
            print(f"Refresh rate: {web_config.refresh_rate_seconds} seconds")
            print(
                f"Plotting loop sleep: {web_config.plotting_loop_sleep_seconds} seconds"
            )

            web_plotter = self
            # Start the plotting loop
            self.run_plotting_loop()

            print("Starting web server on http://localhost:5000")
            print("Press Ctrl+C to stop")
            socketio.run(app, host="0.0.0.0", port=5000, debug=False)

        except KeyboardInterrupt:
            print("\nShutting down...")
            if web_plotter:
                web_plotter.cleanup()
        except Exception as e:
            print(f"Error starting web server: {e}")
            import traceback

            traceback.print_exc()
            if web_plotter:
                web_plotter.cleanup()
        finally:
            print("Web dashboard stopped")
