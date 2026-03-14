"""Per-service client for offloading image perception inference to a remote server.

Each Client instance manages one perception service (e.g., one camera feed). It:
  1. Reads raw camera frames from CameraDataStream via ZMQ + shared memory.
  2. Applies image preprocessing and/or JPEG/PNG compression based on the currently
     allocated model configuration (received from BandwidthAllocator via ZMQ).
  3. Serializes a ModelServerRequest and writes it to shared memory, then signals
     the QUIC client (Rust) via ZMQ to transmit it to the remote server.
  4. Listens for ModelServerResponse results from the QUIC client, enforcing an
     SLO timeout. Outdated responses (mismatched context_id) are drained.
  5. Logs per-request latency breakdowns and bounding box results to Parquet files
     via ClientSpillableStore, and emits diagnostic metrics to the web dashboard.

When the allocated model is the on-vehicle baseline (edd1-imgcompNone-inpcompNone),
the client skips remote offloading and logs the result as locally served.
"""

import json
import logging
from multiprocessing import shared_memory
from pathlib import Path
import pickle
import re
from threading import Lock
from typing import List
import effdet
from matplotlib import pyplot as plt
import numpy as np
from pydantic import BaseModel, field_validator
import polars as pl
import zmq
import time
from PIL import Image

from camera_stream.camera_data_stream import CameraDataRequest
from model_server.effdet_inference import compress_image, create_preprocessing_function
from server import ModelServerRequest, ModelServerResponse
from util.plotting_main import CLIENT_STATUS_UPDATE
from util.spillable_store import SpillableStore
from util.thread_pool_manager import ThreadPoolManager
from accuracy.detection.utils import PredictedBoundingBox2D
from exceptions import ConfigurationError, GracefulShutdown, NetworkError, ModelError

LOGGER = logging.getLogger("client")

# Timeout constants (all in milliseconds)
#
# ZMQ_CAMERA_TIMEOUT_MS: Maximum time to wait for camera frame metadata response.
#                        Set to 1000ms to accommodate USB camera initialization delays.
#                        If camera doesn't respond within this time, retry the request.
# ZMQ_QUIC_TIMEOUT_MS: Maximum time to wait for QUIC client ACK confirmation.
#                      Set to 1000ms to handle network congestion or QUIC processing delays.
#                      Raising RuntimeError if exceeded prevents deadlock in send path.
# QUEUE_DRAIN_POLL_MS: Poll interval when draining stale responses from QUIC receive queue.
#                      Set to 1ms for fast queue draining (~1ms overhead per stale response).
#                      Higher values reduce CPU but increase latency; lower causes busy-wait.
ZMQ_CAMERA_TIMEOUT_MS = 1000  # Timeout for camera frame requests
ZMQ_QUIC_TIMEOUT_MS = 1000    # Timeout for QUIC ACK responses
QUEUE_DRAIN_POLL_MS = 1       # Poll interval when draining stale responses

# Local model simulation constants
#
# LOCAL_MODEL_SLO_FRACTION: Fraction of SLO timeout to sleep when simulating local processing.
#                           Set to 0.5 (50%) to simulate on-vehicle EfficientDet-d1 inference
#                           time based on profiling (typically ~100ms for 200ms SLO).
#                           Adjust based on actual hardware capabilities.
LOCAL_MODEL_SLO_FRACTION = 0.5  # Simulate local model processing as 50% of SLO timeout

CLIENT_STORE_ROW_SCHEMA = {
    "service_id": pl.Int32,
    "context_id": pl.Int32,
    "start_time_unix": pl.Float64,
    "start_time": pl.Float64,
    "camera_recv_latency": pl.Float64,
    "camera_ack_latency": pl.Float64,
    "preprocessing_delay": pl.Float64,
    "request_start_time": pl.Float64,
    "request_serialization_delay": pl.Float64,
    "request_ack_latency": pl.Float64,
    "response_listen_start_time": pl.Float64,
    "good_response_listen_delay": pl.Float64,
    "good_response_deserialization_latency": pl.Float64,
    "response_listen_end_time": pl.Float64,
    "response_overall_recv_delay": pl.Float64,
    "end_time": pl.Float64,
    "total_latency": pl.Float64,
    "allocated_model": pl.String,
    "remote_request_received": pl.Boolean,
    "[CameraBoxComponent].box.center.x": pl.Float64,
    "[CameraBoxComponent].box.center.y": pl.Float64,
    "[CameraBoxComponent].box.size.x": pl.Float64,
    "[CameraBoxComponent].box.size.y": pl.Float64,
    "[CameraBoxComponent].type": pl.Int8,
    "score": pl.Float64,
}


class ClientSpillableStore(SpillableStore):
    """Spillable storage for per-client query logs with latency breakdowns and detection results."""

    def __init__(self, MAX_ENTRIES: int, service_id: int, store_dir: str):
        super().__init__(MAX_ENTRIES)

        self.SERVICE_ID = service_id
        self.STORE_DIR = Path(store_dir)

        # Validate storage directory exists
        if not self.STORE_DIR.exists() or not self.STORE_DIR.is_dir():
            raise ConfigurationError(f"Client storage directory does not exist or is not a directory: {store_dir}")

    def generate_filepath(self) -> Path:
        """Generate the parquet file path for the current file number."""
        return (
            self.STORE_DIR
            / f"client_queries_service{self.SERVICE_ID}_temp{self.fileno}.csv"
        )

    def append_record(
        self,
        context_id: int,
        start_time_unix: float,
        start_time: float,
        camera_recv_latency: float,
        camera_ack_latency: float,
        preprocessing_delay: float,
        request_start_time: float,
        request_serialization_delay: float,
        request_ack_latency: float,
        response_listen_start_time: float,
        good_response_listen_delay: float,
        good_response_deserialization_latency: float,
        response_listen_end_time: float,
        response_overall_recv_delay: float,
        end_time: float,
        total_latency: float,
        allocated_model: str,
        remote_response: np.ndarray | None,
    ) -> None:
        """Append a client query record with latency breakdowns and detection results.

        If remote_response is None, logs a local-only request with no detections.
        If remote_response contains detections, logs one row per bounding box.
        """
        if remote_response is None:
            row = {
                "service_id": self.SERVICE_ID,
                "context_id": context_id,
                "start_time_unix": start_time_unix,
                "start_time": start_time,
                "camera_recv_latency": camera_recv_latency,
                "camera_ack_latency": camera_ack_latency,
                "preprocessing_delay": preprocessing_delay,
                "request_start_time": request_start_time,
                "request_serialization_delay": request_serialization_delay,
                "request_ack_latency": request_ack_latency,
                "response_listen_start_time": response_listen_start_time,
                "good_response_listen_delay": good_response_listen_delay,
                "good_response_deserialization_latency": good_response_deserialization_latency,
                "response_listen_end_time": response_listen_end_time,
                "response_overall_recv_delay": response_overall_recv_delay,
                "end_time": end_time,
                "total_latency": total_latency,
                "allocated_model": allocated_model,
                "remote_request_received": False,
                "[CameraBoxComponent].box.center.x": None,
                "[CameraBoxComponent].box.center.y": None,
                "[CameraBoxComponent].box.size.x": None,
                "[CameraBoxComponent].box.size.y": None,
                "[CameraBoxComponent].type": None,
                "score": None,
            }
            self.row_list.append(row)
            super().append_record()
        else:
            # Validate response shape (should have batch dimension = 1)
            if remote_response.shape[0] != 1:
                raise NetworkError(
                    f"Invalid remote response shape: expected batch size 1, got {remote_response.shape[0]}"
                )
            bboxes: List[PredictedBoundingBox2D] = []
            for x_min, y_min, x_max, y_max, score, label in remote_response[0]:
                # Filter out low-confidence predictions and degenerate bounding boxes.
                if x_min < x_max and y_min < y_max:
                    bbox = PredictedBoundingBox2D(
                        x_min, x_max, y_min, y_max, label, score
                    )
                    bboxes.append(bbox)
            # If no valid bounding boxes, add a placeholder entry with None values
            if len(bboxes) == 0:
                bboxes.append(
                    PredictedBoundingBox2D(None, None, None, None, None, None)
                )

            # Log one row per bounding box detected (or one row with None if no detections)
            for bbox in bboxes:
                center_x, center_y = bbox.center
                row = {
                    "service_id": self.SERVICE_ID,
                    "context_id": context_id,
                    "start_time_unix": start_time_unix,
                    "start_time": start_time,
                    "camera_recv_latency": camera_recv_latency,
                    "camera_ack_latency": camera_ack_latency,
                    "preprocessing_delay": preprocessing_delay,
                    "request_start_time": request_start_time,
                    "request_serialization_delay": request_serialization_delay,
                    "request_ack_latency": request_ack_latency,
                    "response_listen_start_time": response_listen_start_time,
                    "good_response_listen_delay": good_response_listen_delay,
                    "good_response_deserialization_latency": good_response_deserialization_latency,
                    "response_listen_end_time": response_listen_end_time,
                    "response_overall_recv_delay": response_overall_recv_delay,
                    "end_time": end_time,
                    "total_latency": total_latency,
                    "allocated_model": allocated_model,
                    "remote_request_received": True,
                    "[CameraBoxComponent].box.center.x": center_x,
                    "[CameraBoxComponent].box.center.y": center_y,
                    "[CameraBoxComponent].box.size.x": float(bbox.width),
                    "[CameraBoxComponent].box.size.y": float(bbox.height),
                    "[CameraBoxComponent].type": bbox.label,
                    "score": bbox.score,
                }
                self.row_list.append(row)
                super().append_record()

    def write_to_disk(self) -> None:
        """Write accumulated query records to a parquet file and reset the buffer."""
        if self.currfile_size == 0:
            return

        pl.DataFrame(self.row_list, schema=CLIENT_STORE_ROW_SCHEMA).write_parquet(
            self.generate_filepath()
        )
        super().write_to_disk()


class ClientConfig(BaseModel):
    service_id: int
    client_savedir: Path
    max_entries: int
    thread_concurrency: int
    camera_bidirectional_zmq_sockname: str
    camera_stream_shmem_filename: str
    bandwidth_allocation_incoming_zmq_sockname: str
    quic_rcv_zmq_sockname: str
    quic_snd_zmq_sockname: str
    outgoing_zmq_diagnostic_sockname: str
    camera_np_size: List[int]
    model_name_imagesize_map: dict[str, List[int]]

    zmq_kill_switch_sockname: str

    quic_snd_shm_filename: str
    quic_rcv_shm_filename: str

    quic_shm_size: int
    SLO_TIMEOUT_MS: int

    @field_validator('service_id')
    @classmethod
    def validate_service_id(cls, v: int) -> int:
        if v < 0:
            raise ConfigurationError(f"service_id must be non-negative, got {v}")
        return v

    @field_validator('max_entries', 'thread_concurrency', 'quic_shm_size')
    @classmethod
    def validate_positive_ints(cls, v: int) -> int:
        if v <= 0:
            raise ConfigurationError(f"Value must be positive, got {v}")
        return v

    @field_validator('SLO_TIMEOUT_MS')
    @classmethod
    def validate_slo_timeout(cls, v: int) -> int:
        if v <= 0:
            raise ConfigurationError(f"SLO_TIMEOUT_MS must be positive, got {v}")
        if v > 10000:  # 10 seconds seems unreasonably high
            raise ConfigurationError(f"SLO_TIMEOUT_MS seems too high (>{10}s), got {v}ms")
        return v

    @field_validator('camera_np_size')
    @classmethod
    def validate_camera_np_size(cls, v: List[int]) -> List[int]:
        if len(v) != 3:
            raise ConfigurationError(f"camera_np_size must have 3 dimensions (H, W, C), got {len(v)}")
        if any(dim <= 0 for dim in v):
            raise ConfigurationError(f"All camera dimensions must be positive, got {v}")
        return v


MODEL_P = re.compile("edd([0-9x]*)-imgcomp([A-Za-z0-9]*)-inpcomp([A-Za-z0-9]*)")


class Client:
    """Per-service client for offloading image perception inference to a remote server.

    Each Client instance manages one perception service (camera feed), handling:
    - Camera frame acquisition and preprocessing
    - Dynamic model configuration based on bandwidth allocation
    - QUIC-based remote inference with SLO timeout enforcement
    - Local fallback processing for baseline model (edd1)
    - Detailed latency logging to Parquet files
    """

    def __init__(self, config: ClientConfig, ready_queue=None) -> None:
        self.service_id = config.service_id
        self._is_cleaned_up = False
        self.spillable_store = ClientSpillableStore(
            config.max_entries, config.service_id, config.client_savedir
        )
        self.thread_pool_manager = ThreadPoolManager(config.thread_concurrency)

        self.context = zmq.Context(io_threads=4)
        self.camera_bidirectional_zmq_socket = self.context.socket(zmq.REQ)
        self.camera_bidirectional_zmq_socket.connect(
            config.camera_bidirectional_zmq_sockname
        )

        self.bandwidth_allocation_incoming_zmq_socket = self.context.socket(zmq.SUB)
        self.bandwidth_allocation_incoming_zmq_socket.setsockopt_string(
            zmq.SUBSCRIBE, ""
        )

        self.bandwidth_allocation_incoming_zmq_socket.bind(
            config.bandwidth_allocation_incoming_zmq_sockname
        )

        self.camera_stream_shmem = shared_memory.SharedMemory(
            name=config.camera_stream_shmem_filename, create=False
        )
        self.camera_stream_arr = np.ndarray(
            config.camera_np_size, dtype=np.uint8, buffer=self.camera_stream_shmem.buf
        )

        # Python↔Rust ZMQ startup ordering (must follow this sequence):
        #   1. Python creates SHM files and binds on incoming ZMQ socket (REP)
        #   2. Rust starts, connects to Python's incoming socket, binds its own outgoing socket
        #   3. Rust sends "hello" to Python's incoming socket to signal readiness
        #   4. Python receives "hello", then connects to Rust's outgoing socket
        # This ensures both sides are ready before any data flows.

        self.quic_rcv_zmq_socket = self.context.socket(zmq.REP)
        self.quic_rcv_zmq_socket.bind(config.quic_rcv_zmq_sockname)

        if ready_queue is not None:
            ready_queue.put(self.service_id)

        # Create kill switch early so we can check it during the handshake wait
        self.quic_kill_switch = self.context.socket(zmq.SUB)
        self.quic_kill_switch.setsockopt_string(zmq.SUBSCRIBE, "")
        self.quic_kill_switch.bind(config.zmq_kill_switch_sockname)

        LOGGER.info("Client %d: Python waiting for Rust QUIC client handshake", self.service_id)

        while True:
            if self.quic_rcv_zmq_socket.poll(timeout=1000):
                self.quic_rcv_zmq_socket.recv_string()
                self.quic_rcv_zmq_socket.send_string("hello")
                break
            if self.quic_kill_switch.poll(timeout=0):
                LOGGER.info("Client %d: Kill signal received during handshake wait, exiting", self.service_id)
                self.cleanup()
                raise GracefulShutdown(f"Client {self.service_id}: Kill signal received during handshake")

        self.quic_snd_zmq_socket = self.context.socket(zmq.REQ)
        self.quic_snd_zmq_socket.connect(config.quic_snd_zmq_sockname)

        LOGGER.info("Client %d: QUIC handshake complete", self.service_id)

        self.diagnostic_outgoing_zmq_socket = self.context.socket(zmq.PUB)
        self.diagnostic_outgoing_zmq_socket.connect(
            config.outgoing_zmq_diagnostic_sockname
        )

        self.is_terminated = False

        self.current_model = "edd1-imgcompNone-inpcompNone"
        self.context_id_ctr = 0
        self.SLO_TIMEOUT_MS = config.SLO_TIMEOUT_MS

        self.quic_snd_shm = shared_memory.SharedMemory(
            name=config.quic_snd_shm_filename, create=True, size=config.quic_shm_size
        )
        self.quic_rcv_shm = shared_memory.SharedMemory(
            name=config.quic_rcv_shm_filename, create=True, size=config.quic_shm_size
        )

        self.preprocess_fn_map = {
            model_name: create_preprocessing_function(image_size, raw=False)
            for (model_name, image_size) in config.model_name_imagesize_map.items()
        }

        self.preprocess_fn_map_raw = {
            model_name: create_preprocessing_function(image_size, raw=True)
            for (model_name, image_size) in config.model_name_imagesize_map.items()
        }

    def cleanup(self) -> None:
        """Clean up all resources including ZMQ sockets and shared memory.

        This method should be called during shutdown to ensure proper resource cleanup.
        Flushes any buffered data to Parquet, closes all ZMQ sockets, terminates the
        ZMQ context, and releases shared memory.
        Guards each resource with hasattr to handle partially-initialized objects.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        LOGGER.info("Client %d: Starting cleanup of resources", self.service_id)

        # Flush buffered data to disk before closing any resources
        if hasattr(self, 'spillable_store'):
            try:
                self.spillable_store.write_to_disk()
            except Exception as e:
                LOGGER.error("Client %d: Error flushing spillable store: %s", self.service_id, e)

        # Close all ZMQ sockets individually so partial init doesn't skip remaining cleanup
        for attr in (
            'camera_bidirectional_zmq_socket',
            'bandwidth_allocation_incoming_zmq_socket',
            'quic_rcv_zmq_socket',
            'quic_snd_zmq_socket',
            'diagnostic_outgoing_zmq_socket',
            'quic_kill_switch',
        ):
            try:
                if hasattr(self, attr):
                    getattr(self, attr).close(linger=0)
            except Exception as e:
                LOGGER.error("Client %d: Error closing %s: %s", self.service_id, attr, e)

        # Terminate ZMQ context
        if hasattr(self, 'context'):
            try:
                self.context.term()
            except Exception as e:
                LOGGER.error("Client %d: Error terminating ZMQ context: %s", self.service_id, e)

        # Close shared memory (camera stream - opened with create=False, so just close)
        if hasattr(self, 'camera_stream_shmem'):
            try:
                self.camera_stream_shmem.close()
            except Exception as e:
                LOGGER.error("Client %d: Error closing camera shared memory: %s", self.service_id, e)

        # Close and unlink QUIC shared memory (created by this process with create=True)
        if hasattr(self, 'quic_snd_shm'):
            try:
                self.quic_snd_shm.close()
                self.quic_snd_shm.unlink()
            except Exception as e:
                LOGGER.error("Client %d: Error cleaning up QUIC send shared memory: %s", self.service_id, e)

        if hasattr(self, 'quic_rcv_shm'):
            try:
                self.quic_rcv_shm.close()
                self.quic_rcv_shm.unlink()
            except Exception as e:
                LOGGER.error("Client %d: Error cleaning up QUIC receive shared memory: %s", self.service_id, e)

        LOGGER.info("Client %d: Resource cleanup complete", self.service_id)

    def __del__(self) -> None:
        """Destructor to ensure cleanup is called even if not explicitly invoked."""
        if hasattr(self, 'service_id'):
            try:
                self.cleanup()
            except Exception as e:
                # Use print instead of LOGGER since logging may already be shut down
                print(f"Client {self.service_id}: Error during __del__ cleanup: {e}")

    def main_loop(self) -> None:
        """Main processing loop for the client.

        Each iteration represents one perception query cycle:
        1. Check for kill signal or bandwidth allocation update (new model config)
        2. Parse model config string → determines preprocessing pipeline + base model
        3. Request + receive camera frame from CameraDataStream (ZMQ+SHM handshake)
        4. Branch on model config:
           a. LOCAL path (edd1): simulate on-vehicle inference, drain stale QUIC responses
           b. REMOTE path: preprocess image → serialize to SHM → signal QUIC client →
              listen for response with SLO timeout, dropping stale context_id mismatches
        5. Log latency breakdown + detection results to Parquet, emit diagnostics
        """
        LOGGER.info(f"Client Main {self.service_id} is ready!")
        while True:
            if self.thread_pool_manager.check_due():
                self.thread_pool_manager.check_pending()

            if self.quic_kill_switch.poll(timeout=1):
                self.quic_kill_switch.recv()

                self.is_terminated = True

                self.camera_bidirectional_zmq_socket.send_pyobj(0)
                LOGGER.info(
                    f"terminating client main loop for camera {self.service_id}"
                )
                self.diagnostic_outgoing_zmq_socket.send_string(
                    json.dumps({"plot_id": -1})
                )
                self.thread_pool_manager.await_all()
                self.cleanup()
                # Note: Bandwidth allocator and plotter are terminated by client_main.py
                # via their respective kill switches, not by individual clients
                return

            # Refresh model configuration from bandwidth allocator module
            if self.bandwidth_allocation_incoming_zmq_socket.poll(timeout=1):
                LOGGER.info(
                    f"Client {self.service_id} received incoming bandwidth allocation message"
                )
                bw_json = json.loads(
                    self.bandwidth_allocation_incoming_zmq_socket.recv_string()
                )
                LOGGER.info(
                    f"Client {self.service_id} has parsed incoming message: {bw_json}"
                )

                self.current_model = bw_json["model_config_map"][str(self.service_id)]

                if self.current_model == "ed1-on-vehicle":
                    self.current_model = "edd1-imgcompNone-inpcompNone"

                LOGGER.info(
                    f"Client {self.service_id} has set current model to {self.current_model}"
                )

            # Parse model config string, e.g. "edd4-imgcomp50-inpcompNone"
            # → model_num="4", model_imgcomp="50", model_inpcomp="None"
            # → base_model="tf_efficientdet_d4", enable_image_processing=True, enable_compression=True
            currmodel = self.current_model
            match = MODEL_P.match(self.current_model)
            if not match:
                LOGGER.error(
                    "Invalid model configuration string: %s. Expected format: eddX-imgcompY-inpcompZ",
                    self.current_model
                )
                raise ModelError(f"Invalid model configuration: {self.current_model}")

            model_num, model_imgcomp, model_inpcomp = match.groups()

            # Determine preprocessing pipeline from model configuration 
            enable_image_processing = model_imgcomp != "None"
            enable_input_processing = model_inpcomp != "None"

            # If both are "None", default to image processing (uncompressed)
            if not enable_input_processing and not enable_image_processing:
                enable_image_processing = True

            # Compression is enabled if either pipeline specifies a compression level
            enable_compression = (
                enable_image_processing and model_imgcomp != "None"
            ) or (enable_input_processing and model_inpcomp != "None")

            base_model = f"tf_efficientdet_d{model_num}"

            LOGGER.debug(
                "Parsed model config for service %d: model=%s, base=%s, img_proc=%s, inp_proc=%s, compress=%s",
                self.service_id,
                currmodel,
                base_model,
                enable_image_processing,
                enable_input_processing,
                enable_compression
            )

            # ========== TIMING STRUCTURE ==========
            # All times are in seconds (using perf_counter for precision)
            # start_time_unix: Unix timestamp for absolute time reference
            # start_time: Relative perf_counter timestamp (base for all latencies)
            #   camera_recv_latency: Time to receive camera metadata
            #   camera_ack_latency: Time for camera ACK handshake
            #   preprocessing_delay: Image preprocessing + compression time
            #   request_start_time: Relative timestamp when QUIC request begins
            #     request_serialization_delay: Pickle + SHM write time
            #     request_ack_latency: QUIC ACK wait time
            #     response_listen_start_time: When we start listening for response
            #       good_response_listen_delay: Time until matching response arrives
            #       good_response_deserialization_latency: Unpickle time
            #     response_listen_end_time: When response processing completes
            #   response_overall_recv_delay: Total from request_start to response received
            # end_time: Final perf_counter timestamp
            # total_latency: end_time - start_time
            # ======================================

            start_time_unix = time.time()
            start_time = time.perf_counter()

            # Request latest camera frame from CameraDataStream 
            stream_request = CameraDataRequest(
                is_ack_request=False, context_id=self.context_id_ctr
            )
            self.camera_bidirectional_zmq_socket.send_pyobj(stream_request)

            # Wait for camera frame metadata (with retry loop) 
            while not self.camera_bidirectional_zmq_socket.poll(timeout=ZMQ_CAMERA_TIMEOUT_MS):
                LOGGER.warning(
                    "Client %d waiting for camera frame response (context_id=%d)",
                    self.service_id,
                    self.context_id_ctr
                )

                if self.quic_kill_switch.poll(timeout=1):
                    self.quic_kill_switch.recv()

                    self.is_terminated = True

                    self.camera_bidirectional_zmq_socket.send_pyobj(0)
                    LOGGER.info(
                        f"terminating client main loop for camera {self.service_id}"
                    )
                    self.diagnostic_outgoing_zmq_socket.send_string(
                        json.dumps({"plot_id": -1})
                    )
                    self.thread_pool_manager.await_all()
                    self.cleanup()
                    # Note: Bandwidth allocator and plotter are terminated by client_main.py
                    # via their respective kill switches, not by individual clients
                    return

            # Get response from camera stream
            stream_response = self.camera_bidirectional_zmq_socket.recv_pyobj()
            camera_recv_latency = time.perf_counter() - start_time
            LOGGER.debug("Camera frame metadata received for service %d (latency=%.3fms)",
                        self.service_id, camera_recv_latency * 1000)

            # Send ACK to confirm frame has been read from shared memory
            self.camera_bidirectional_zmq_socket.send_pyobj(
                CameraDataRequest(is_ack_request=True, context_id=self.context_id_ctr)
            )

            # Wait for ACK-ACK confirmation
            while not self.camera_bidirectional_zmq_socket.poll(timeout=ZMQ_CAMERA_TIMEOUT_MS):
                LOGGER.warning(
                    "Client %d waiting for camera ACK-ACK (context_id=%d)",
                    self.service_id,
                    self.context_id_ctr
                )

            self.camera_bidirectional_zmq_socket.recv()
            camera_ack_latency = time.perf_counter() - start_time - camera_recv_latency
            LOGGER.debug("Camera ACK handshake complete for service %d (ack_latency=%.3fms)",
                        self.service_id, camera_ack_latency * 1000)

            camera_ack_latency = time.perf_counter() - start_time - camera_recv_latency

            # ========== LOCAL MODEL PATH (edd1 - on-vehicle baseline) ========== 
            if self.current_model == "edd1-imgcompNone-inpcompNone":
                LOGGER.info(
                    "Client %d using local on-vehicle model (edd1). No remote offloading.",
                    self.service_id
                )
                this_image_pil = Image.fromarray(self.camera_stream_arr)
                remote_request_made = False

                # Calculate preprocessing delay (minimal for local model)
                preprocessing_delay = (
                    time.perf_counter()
                    - start_time
                    - camera_recv_latency
                    - camera_ack_latency
                )

                # Set dummy timing values for consistency in logging schema
                request_start_time = time.perf_counter()
                request_serialization_delay = 0.0
                request_ack_latency = 0.0

                # Sleep for a fraction of the SLO timeout to simulate local processing delay
                time.sleep(self.SLO_TIMEOUT_MS * LOCAL_MODEL_SLO_FRACTION / 1000)

                # Drain any stale responses from QUIC receive queue 
                # This prevents the QUIC client from blocking on unprocessed responses 
                # Queue draining is fast (~1ms per stale response) since we only send ACK 
                # TODO(optimization): Send image_context in ZMQ message header to skip
                # SHM read + unpickling for outdated responses (can check context_id before deserializing)
                response_listen_start_time = time.perf_counter()
                good_response_listen_delay = None
                good_response_deserialization_delay = None

                drained_count = 0
                while self.quic_rcv_zmq_socket.poll(timeout=QUEUE_DRAIN_POLL_MS):
                    stale_response_len = int(self.quic_rcv_zmq_socket.recv_string(0))
                    stale_response = pickle.loads(
                        self.quic_rcv_shm.buf[:stale_response_len]
                    )
                    self.quic_rcv_zmq_socket.send_string("ACK")

                    if not isinstance(stale_response, ModelServerResponse):
                        LOGGER.error(
                            "Received unexpected response type during queue drain: %s",
                            type(stale_response)
                        )
                        continue

                    drained_count += 1
                    LOGGER.debug(
                        "Drained stale response from queue (service=%d, stale_context_id=%d)",
                        self.service_id,
                        stale_response.context_id
                    )

                if drained_count > 0:
                    LOGGER.info(
                        "Queue drain complete for service %d: drained %d stale responses",
                        self.service_id,
                        drained_count
                    )

                response_listen_end_time = time.perf_counter()
                response_overall_recv_delay = time.perf_counter() - request_start_time

                # Set response to None to indicate local-only processing (no remote result)
                response = None
            # ========== REMOTE MODEL PATH (offload to server) ========== 
            else:
                LOGGER.info(
                    "Client %d using remote model %s - will offload to server",
                    self.service_id,
                    self.current_model
                )
                # NOTE: Variable name is this_image_pil but may become np.ndarray or torch.Tensor
                # after preprocessing, depending on the pipeline configuration
                this_image_pil = Image.fromarray(self.camera_stream_arr)

                LOGGER.debug(
                    "Raw image loaded for service %d: size=%s (PIL), shape=%s (np)",
                    self.service_id,
                    this_image_pil.size,
                    np.array(this_image_pil).shape
                )

                remote_request_made = True

                # PSEUDOCODE — Client-side preprocessing pipeline:
                #   4 possible paths based on (image_proc, input_proc, compress):
                #     1. image_proc + compress:    compress raw image (JPEG/PNG) → server decompresses + resizes + normalizes
                #     2. image_proc + no compress: send raw PIL image → server resizes + normalizes
                #     3. input_proc + no compress: client resizes + normalizes → send torch.Tensor directly
                #     4. input_proc + compress:    client resizes + normalizes → transpose (C,H,W)→(H,W,C) → compress →
                #                                  server decompresses → transpose back → reconstruct tensor
                #   Tradeoff: image_proc = more network bytes, less client CPU;
                #             input_proc = fewer bytes, more client CPU
                if enable_image_processing:
                    if enable_compression:
                        # Compress raw image (JPEG or PNG)
                        this_image_pil = compress_image(
                            this_image_pil,
                            int(model_imgcomp) if model_imgcomp != "PNG" else None,
                        )
                        LOGGER.debug(
                            "Image-level compression complete: compressed_size=%d bytes",
                            this_image_pil.nbytes
                        )

                elif enable_input_processing:
                    if not enable_compression:
                        # Resize + normalize to model input size (returns torch.Tensor) 
                        this_image_pil = self.preprocess_fn_map[base_model](
                            this_image_pil
                        )
                        LOGGER.debug(
                            "Input preprocessing complete (uncompressed): tensor_shape=%s",
                            this_image_pil.shape
                        )

                    elif enable_compression:
                        # Resize + normalize using raw=True (returns np.ndarray) 
                        this_image_pil = self.preprocess_fn_map_raw[base_model](
                            this_image_pil
                        )
                        LOGGER.debug(
                            "Raw input preprocessing complete: array_shape=%s",
                            this_image_pil.shape
                        )

                        # Transpose from (C, H, W) to (H, W, C) for image encoding
                        # This converts the channels-first tensor format to channels-last image format
                        this_image_pil = np.transpose(
                            this_image_pil, axes=[1, 2, 0]
                        )
                        LOGGER.debug(
                            "Transposed to channels-last: array_shape=%s",
                            this_image_pil.shape
                        )

                        # Compress the preprocessed input 
                        this_image_pil = compress_image(
                            this_image_pil,
                            int(model_inpcomp) if model_inpcomp != "PNG" else None,
                        )
                        LOGGER.debug(
                            "Input-level compression complete: compressed_size=%d bytes",
                            this_image_pil.nbytes
                        )

                preprocessing_delay = (
                    time.perf_counter()
                    - start_time
                    - camera_recv_latency
                    - camera_ack_latency
                )

                # Expected data types after preprocessing:
                # - Image proc + compress: np.ndarray (compressed bytes)
                # - Image proc + no compress: PIL.Image
                # - Input proc + compress: np.ndarray (compressed bytes)
                # - Input proc + no compress: torch.Tensor
                LOGGER.info(
                    "Sending to server (service=%d): type=%s, shape=%s, model=%s, preproc_time=%.3fms",
                    self.service_id,
                    type(this_image_pil).__name__,
                    this_image_pil.shape if hasattr(this_image_pil, 'shape') else this_image_pil.size,
                    currmodel,
                    preprocessing_delay * 1000
                )

                # Serialize request and write to SHM for QUIC client (Rust).
                # SHM write + ZMQ signal is the Python→Rust IPC pattern used throughout:
                #   Python writes pickled payload to SHM → sends payload length via ZMQ →
                #   Rust reads length, copies payload from SHM, sends ACK via ZMQ
                request_start_time = time.perf_counter()

                request = ModelServerRequest(
                    context_id=self.context_id_ctr,
                    base_model=base_model,
                    enable_image_processing=enable_image_processing,
                    enable_input_processing=enable_input_processing,
                    enable_compression=enable_compression,
                    input_image=this_image_pil,
                    requested_processing=self.current_model,
                )

                request_pickle = pickle.dumps(request)
                self.quic_snd_shm.buf[: len(request_pickle)] = request_pickle

                request_serialization_delay = time.perf_counter() - request_start_time

                LOGGER.info(
                    "Client %d serialized request: context_id=%d, payload_size=%d bytes, serial_time=%.3fms",
                    self.service_id,
                    self.context_id_ctr,
                    len(request_pickle),
                    request_serialization_delay * 1000
                )

                # Signal QUIC client to transmit the request (send context_id and length via ZMQ) 
                self.quic_snd_zmq_socket.send_multipart(
                    [
                        str.encode(str(self.context_id_ctr)),
                        str.encode(str(len(request_pickle))),
                    ]
                )

                # Wait for ACK from QUIC client confirming it has read the request from SHM 
                if not self.quic_snd_zmq_socket.poll(timeout=ZMQ_QUIC_TIMEOUT_MS):
                    LOGGER.error(
                        "Timeout waiting for QUIC ACK for service %d context_id %d",
                        self.service_id,
                        self.context_id_ctr
                    )
                    raise NetworkError(
                        f"Failed to receive ACK from QUIC client for service {self.service_id}"
                    )

                ack = self.quic_snd_zmq_socket.recv()
                request_ack_latency = (
                    time.perf_counter()
                    - request_start_time
                    - request_serialization_delay
                )

                LOGGER.debug(
                    "QUIC ACK received for service %d context_id %d: ack=%s, ack_latency=%.3fms",
                    self.service_id,
                    self.context_id_ctr,
                    ack,
                    request_ack_latency * 1000
                )

                response = None

                # PSEUDOCODE — Response listening with SLO timeout + queue draining:
                #   while elapsed < SLO_TIMEOUT:
                #     poll QUIC receive socket with remaining_timeout
                #     if response arrives:
                #       deserialize from SHM, send ACK to QUIC client
                #       if response.context_id != current context_id:
                #         discard (stale response from previous cycle), keep polling
                #       else:
                #         valid response found, break
                #   if no valid response by SLO deadline → response = None (timeout)
                #
                # Queue draining overhead is ~1ms per stale item (SHM read + unpickle + ACK).
                # TODO: Optimize by sending context_id in ZMQ message header to skip
                # SHM read + unpickling for outdated responses
                response_listen_start_time = time.perf_counter()

                good_response_listen_delay = None
                good_response_deserialization_delay = None
                stale_response_count = 0

                LOGGER.debug(
                    "Client %d starting response listen loop: context_id=%d, SLO_timeout=%dms",
                    self.service_id,
                    self.context_id_ctr,
                    self.SLO_TIMEOUT_MS
                )

                while True:
                    # Check if SLO timeout has been exceeded 
                    elapsed_since_camera_ms = (
                        time.perf_counter()
                        - start_time
                        - camera_ack_latency
                        - camera_recv_latency
                    ) * 1000

                    if elapsed_since_camera_ms > self.SLO_TIMEOUT_MS:
                        LOGGER.warning(
                            "SLO timeout exceeded for service %d context_id %d: elapsed=%.1fms > timeout=%dms",
                            self.service_id,
                            self.context_id_ctr,
                            elapsed_since_camera_ms,
                            self.SLO_TIMEOUT_MS
                        )
                        break

                    # Calculate remaining time for this polling attempt
                    remaining_timeout_ms = self.SLO_TIMEOUT_MS - elapsed_since_camera_ms

                    if not self.quic_rcv_zmq_socket.poll(timeout=remaining_timeout_ms):
                        LOGGER.warning(
                            "Response poll timeout for service %d context_id %d: no response within %dms",
                            self.service_id,
                            self.context_id_ctr,
                            self.SLO_TIMEOUT_MS
                        )
                        break

                    good_response_listen_delay = (
                        time.perf_counter() - response_listen_start_time
                    )

                    # Receive response length from QUIC client 
                    response_len = int(self.quic_rcv_zmq_socket.recv_string(0))
                    LOGGER.debug(
                        "Client %d received response signal from QUIC: length=%d bytes",
                        self.service_id,
                        response_len
                    )

                    # Deserialize response from shared memory 
                    response = pickle.loads(self.quic_rcv_shm.buf[:response_len])

                    good_response_deserialization_delay = (
                        time.perf_counter()
                        - response_listen_start_time
                        - good_response_listen_delay
                    )

                    # Send ACK to QUIC client to confirm response has been read from SHM 
                    self.quic_rcv_zmq_socket.send_string("ACK")

                    LOGGER.debug(
                        "Client %d sent ACK to QUIC for response (deser_time=%.3fms)",
                        self.service_id,
                        good_response_deserialization_delay * 1000
                    )

                    # Validate response type
                    if not isinstance(response, ModelServerResponse):
                        LOGGER.error(
                            "Received invalid response type for service %d: expected ModelServerResponse, got %s",
                            self.service_id,
                            type(response).__name__
                        )
                        response = None
                        continue

                    LOGGER.debug(
                        "Client %d received response with context_id=%d (expected=%d)",
                        self.service_id,
                        response.context_id,
                        self.context_id_ctr
                    )

                    # Check if response matches current context_id (drop stale responses) 
                    if response.context_id != self.context_id_ctr:
                        stale_response_count += 1
                        LOGGER.info(
                            "Dropping stale response for service %d: response_context_id=%d != expected=%d",
                            self.service_id,
                            response.context_id,
                            self.context_id_ctr
                        )
                        response = None
                        continue  # Keep trying to listen until we get a relevant response, or timeout 

                    # Got a valid response matching our context_id; break to process it 
                    LOGGER.info(
                        "Client %d received valid response: context_id=%d, listen_delay=%.3fms, deser_delay=%.3fms, stale_dropped=%d",
                        self.service_id,
                        self.context_id_ctr,
                        good_response_listen_delay * 1000,
                        good_response_deserialization_delay * 1000,
                        stale_response_count
                    )
                    break

                # If no valid response received, reset timing metrics
                if response is None:
                    LOGGER.warning(
                        "No valid response received for service %d context_id %d: dropped %d stale responses",
                        self.service_id,
                        self.context_id_ctr,
                        stale_response_count
                    )
                    good_response_listen_delay = None
                    good_response_deserialization_delay = None

                response_listen_end_time = time.perf_counter()

                response_overall_recv_delay = time.perf_counter() - request_start_time

            end_time = time.perf_counter()

            # Submit query result to spillable store (async write to avoid blocking main loop)
            self.thread_pool_manager.submit(
                self.spillable_store.append_record,
                self.context_id_ctr,
                start_time_unix,
                start_time,
                camera_recv_latency,
                camera_ack_latency,
                preprocessing_delay,
                request_start_time,
                request_serialization_delay,
                request_ack_latency,
                response_listen_start_time,
                good_response_listen_delay,
                good_response_deserialization_delay,
                response_listen_end_time,
                response_overall_recv_delay,
                end_time,
                end_time - start_time,
                self.current_model,  # NOTE: This is actually thread-safe because you only update current_model during the main loop, not as a coroutine
                None if response is None else response.response,
            )

            # Send diagnostic update to web dashboard plotter
            diagnostic_msg = json.dumps(
                {
                    "plot_id": CLIENT_STATUS_UPDATE,
                    "timestamp": time.time(),
                    "service_id": self.service_id,
                    "remote_request_made": remote_request_made,
                    "remote_request_successful": response is not None,
                }
            )
            self.diagnostic_outgoing_zmq_socket.send_string(diagnostic_msg)

            LOGGER.debug(
                "Client %d sent diagnostic update: remote_request=%s, successful=%s, context_id=%d",
                self.service_id,
                remote_request_made,
                response is not None,
                self.context_id_ctr
            )

            # Log complete request lifecycle summary
            total_latency = end_time - start_time
            LOGGER.info(
                "Client %d completed request cycle: context_id=%d, model=%s, total_latency=%.1fms, success=%s",
                self.service_id,
                self.context_id_ctr,
                self.current_model,
                total_latency * 1000,
                response is not None
            )

            self.context_id_ctr += 1
