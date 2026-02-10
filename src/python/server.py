"""Per-service model server for running EfficientDet inference on the remote cloud.

Each ModelServer instance handles one perception service. It:
  1. Creates POSIX shared memory buffers and ZMQ sockets, then handshakes with the
     QUIC server (Rust) which connects to those sockets.
  2. Loads all configured EfficientDet model variants (d2, d4, d6, d7x) onto a
     specified GPU via EfficientDetProfiler.
  3. In its main loop, receives ModelServerRequest objects from the QUIC server via
     shared memory (the QUIC server writes the pickled request to SHM and sends its
     length over ZMQ). It drains the queue to always process the most recent frame.
  4. Depending on the request's flags, performs server-side decompression and/or
     preprocessing, then runs model inference on the appropriate EfficientDet variant.
  5. Serializes the detection results as a ModelServerResponse, writes it to outgoing
     shared memory, and signals the QUIC server via ZMQ.
  6. Logs per-request latency breakdowns (deserialization, preprocessing, inference,
     serialization, ACK) to Parquet files via ModelServerSpillableStore.

Also defines ModelServerRequest and ModelServerResponse, the serializable message
types exchanged between the client and server through the QUIC transport layer.
"""

from multiprocessing import Lock, shared_memory
from pathlib import Path
import pickle
import time
from typing import List, Optional
import effdet
from pydantic import BaseModel
from PIL import Image
import numpy as np
import polars as pl

import logging

import torch
import zmq

from model_server.effdet_inference import (
    EfficientDetProfiler,
    ModelMetadata,
    create_preprocessing_function,
    decompress_image,
)
from util.spillable_store import SpillableStore
from util.thread_pool_manager import ThreadPoolManager
from exceptions import ConfigurationError, GracefulShutdown

LOGGER = logging.getLogger("model_server")


class ModelServerConfig(BaseModel):
    service_id: int
    server_log_savedir: Path
    max_entries: int
    model_metadata_list: List[ModelMetadata]
    device: str
    incoming_zmq_sockname: str
    incoming_shm_filename: str
    outgoing_zmq_sockname: str
    outgoing_shm_filename: str
    thread_concurrency: int
    shm_filesize: int
    zmq_kill_switch_sockname: str
    mock_inference_output_path: Optional[str] = None


MODEL_SERVER_STORE_SCHEMA = {
    "service_id": pl.String,
    "context_id": pl.String,
    "timestamp_secs": pl.Float64,
    "deserialization_latency": pl.Float64,
    "deserialization_polling_latency": pl.Float64,
    "preprocessing_latency": pl.Float64,
    "inference_latency": pl.Float64,
    "serialization_latency": pl.Float64,
    "ack_latency": pl.Float64,
    "overall_response_latency_without_ack": pl.Float64,
    "start_time_counter": pl.Float64,
    "end_time_counter": pl.Float64,
    "requested_processing": pl.String,
}


class ModelServerRequest:
    def __init__(
        self,
        context_id: int,
        base_model: str,
        input_image: np.ndarray,
        enable_image_processing: bool,
        enable_input_processing: bool,
        enable_compression: bool,
        requested_processing: str,
    ) -> None:
        self.context_id = context_id
        self.base_model = base_model
        self.input_image = input_image
        self.enable_image_processing = enable_image_processing
        self.enable_input_processing = enable_input_processing
        self.enable_compression = enable_compression
        self.requested_processing = requested_processing


class ModelServerResponse:
    def __init__(self, context_id: int, response: np.ndarray) -> None:
        self.context_id = context_id
        self.response = response


class ModelServerSpillableStore(SpillableStore):
    def __init__(self, MAX_ENTRIES: int, service_id: int, store_dir: Path):
        super().__init__(MAX_ENTRIES)

        self.SERVICE_ID = service_id
        self.STORE_DIR = store_dir

        # Validate storage directory exists
        if not store_dir.exists() or not store_dir.is_dir():
            raise ConfigurationError(
                f"ModelServer storage directory does not exist or is not a directory: {store_dir}"
            )

    def generate_filepath(self) -> Path:
        """Generate the parquet file path for the current file number."""
        return (
            self.STORE_DIR
            / f"server_results_service{self.SERVICE_ID}_temp{self.fileno}"
        )

    def append_record(
        self,
        service_id: int,
        context_id: str,
        timestamp_secs: int,
        deserialization_latency: float,
        deserialization_polling_latency: float,
        preprocessing_latency: float,
        inference_latency: float,
        serialization_latency: float,
        ack_latency: float,
        overall_response_latency_without_ack: float,
        start_time_counter: float,
        end_time_counter: float,
        requested_processing: str,
    ) -> None:
        """Append a model server inference record with detailed latency breakdowns."""
        with self.lock:
            row = {
                "service_id": service_id,
                "context_id": context_id,
                "timestamp_secs": timestamp_secs,
                "deserialization_latency": deserialization_latency,
                "deserialization_polling_latency": deserialization_polling_latency,
                "preprocessing_latency": preprocessing_latency,
                "inference_latency": inference_latency,
                "serialization_latency": serialization_latency,
                "ack_latency": ack_latency,
                "overall_response_latency_without_ack": overall_response_latency_without_ack,
                "start_time_counter": start_time_counter,
                "end_time_counter": end_time_counter,
                "requested_processing": requested_processing,
            }
            self.row_list.append(row)

            super().append_record()

    def write_to_disk(self) -> None:
        """Write accumulated inference records to a parquet file and reset the buffer.

        This will be called as a final cleanup, or while append_record's lock is acquired.
        When doing final cleanup, make sure that any append processes are killed.
        """
        if self.currfile_size == 0:
            return

        pl.DataFrame(self.row_list, schema=MODEL_SERVER_STORE_SCHEMA).write_parquet(
            self.generate_filepath()
        )

        super().write_to_disk()


def create_torch_model_map(
    config: ModelServerConfig, device: str
) -> dict[str, EfficientDetProfiler]:
    """Load all configured EfficientDet model variants onto the specified GPU device.

    Returns a dictionary mapping base model names (e.g., 'tf_efficientdet_d4')
    to EfficientDetProfiler instances ready for inference.
    """
    model_map = {}
    for model_metadata in config.model_metadata_list:
        profiler = EfficientDetProfiler(
            model_metadata.checkpoint_path,
            model_metadata.base_model,
            device,
            model_metadata.num_classes,
        )
        model_map[model_metadata.base_model] = profiler

    return model_map


class ModelServer:
    """Per-service model server for running EfficientDet inference on GPU.

    Each ModelServer handles one perception service, managing:
    - Model loading and GPU memory management
    - Request deserialization from QUIC server via shared memory
    - Image decompression and preprocessing
    - Model inference with multiple EfficientDet variants
    - Response serialization and latency logging
    """

    def __init__(self, config: ModelServerConfig) -> None:
        self.service_id = config.service_id
        self._is_cleaned_up = False
        self.spillable_store = ModelServerSpillableStore(
            config.max_entries, config.service_id, config.server_log_savedir
        )

        self.thread_pool_manager = ThreadPoolManager(config.thread_concurrency)

        self.incoming_shm_file = shared_memory.SharedMemory(
            create=True, name=config.incoming_shm_filename, size=config.shm_filesize
        )
        self.outgoing_shm_file = shared_memory.SharedMemory(
            create=True, name=config.outgoing_shm_filename, size=config.shm_filesize
        )

        self.context = zmq.Context()

        # order must be:
        # clear the ZMQ directory
        # you start FIRST, create SHM file, and bind on this incoming socket
        # rust starts AFTER you are done; you wait for rust to connect to this incoming socket, and for it to bind on this outgoing socket
        # rust sends message that its bind on this outgoing socket is ready
        # you connect to this outgoing socket

        self.incoming_zmq_socket = self.context.socket(zmq.REP)
        self.incoming_zmq_socket.bind(config.incoming_zmq_sockname)

        # Create kill switch early so we can check it during the handshake wait
        self.zmq_kill_switch = self.context.socket(zmq.SUB)
        self.zmq_kill_switch.setsockopt_string(zmq.SUBSCRIBE, "")
        self.zmq_kill_switch.bind(config.zmq_kill_switch_sockname)

        LOGGER.info(
            "ModelServer %d: Python waiting for Rust QUIC server handshake",
            self.service_id
        )

        while True:
            if self.incoming_zmq_socket.poll(timeout=1000):
                self.incoming_zmq_socket.recv_string()
                self.incoming_zmq_socket.send_string("hello")
                break
            if self.zmq_kill_switch.poll(timeout=0):
                LOGGER.info("ModelServer %d: Kill signal received during handshake wait, exiting", self.service_id)
                self.cleanup()
                raise GracefulShutdown(f"ModelServer {self.service_id}: Kill signal received during handshake")

        self.outgoing_zmq_socket = self.context.socket(zmq.REQ)
        self.outgoing_zmq_socket.connect(config.outgoing_zmq_sockname)

        LOGGER.info("ModelServer %d: QUIC handshake complete", self.service_id)

        self.mock_mode = config.mock_inference_output_path is not None

        if self.mock_mode:
            mock_path = Path(config.mock_inference_output_path)
            if not mock_path.exists():
                raise ConfigurationError(f"Mock inference output file not found: {mock_path}")
            self.mock_result = np.fromfile(str(mock_path), dtype=np.float32)

            # TODO: use actual model benchmarks to tune this magic number.
            mock_sleep_time = .50 # sleep for 50 ms
            time.sleep(mock_sleep_time)

            LOGGER.info("ModelServer %d: Mock mode enabled, loaded mock output from %s (shape=%s)",
                        self.service_id, mock_path, self.mock_result.shape)
        else:
            self.torch_model_map = create_torch_model_map(config, config.device)
            self.preprocess_fn_map = {
                model_name: create_preprocessing_function(image_size)
                for (model_name, image_size) in [
                    (metadata.base_model, metadata.image_size)
                    for metadata in config.model_metadata_list
                ]
            }

        self.is_terminated = False

    def cleanup(self) -> None:
        """Clean up all resources including ZMQ sockets, shared memory, and GPU memory.

        This method should be called during shutdown to ensure proper resource cleanup.
        Flushes any buffered data to Parquet, closes all ZMQ sockets, terminates the
        ZMQ context, and releases shared memory.
        Guards each resource with hasattr to handle partially-initialized objects.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        LOGGER.info("ModelServer %d: Starting cleanup of resources", self.service_id)

        # Flush buffered data to disk before closing any resources
        if hasattr(self, 'spillable_store'):
            try:
                self.spillable_store.write_to_disk()
            except Exception as e:
                LOGGER.error("ModelServer %d: Error flushing spillable store: %s", self.service_id, e)

        # Close all ZMQ sockets individually so partial init doesn't skip remaining cleanup
        for attr in ('incoming_zmq_socket', 'outgoing_zmq_socket', 'zmq_kill_switch'):
            try:
                if hasattr(self, attr):
                    getattr(self, attr).close(linger=0)
            except Exception as e:
                LOGGER.error("ModelServer %d: Error closing %s: %s", self.service_id, attr, e)

        if hasattr(self, 'context'):
            try:
                self.context.term()
            except Exception as e:
                LOGGER.error("ModelServer %d: Error terminating ZMQ context: %s", self.service_id, e)

        # Close and unlink shared memory (created by this process with create=True)
        if hasattr(self, 'incoming_shm_file'):
            try:
                self.incoming_shm_file.close()
                self.incoming_shm_file.unlink()
            except Exception as e:
                LOGGER.error("ModelServer %d: Error cleaning up incoming shared memory: %s", self.service_id, e)

        if hasattr(self, 'outgoing_shm_file'):
            try:
                self.outgoing_shm_file.close()
                self.outgoing_shm_file.unlink()
            except Exception as e:
                LOGGER.error("ModelServer %d: Error cleaning up outgoing shared memory: %s", self.service_id, e)

        # GPU memory is automatically reclaimed by the CUDA driver when the process exits.

        LOGGER.info("ModelServer %d: Resource cleanup complete", self.service_id)

    def __del__(self) -> None:
        """Destructor to ensure cleanup is called even if not explicitly invoked."""
        if hasattr(self, 'service_id'):
            try:
                self.cleanup()
            except Exception as e:
                # Use print instead of LOGGER since logging may already be shut down
                print(f"ModelServer {self.service_id}: Error during __del__ cleanup: {e}")

    def main_loop(self) -> None:
        """Main processing loop for the model server.

        Continuously:
        1. Polls for incoming inference requests from QUIC server
        2. Drains queue to process only the freshest frame
        3. Performs server-side preprocessing if needed
        4. Runs model inference on GPU
        5. Serializes and returns detection results
        6. Handles graceful shutdown via kill switch
        """
        LOGGER.info("ModelServer %d main loop starting", self.service_id)

        while True:
            if self.thread_pool_manager.check_due():
                self.thread_pool_manager.check_pending()

            if self.zmq_kill_switch.poll(timeout=1):
                LOGGER.info(
                    "ModelServer %d received termination signal - initiating graceful shutdown",
                    self.service_id
                )
                self.is_terminated = True
                self.thread_pool_manager.await_all()
                self.cleanup()
                LOGGER.info("ModelServer %d shutdown complete", self.service_id)
                return

            recv_obj = None

            # Queue draining loop: processes all pending requests, keeping only the most recent
            # Each queue item requires SHM read + unpickling + ACK send (~1ms overhead per item)
            # This ensures we always process the freshest frame, dropping stale requests
            # TODO: Optimize by sending context_id in ZMQ message header to skip SHM read +
            # unpickling for outdated requests (check context_id before deserializing)
            # TODO: Tune polling timeout based on expected request frequency
            while self.incoming_zmq_socket.poll(timeout=1):
                start_time_unix = time.time()
                start_time = time.perf_counter()
                recv_objlen = int(self.incoming_zmq_socket.recv_string())
                LOGGER.debug(
                    "ModelServer %d received request signal: payload_size=%d bytes",
                    self.service_id,
                    recv_objlen
                )

                # Deserialize request from shared memory
                recv_obj = pickle.loads(self.incoming_shm_file.buf[:recv_objlen])
                deserialization_latency = time.perf_counter() - start_time

                # Send ACK to QUIC server confirming request has been read from SHM
                self.incoming_zmq_socket.send_string("ACK")

                LOGGER.debug(
                    "ModelServer %d deserialized request: deser_time=%.3fms",
                    self.service_id,
                    deserialization_latency * 1000
                )

            if recv_obj is None:
                continue

            # Validate request type
            if not isinstance(recv_obj, ModelServerRequest):
                LOGGER.error(
                    "ModelServer %d received invalid request type: expected ModelServerRequest, got %s",
                    self.service_id,
                    type(recv_obj).__name__
                )
                continue

            deserialization_polling_latency = time.perf_counter() - start_time

            LOGGER.info(
                "ModelServer %d processing request: context_id=%d, base_model=%s, "
                "img_proc=%s, inp_proc=%s, compress=%s, requested=%s",
                self.service_id,
                recv_obj.context_id,
                recv_obj.base_model,
                recv_obj.enable_image_processing,
                recv_obj.enable_input_processing,
                recv_obj.enable_compression,
                recv_obj.requested_processing
            )

            if self.mock_mode:
                preprocessing_latency = 0.0
                inference_latency = 0.0
                result_numpy = self.mock_result
            else:
                this_image = recv_obj.input_image

                # Expected input types based on preprocessing pipeline:
                # - Image proc + compress: np.ndarray (compressed bytes)
                # - Image proc + no compress: PIL.Image
                # - Input proc + compress: np.ndarray (compressed bytes)
                # - Input proc + no compress: torch.Tensor
                image_shape = (
                    recv_obj.input_image.shape
                    if not (recv_obj.enable_image_processing and not recv_obj.enable_compression)
                    else recv_obj.input_image.size
                )
                LOGGER.debug(
                    "ModelServer %d received input: type=%s, shape=%s",
                    self.service_id,
                    type(recv_obj.input_image).__name__,
                    image_shape
                )

                # Apply server-side preprocessing based on client's pipeline configuration
                if recv_obj.enable_image_processing:
                    if recv_obj.enable_compression:
                        this_image = decompress_image(this_image)
                        LOGGER.debug(
                            "Image decompressed: shape=%s (PIL)", this_image.size
                        )

                    # Resize + normalize to model input size (returns torch.Tensor)
                    this_image = self.preprocess_fn_map[recv_obj.base_model](this_image)
                    LOGGER.debug(
                        "Image preprocessing complete: shape=%s (tensor)", this_image.shape
                    )

                elif recv_obj.enable_input_processing:
                    if not recv_obj.enable_compression:
                        # Client already did all preprocessing - no server-side work needed
                        LOGGER.debug(
                            "Input preprocessing: no server-side work (client sent preprocessed tensor)"
                        )
                    else:
                        # Client sent compressed preprocessed input - decompress and reconstruct tensor
                        this_image = decompress_image(this_image)
                        LOGGER.debug(
                            "Input decompressed: shape=%s (PIL)", this_image.size
                        )

                        # Convert PIL Image to numpy array
                        this_image = np.array(this_image)
                        LOGGER.debug(
                            "Converted to numpy: shape=%s", this_image.shape
                        )

                        # Transpose from channels-last (H, W, C) to channels-first (C, H, W)
                        # This reverses the transpose done in client.py before compression
                        this_image = np.transpose(this_image, axes=[2, 0, 1])

                        # Convert to torch.Tensor and add batch dimension
                        this_image = torch.from_numpy(this_image).unsqueeze_(0)
                        LOGGER.debug(
                            "Reconstructed tensor: shape=%s (after transpose + unsqueeze)",
                            this_image.shape
                        )

                preprocessing_latency = (
                    time.perf_counter() - start_time - deserialization_polling_latency
                )

                this_image = self.torch_model_map[recv_obj.base_model].to_device(this_image)

                result = self.torch_model_map[recv_obj.base_model].predict(this_image)

                inference_latency = (
                    time.perf_counter()
                    - start_time
                    - preprocessing_latency
                    - deserialization_polling_latency
                )

                result_numpy = result.detach().cpu().numpy()

            resp_pickle = pickle.dumps(
                ModelServerResponse(
                    context_id=recv_obj.context_id,
                    response=result_numpy,
                )
            )
            self.outgoing_shm_file.buf[: len(resp_pickle)] = resp_pickle

            self.outgoing_zmq_socket.send_multipart(
                [
                    str.encode(str(recv_obj.context_id)),
                    str.encode(str(len(resp_pickle))),
                ]
            )

            serialization_latency = (
                time.perf_counter()
                - start_time
                - preprocessing_latency
                - deserialization_polling_latency
                - inference_latency
            )

            while True:
                if self.outgoing_zmq_socket.poll(timeout=1000):
                    self.outgoing_zmq_socket.recv()
                    break
                if self.zmq_kill_switch.poll(timeout=0):
                    LOGGER.info("ModelServer %d: Kill signal received during ACK wait, shutting down", self.service_id)
                    self.is_terminated = True
                    self.thread_pool_manager.await_all()
                    self.cleanup()
                    return

            ack_latency = (
                time.perf_counter()
                - start_time
                - preprocessing_latency
                - deserialization_polling_latency
                - inference_latency
                - serialization_latency
            )

            end_time = time.perf_counter()

            # Log complete request processing summary
            total_latency = end_time - start_time
            LOGGER.info(
                "ModelServer %d completed request: context_id=%d, total_latency=%.1fms "
                "(deser=%.1fms, preproc=%.1fms, inference=%.1fms, serial=%.1fms, ack=%.1fms)",
                self.service_id,
                recv_obj.context_id,
                total_latency * 1000,
                deserialization_polling_latency * 1000,
                preprocessing_latency * 1000,
                inference_latency * 1000,
                serialization_latency * 1000,
                ack_latency * 1000
            )

            # Submit result to spillable store (async write to avoid blocking main loop)
            self.thread_pool_manager.submit(
                self.spillable_store.append_record,
                self.service_id,
                recv_obj.context_id,
                start_time_unix,
                deserialization_latency,
                deserialization_polling_latency,
                preprocessing_latency,
                inference_latency,
                serialization_latency,
                ack_latency,
                end_time - start_time - ack_latency,
                start_time,
                end_time,
                recv_obj.requested_processing,
            )
