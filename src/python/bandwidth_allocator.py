"""Bandwidth allocation service that optimally distributes available bandwidth across
multiple image perception services.

The BandwidthAllocator runs as a client-side process that:
  1. On startup, loads pre-computed utility curve data from Parquet files and builds a
     GlobalScene with a GlobalStaticUtilityCurvePolicy and an LP-based allocator.
  2. In its main loop, receives available bandwidth estimates from the QUIC client via
     ZMQ, and queries the PingHandler for the current RTT.
  3. Calls the GlobalScene's LP solver to compute the optimal per-service bandwidth
     allocation and model configuration (e.g., which EfficientDet variant and what
     image/input compression level) that maximizes total expected detection utility
     subject to the bandwidth and SLO constraints.
  4. Broadcasts the allocation result to:
     - The QUIC client (to enforce per-service bandwidth limits on the transport layer)
     - Each Client process (to adjust image preprocessing/compression accordingly)
     - The web dashboard (for real-time visualization of allocation decisions)
"""

import json
import logging
from pydantic import BaseModel, field_validator
import zmq

import time
from util.plotting_main import BANDWIDTH_ALLOCATION_UPDATE
from utility_curve_stream.allocators.lp_allocator import LPAllocator
from utility_curve_stream.global_scene import GlobalScene
from utility_curve_stream.utility_curve_policies.global_static_utility_curve_policy import (
    GlobalStaticUtilityCurvePolicy,
)
import polars as pl

from utility_curve_stream.utility_curve_utils import read_parquet_data
from exceptions import ConfigurationError

# Default timestamp for utility curve evaluation (Unix timestamp in microseconds)
# This is a fixed timestamp from the Waymo Open Dataset used for consistent evaluation.
# Corresponds to a representative frame from the training data with diverse objects.
# Value: April 2, 2018 20:40:14.970187 UTC
DEFAULT_TIMESTAMP = 1522688014970187

# Local utility baseline parameters for eval_utilities_and_allocations_timestamp()
# These represent the baseline (on-vehicle only) performance for comparison against
# remote offloading. The bandwidth is 0 to simulate no network offloading.
#
# LOCAL_UTILITY_BANDWIDTH_MBPS: Zero bandwidth simulates all processing on-vehicle
# LOCAL_UTILITY_TIMESTAMP: Index 169 in the utility curve corresponds to a representative
#                          frame from training data with typical detection workload
# LOCAL_UTILITY_RTT_MS: Baseline RTT of 200ms represents typical 4G LTE latency
#                       (used only for utility curve lookup, not actual networking)
# LOCAL_UTILITY_SLO_MS: Baseline SLO of 200ms is the maximum tolerable latency for
#                       perception tasks in autonomous driving (from literature)
LOCAL_UTILITY_BANDWIDTH_MBPS = 0    # Zero bandwidth = local-only processing
LOCAL_UTILITY_TIMESTAMP = 169       # Timestamp index for utility curve lookup
LOCAL_UTILITY_RTT_MS = 200          # Baseline RTT assumption in milliseconds
LOCAL_UTILITY_SLO_MS = 200          # Baseline SLO timeout in milliseconds

LOGGER = logging.getLogger("bandwidth_allocator")


class BandwidthAllocatorConfig(BaseModel):
    service_id_list: list[int]
    t_SLO: float
    parquet_eval_dir: str
    model_info_csv_path: str
    outgoing_zmq_diagnostic_sockname: str
    outgoing_zmq_client_socknames: list[str]
    bidirectional_zmq_quic_sockname: str
    zmq_kill_switch_sockname: str
    bidirectional_zmq_ping_handler_sockname: str

    @field_validator('t_SLO')
    @classmethod
    def validate_slo(cls, v: float) -> float:
        if v <= 0:
            raise ConfigurationError(f"t_SLO must be positive, got {v}")
        if v > 10000:  # 10 seconds seems unreasonably high
            raise ConfigurationError(f"t_SLO seems too high (>10s), got {v}ms")
        return v

    @field_validator('service_id_list')
    @classmethod
    def validate_service_ids(cls, v: list[int]) -> list[int]:
        if not v:
            raise ConfigurationError("service_id_list cannot be empty")
        if any(sid < 0 for sid in v):
            raise ConfigurationError(f"All service IDs must be non-negative, got {v}")
        if len(v) != len(set(v)):
            raise ConfigurationError(f"service_id_list contains duplicates: {v}")
        return v

    @field_validator('outgoing_zmq_client_socknames')
    @classmethod
    def validate_client_socknames(cls, v: list[str]) -> list[str]:
        if not v:
            raise ConfigurationError("outgoing_zmq_client_socknames cannot be empty")
        return v


class BandwidthAllocator:
    """LP-based bandwidth allocator for optimizing per-service model selection.

    Manages:
    - Loading utility curve data from Parquet files
    - Receiving bandwidth availability updates from QUIC client
    - Querying RTT measurements from PingHandler
    - Solving LP optimization to maximize total expected utility
    - Broadcasting allocation decisions to all clients and QUIC layer
    """

    def __init__(self, config: BandwidthAllocatorConfig) -> None:
        self._is_cleaned_up = False

        utility_df = read_parquet_data(
            config.parquet_eval_dir, config.model_info_csv_path, window_size=1
        )

        LOGGER.info("here is sample of utility df:")
        LOGGER.info(utility_df.glimpse())
        LOGGER.info("here are all models in the utility df:")
        LOGGER.info(set(utility_df["Model"]))
        group_counts = utility_df.group_by(pl.col("Model")).count()
        LOGGER.info("here are all the group counts in utility_df, grouped by model")
        LOGGER.info(group_counts)

        LOGGER.info("All services in utility dataframe:")
        LOGGER.info(set(utility_df["camera_id"]))
        LOGGER.info(
            "Filtering dataframe to configured services: %s",
            config.service_id_list
        )
        utility_df = utility_df.filter(
            pl.col("camera_id").is_in(list(config.service_id_list))
        )

        # Validate that all configured services are present in utility data
        utility_services = set(list(utility_df["camera_id"]) + [None])
        config_services = set(list(config.service_id_list) + [None])
        if utility_services != config_services:
            raise ConfigurationError(
                f"Service ID mismatch: utility data has {utility_services}, "
                f"config specifies {config_services}"
            )

        self.global_scene = GlobalScene(
            utility_df,
            true_curve_policy=GlobalStaticUtilityCurvePolicy(),
            baseline_allocator=LPAllocator(),
        )

        # Compute local-only (on-vehicle) utility baseline for comparison
        # Uses zero bandwidth to simulate all processing done locally on the vehicle
        # TODO: sometimes this throws 'unknown' for some camera results if the camera gets filtered out; check if this breaks in the future
        self.local_utility = self.global_scene.eval_utilities_and_allocations_timestamp(
            LOCAL_UTILITY_BANDWIDTH_MBPS,
            LOCAL_UTILITY_TIMESTAMP,
            LOCAL_UTILITY_RTT_MS,
            LOCAL_UTILITY_SLO_MS
        )[
            1
        ]  # Extract expected utility from tuple return value (index 1)
        self.t_SLO = config.t_SLO

        self.context = zmq.Context()
        self.outgoing_zmq_diagnostic_socket = self.context.socket(zmq.PUB)
        self.outgoing_zmq_diagnostic_socket.connect(
            config.outgoing_zmq_diagnostic_sockname
        )
        self.outgoing_zmq_client_socket_list = []
        for client_sockname in config.outgoing_zmq_client_socknames:
            client_sock = self.context.socket(zmq.PUB)
            client_sock.connect(client_sockname)
            self.outgoing_zmq_client_socket_list.append(client_sock)
        self.bidirectional_zmq_quic_socket = self.context.socket(zmq.REP)
        self.bidirectional_zmq_quic_socket.bind(config.bidirectional_zmq_quic_sockname)

        self.bidirectional_zmq_ping_handler_socket = self.context.socket(zmq.REQ)
        self.bidirectional_zmq_ping_handler_socket.connect(
            config.bidirectional_zmq_ping_handler_sockname
        )

        self.kill_switch = self.context.socket(zmq.SUB)
        self.kill_switch.setsockopt_string(zmq.SUBSCRIBE, "")
        self.kill_switch.bind(config.zmq_kill_switch_sockname)

        self.is_terminated = False

    def cleanup(self) -> None:
        """Clean up all resources including ZMQ sockets and context.

        This method should be called during shutdown to ensure proper resource cleanup.
        Closes all ZMQ sockets (including the list of client sockets) and terminates the ZMQ context.
        Guards each resource with hasattr to handle partially-initialized objects.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        LOGGER.info("BandwidthAllocator: Starting cleanup of resources")

        for attr in (
            'outgoing_zmq_diagnostic_socket',
            'bidirectional_zmq_quic_socket',
            'bidirectional_zmq_ping_handler_socket',
            'kill_switch',
        ):
            try:
                if hasattr(self, attr):
                    getattr(self, attr).close(linger=0)
            except Exception as e:
                LOGGER.error("BandwidthAllocator: Error closing %s: %s", attr, e)

        if hasattr(self, 'outgoing_zmq_client_socket_list'):
            for client_sock in self.outgoing_zmq_client_socket_list:
                try:
                    client_sock.close(linger=0)
                except Exception as e:
                    LOGGER.error("BandwidthAllocator: Error closing client socket: %s", e)

        if hasattr(self, 'context'):
            try:
                self.context.term()
            except Exception as e:
                LOGGER.error("BandwidthAllocator: Error terminating ZMQ context: %s", e)

        LOGGER.info("BandwidthAllocator: Resource cleanup complete")

    def __del__(self) -> None:
        """Destructor to ensure cleanup is called even if not explicitly invoked."""
        try:
            self.cleanup()
        except Exception as e:
            # Use print instead of LOGGER since logging may already be shut down
            print(f"BandwidthAllocator: Error during __del__ cleanup: {e}")

    def main_loop(self) -> None:
        """Main loop for bandwidth allocation.

        On each bandwidth update from the QUIC client:
        1. Query PingHandler for current RTT (with retry)
        2. Run LP solver: maximize total expected detection utility across all services,
           subject to total bandwidth constraint and per-service SLO timeout.
           The solver selects, for each service, the best (EfficientDet variant, compression level)
           pair from pre-computed utility curves.
        3. Broadcast the resulting per-service model configs to:
           - QUIC client (enforces per-service bandwidth rate limits at transport layer)
           - Each Client process (adjusts preprocessing/compression pipeline accordingly)
           - Web dashboard (real-time visualization)
        """
        LOGGER.info("BandwidthAllocator main loop starting")

        while True:

            recv_str = None

            # Poll for bandwidth update from QUIC client 
            if self.bidirectional_zmq_quic_socket.poll(timeout=250):
                recv_str = self.bidirectional_zmq_quic_socket.recv_string()

            # Check for termination signal
            if self.kill_switch.poll(timeout=1):
                LOGGER.info("BandwidthAllocator received termination signal - shutting down")
                # Signal PingHandler to terminate (-1 sentinel value)
                self.bidirectional_zmq_ping_handler_socket.send_pyobj(-1)
                self.is_terminated = True
                self.cleanup()
                LOGGER.info("BandwidthAllocator shutdown complete")
                return

            if recv_str is None:
                continue

            # Get bandwidth measure from QUIC handler
            recv_obj = json.loads(recv_str)
            LOGGER.info(
                "BandwidthAllocator received bandwidth update: available_bw=%.2f Mbps",
                recv_obj.get("bw", 0.0)
            )

            # Request current RTT from PingHandler 
            self.bidirectional_zmq_ping_handler_socket.send_pyobj(1)
            rtt = None
            ping_attempts = 0
            max_ping_attempts = 3

            while rtt is None and ping_attempts < max_ping_attempts:
                if self.bidirectional_zmq_ping_handler_socket.poll(timeout=1000):
                    rtt = self.bidirectional_zmq_ping_handler_socket.recv_pyobj()
                    if rtt is None:
                        ping_attempts += 1
                        LOGGER.error(
                            "PingHandler returned None (attempt %d/%d)",
                            ping_attempts,
                            max_ping_attempts
                        )
                else:
                    ping_attempts += 1
                    LOGGER.error(
                        "PingHandler timeout (attempt %d/%d)",
                        ping_attempts,
                        max_ping_attempts
                    )

            # Validate RTT is a valid numeric value
            if not isinstance(rtt, (int, float)):
                LOGGER.error(
                    "Invalid RTT value from PingHandler: expected int or float, got %s (value=%s)",
                    type(rtt).__name__,
                    rtt
                )
                continue

            recv_obj["rtt"] = rtt

            LOGGER.debug(
                "Running LP solver with: available_bw=%.2f Mbps, rtt=%.1f ms, SLO=%.1f ms",
                recv_obj["bw"],
                recv_obj["rtt"],
                self.t_SLO
            )

            # Compute optimal allocation using LP solver 
            # Returns: (utility without offloading, expected utility with offloading, allocations) 
            _, expected_utility, allocs = (
                self.global_scene.eval_utilities_and_allocations_timestamp(
                    recv_obj["bw"], DEFAULT_TIMESTAMP, recv_obj["rtt"], self.t_SLO
                )
            )

            LOGGER.info(
                "LP solver result: expected_utility=%.3f, allocations=%s",
                expected_utility,
                allocs
            )

            # Extract model configurations and bandwidth allocations per service 
            allocated_model_config = {
                int(scenario): model_config_name
                for (scenario, (_, model_config_name, _)) in allocs.items()
            }
            allocated_bws = {
                int(scenario): model_bw
                for (scenario, (model_bw, _, _)) in allocs.items()
            }

            LOGGER.debug(
                "Per-service allocations: models=%s, bandwidths=%s",
                allocated_model_config,
                allocated_bws
            )

            # Construct allocation update message for broadcast 
            outgoing_resp = json.dumps(
                {
                    "plot_id": BANDWIDTH_ALLOCATION_UPDATE,
                    "timestamp": time.time(),
                    "local_utility": self.local_utility,
                    "expected_utility": expected_utility,
                    "model_config_map": allocated_model_config,
                    "allocation_map": allocated_bws,
                    "available_bw": recv_obj["bw"],
                    "rtt": recv_obj["rtt"],
                }
            )

            # Broadcast allocation to QUIC client (for rate limiting), Client processes 
            # (for preprocessing configuration), and web dashboard (for visualization) 
            self.bidirectional_zmq_quic_socket.send_string(outgoing_resp)
            for outgoing_client_sock in self.outgoing_zmq_client_socket_list:
                outgoing_client_sock.send_string(outgoing_resp)
            self.outgoing_zmq_diagnostic_socket.send_string(outgoing_resp)

            LOGGER.info(
                "Allocation broadcast complete: utility_gain=%.1f%%, num_clients=%d",
                ((expected_utility - self.local_utility) / self.local_utility * 100) if self.local_utility > 0 else 0,
                len(self.outgoing_zmq_client_socket_list)
            )
