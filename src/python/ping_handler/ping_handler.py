"""ICMP ping-based RTT measurement service for monitoring network latency to the
remote server.

PingHandler periodically pings the destination server IP (every 250ms) and maintains
the latest RTT measurement. The BandwidthAllocator queries this service via a ZMQ
REQ/REP socket to obtain the current RTT, which is used as an input to the bandwidth
allocation LP solver. RTT measurements are logged to Parquet files via
PingSpillableStore for post-experiment analysis.
"""

import logging
from multiprocessing import Lock
from pathlib import Path
import polars as pl
import time
from pydantic import BaseModel, field_validator
import zmq
from ping3 import ping

from util.spillable_store import SpillableStore
from util.thread_pool_manager import ThreadPoolManager
from exceptions import ConfigurationError

LOGGER = logging.getLogger("ping_handler")

# Default fallback RTT used when ping fails (in seconds)
# Set to 1.0s (1000ms) as a conservative estimate representing poor network conditions.
# This ensures bandwidth allocation continues gracefully even during ping failures,
# though with reduced accuracy. Lower values risk over-allocation; higher values are
# too pessimistic for typical network conditions.
DEFAULT_FALLBACK_RTT_SECONDS = 1.0


class PingHandlerConfig(BaseModel):
    dst_ip: str
    ping_savedir: str
    max_entries: int
    thread_concurrency: int
    bidirectional_zmq_sockname: str
    zmq_kill_switch_sockname: str

    @field_validator('max_entries', 'thread_concurrency')
    @classmethod
    def validate_positive_ints(cls, v: int) -> int:
        if v <= 0:
            raise ConfigurationError(f"Value must be positive, got {v}")
        return v

    @field_validator('dst_ip')
    @classmethod
    def validate_dst_ip(cls, v: str) -> str:
        if not v or v.isspace():
            raise ConfigurationError("dst_ip cannot be empty")
        return v


PING_STORE_ROW_SCHEMA = {
    "spawn_timestamp_secs": pl.Float64,
    "ping_duration": pl.Float64,
}


class PingSpillableStore(SpillableStore):
    def __init__(self, MAX_ENTRIES: int, store_dir: str):
        super().__init__(MAX_ENTRIES)

        self.STORE_DIR = Path(store_dir)

        # Validate storage directory exists
        if not self.STORE_DIR.exists() or not self.STORE_DIR.is_dir():
            raise ConfigurationError(
                f"PingHandler storage directory does not exist or is not a directory: {store_dir}"
            )

    def generate_filepath(self) -> Path:
        """Generate the parquet file path for the current file number."""
        return self.STORE_DIR / f"ping_loop_allservices_temp{self.fileno}.csv"

    def append_record(self, spawn_timestamp_secs: float, ping_duration: float) -> None:
        """Append a ping measurement record with timestamp and RTT duration."""
        with self.lock:
            row = {
                "spawn_timestamp_secs": spawn_timestamp_secs,
                "ping_duration": ping_duration,
            }
            self.row_list.append(row)

            super().append_record()

    def write_to_disk(self) -> None:
        """Write accumulated ping records to a parquet file and reset the buffer.

        This will be called as a final cleanup, or while append_record's lock is acquired.
        When doing final cleanup, make sure that any append processes are killed.
        """
        if self.currfile_size == 0:
            return

        pl.DataFrame(self.row_list, schema=PING_STORE_ROW_SCHEMA).write_parquet(
            self.generate_filepath()
        )
        super().write_to_disk()


class PingHandler:
    """ICMP ping-based RTT measurement service.

    Manages:
    - Periodic ICMP pings to destination server (every 250ms)
    - RTT measurement with fallback on failure (1.0s default)
    - ZMQ REQ/REP protocol for serving RTT queries
    - Logging ping results to Parquet files
    """

    def __init__(self, ping_handler_config: PingHandlerConfig) -> None:
        self._is_cleaned_up = False

        self.spillable_store = PingSpillableStore(
            ping_handler_config.max_entries, Path(ping_handler_config.ping_savedir)
        )
        self.thread_pool_manager = ThreadPoolManager(
            ping_handler_config.thread_concurrency
        )

        self.context = zmq.Context()
        self.bidirectional_zmq_socket = self.context.socket(zmq.REP)
        self.bidirectional_zmq_socket.bind(
            ping_handler_config.bidirectional_zmq_sockname
        )

        self.curr_rtt = DEFAULT_FALLBACK_RTT_SECONDS  # Initialize with fallback RTT
        self.curr_rtt_query_time = time.time()

        self.kill_switch = self.context.socket(zmq.SUB)
        self.kill_switch.setsockopt_string(zmq.SUBSCRIBE, "")
        self.kill_switch.bind(ping_handler_config.zmq_kill_switch_sockname)

        self.is_terminated = False

        self.dst_ip = ping_handler_config.dst_ip

        LOGGER.info("PingHandler initialized: dst_ip=%s, kill_switch=%s",
                     self.dst_ip, ping_handler_config.zmq_kill_switch_sockname)

    def cleanup(self) -> None:
        """Clean up all resources including ZMQ socket and context.

        This method should be called during shutdown to ensure proper resource cleanup.
        Flushes any buffered data to Parquet, closes the ZMQ socket, and terminates
        the ZMQ context.
        Guards each resource with hasattr to handle partially-initialized objects.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        LOGGER.info("PingHandler: Starting cleanup of resources")

        if hasattr(self, 'spillable_store'):
            try:
                self.spillable_store.write_to_disk()
            except Exception as e:
                LOGGER.error("PingHandler: Error flushing spillable store: %s", e)

        for attr in ('bidirectional_zmq_socket', 'kill_switch'):
            try:
                if hasattr(self, attr):
                    getattr(self, attr).close(linger=0)
            except Exception as e:
                LOGGER.error("PingHandler: Error closing %s: %s", attr, e)

        if hasattr(self, 'context'):
            try:
                self.context.term()
            except Exception as e:
                LOGGER.error("PingHandler: Error terminating ZMQ context: %s", e)

        LOGGER.info("PingHandler: Resource cleanup complete")

    def __del__(self) -> None:
        """Destructor to ensure cleanup is called even if not explicitly invoked."""
        try:
            self.cleanup()
        except Exception as e:
            # Use print instead of LOGGER since logging may already be shut down
            print(f"PingHandler: Error during __del__ cleanup: {e}")

    def refresh_loop(self) -> None:
        """Background loop that periodically pings the destination server and updates RTT.

        Runs every 250ms. On ping failure (timeout or unreachable), falls back to 1.0s RTT.
        This conservative fallback ensures bandwidth allocation continues even when pings fail.
        """
        ping_failure_count = 0
        ping_success_count = 0

        LOGGER.info("PingHandler refresh loop starting: dst_ip=%s, interval=250ms", self.dst_ip)

        while True:
            if self.is_terminated:
                LOGGER.info(
                    "PingHandler refresh loop terminating: success=%d, failures=%d",
                    ping_success_count,
                    ping_failure_count
                )
                return

            start_time = time.time()
            ping_res = ping(self.dst_ip, timeout=1)

            # Handle ping failures (None = timeout/unreachable, False = error)
            if ping_res is None or ping_res is False:
                ping_failure_count += 1
                self.curr_rtt = DEFAULT_FALLBACK_RTT_SECONDS  # Use conservative fallback
                if ping_failure_count % 5 == 1:  # Log every 5th failure to avoid spam
                    LOGGER.warning(
                        "Ping failed to %s (failure count: %d): result=%s. Using fallback RTT=%.1fs",
                        self.dst_ip,
                        ping_failure_count,
                        ping_res,
                        DEFAULT_FALLBACK_RTT_SECONDS
                    )
            else:
                ping_success_count += 1
                self.curr_rtt = ping_res
                LOGGER.debug(
                    "Ping successful to %s: RTT=%.3f ms (%.6f s)",
                    self.dst_ip,
                    ping_res * 1000,
                    ping_res
                )

            self.curr_rtt_query_time = start_time

            # Log RTT measurement to Parquet
            self.spillable_store.append_record(
                self.curr_rtt_query_time,
                self.curr_rtt,
            )

            time.sleep(0.250)

    def main_loop(self) -> None:
        """Main loop that responds to RTT queries from BandwidthAllocator.

        Protocol:
        - Receives integer requests via ZMQ REQ/REP socket
        - Request value 1: return current RTT
        - Request value -1: terminate gracefully
        - Invalid requests are logged and ignored
        """
        # Start background ping refresh loop in thread pool
        self.thread_pool_manager.submit(PingHandler.refresh_loop, self)

        LOGGER.info("PingHandler main loop ready - listening for RTT requests")
        request_count = 0

        while True:
            if self.thread_pool_manager.check_due():
                self.thread_pool_manager.check_pending()

            if self.kill_switch.poll(timeout=1):
                self.kill_switch.recv()
                LOGGER.info(
                    "PingHandler received kill switch signal - shutting down (served %d requests)",
                    request_count
                )
                self.is_terminated = True
                self.thread_pool_manager.await_all()
                self.cleanup()
                LOGGER.info("PingHandler shutdown complete")
                return

            if not self.bidirectional_zmq_socket.poll(timeout=100):
                continue

            recv_obj = self.bidirectional_zmq_socket.recv_pyobj()
            request_count += 1

            # Validate request is an integer
            if not isinstance(recv_obj, int):
                LOGGER.error(
                    "PingHandler received invalid request type: expected int, got %s (value=%s)",
                    type(recv_obj).__name__,
                    recv_obj
                )
                # Send None to indicate error (BandwidthAllocator will retry)
                self.bidirectional_zmq_socket.send_pyobj(None)
                continue

            # Handle termination request (-1 sentinel value)
            if recv_obj == -1:
                LOGGER.info(
                    "PingHandler received termination signal - shutting down (served %d requests)",
                    request_count
                )
                self.is_terminated = True
                self.thread_pool_manager.await_all()
                self.cleanup()
                LOGGER.info("PingHandler shutdown complete")
                return

            # Return current RTT measurement
            LOGGER.debug(
                "PingHandler serving RTT request %d: RTT=%.3f ms",
                request_count,
                self.curr_rtt * 1000
            )
            self.bidirectional_zmq_socket.send_pyobj(self.curr_rtt)
