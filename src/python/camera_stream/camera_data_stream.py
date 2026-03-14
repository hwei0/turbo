"""Camera data streaming service that captures USB webcam frames and serves them to
Client processes via ZMQ + shared memory.

CameraDataStream captures video frames from a USB camera (via OpenCV VideoCapture),
continuously refreshes the latest frame in POSIX shared memory, and responds to ZMQ
REP/REQ requests from Client instances. The request/response protocol includes a
handshake (ACK) step to ensure the client has copied the frame from shared memory
before the next frame overwrites it.

Also provides CameraSpillableStore for logging camera metadata to Parquet files, and
CameraDataRequest/CameraDataResponse Pydantic models for the ZMQ message protocol.
"""

from concurrent.futures import ThreadPoolExecutor
import logging
from multiprocessing import Lock, shared_memory
import time
from typing import Optional
import cv2
import effdet
import numpy as np
from pydantic import BaseModel
from pathlib import Path
import polars as pl
import torch
import zmq
from PIL import Image

from model_server.effdet_inference import compress_image
from util.spillable_store import SpillableStore
from util.thread_pool_manager import ThreadPoolManager
import functools
from exceptions import ConfigurationError, ResourceError, NetworkError

LOGGER = logging.getLogger("camera_data_stream")


class CameraDataStreamConfig(BaseModel):
    usb_id: int
    camera_id: int
    camera_savedir: str
    max_entries: int
    thread_concurrency: int
    bidirectional_zmq_sockname: str
    camera_stream_shmem_filename: str
    camera_np_size: list[int]
    shmem_buf_size: int = 50 * 1e6
    zmq_kill_switch_sockname: str
    mock_camera_image_path: Optional[str] = None


class CameraDataRequest(BaseModel):
    """Request message from Client to CameraDataStream for frame data.

    Fields:
        is_ack_request: True if this is the ACK handshake after frame copy, False for initial request
        context_id: Frame sequence number for tracking purposes
    """
    is_ack_request: bool
    context_id: int


class CameraDataResponse(BaseModel):
    is_ack_response: bool
    context_id: int


CAMERA_STORE_ROW_SCHEMA = {
    "context_id": pl.String,
    "camera_id": pl.String,
    "spawn_timestamp_secs": pl.Float64,
    "camera_image_path": pl.String,
    "image_age": pl.Float64,
}


# TODO: Use threadpoolexecutor.
class CameraSpillableStore(SpillableStore):
    def __init__(self, MAX_ENTRIES: int, camera_id: int, store_dir: str):
        super().__init__(MAX_ENTRIES)
        self.row_list = []

        self.CAMERA_ID = camera_id
        self.STORE_DIR = Path(store_dir)
        if not self.STORE_DIR.exists() or not self.STORE_DIR.is_dir():
            raise ConfigurationError(
                f"Camera storage directory does not exist or is not a directory: {store_dir}"
            )
        self.lock = Lock()

    def generate_filepath(self):
        return (
            self.STORE_DIR
            / f"camera_contexts_service{self.CAMERA_ID}_temp{self.fileno}.csv"
        )

    def append_record(
        self,
        context_id: str,
        camera_id: int,
        spawn_timestamp_secs: float,
        camera_image_bytes: Image,
        image_age: float,
    ):
        with self.lock:
            camera_image_path = (
                self.STORE_DIR / f"service{self.CAMERA_ID}_context_{context_id}.bytes"
            )

            with open(camera_image_path, "wb") as bytes_file:
                bytes_file.write(camera_image_bytes)

            row = {
                "context_id": context_id,
                "camera_id": camera_id,
                "spawn_timestamp_secs": spawn_timestamp_secs,
                "camera_image_path": str(camera_image_path),
                "image_age": image_age,
            }
            self.row_list.append(row)

            super().append_record()

    # this will be called as a final cleanup, or while append_record's lock is acquired. when doing final cleanup, make sure that any append processes are killed.
    def write_to_disk(self):
        if self.currfile_size == 0:
            return

        pl.DataFrame(self.row_list, schema=CAMERA_STORE_ROW_SCHEMA).write_parquet(
            self.generate_filepath()
        )
        super().write_to_disk()

    # triggered by super.write_to_disk only
    def clear_store(self):
        self.row_list.clear()


class CameraDataStream:
    """Captures frames from USB camera and serves them to Client processes via shared memory."""

    def __init__(self, stream_config: CameraDataStreamConfig):
        self._is_cleaned_up = False
        self.camera_id = stream_config.camera_id
        self.mock_mode = stream_config.mock_camera_image_path is not None

        if self.mock_mode:
            # Load mock image from file and resize to expected camera_np_size
            mock_path = Path(stream_config.mock_camera_image_path)
            if not mock_path.exists():
                raise ConfigurationError(f"Mock camera image not found: {mock_path}")
            LOGGER.info("Mock camera mode enabled for service %d, loading image from %s", self.camera_id, mock_path)
            mock_img = cv2.imread(str(mock_path))
            if mock_img is None:
                raise ConfigurationError(f"Failed to read mock camera image: {mock_path}")
            # camera_np_size is [height, width, channels] — resize to (width, height)
            target_h, target_w = stream_config.camera_np_size[0], stream_config.camera_np_size[1]
            mock_img = cv2.resize(mock_img, (target_w, target_h))
            # Convert BGR to RGB (same as the live camera path)
            self.mock_frame_rgb = cv2.cvtColor(mock_img, cv2.COLOR_BGR2RGB)
        else:
            # Initialize USB camera with OpenCV VideoCapture
            # IMPORTANT: Must be released (camera_capture.release()) on shutdown to free USB device
            LOGGER.info("Opening USB camera device %d for service %d", stream_config.usb_id, self.camera_id)
            self.camera_capture = cv2.VideoCapture(stream_config.usb_id)

            if not self.camera_capture.isOpened():
                raise ResourceError(f"Failed to open USB camera device {stream_config.usb_id}")

            # Configure camera properties
            # Buffer size of 1 ensures we always get the latest frame (minimize staleness)
            self.camera_capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.camera_capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            self.camera_capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 1280)
            self.camera_capture.set(cv2.CAP_PROP_FPS, 10.0)

        # Initialize logging storage for camera metadata
        self.spillable_store = CameraSpillableStore(
            stream_config.max_entries,
            stream_config.camera_id,
            Path(stream_config.camera_savedir),
        )
        self.thread_pool_manager = ThreadPoolManager(stream_config.thread_concurrency)

        # Set up ZMQ REP socket for Client requests
        self.context = zmq.Context()
        self.bidirectional_zmq_socket = self.context.socket(zmq.REP)
        self.bidirectional_zmq_socket.bind(stream_config.bidirectional_zmq_sockname)
        LOGGER.info("CameraDataStream bound to ZMQ socket: %s", stream_config.bidirectional_zmq_sockname)

        # Create shared memory region for zero-copy frame transfer to Clients
        self.camera_stream_shmem = shared_memory.SharedMemory(
            name=stream_config.camera_stream_shmem_filename,
            size=int(stream_config.shmem_buf_size),
            create=True,
        )
        LOGGER.info("Created shared memory region: %s (size=%d bytes)",
                   stream_config.camera_stream_shmem_filename,
                   int(stream_config.shmem_buf_size))

        # Map shared memory to numpy array for direct frame writes
        self.camera_stream_arr = np.ndarray(
            stream_config.camera_np_size,
            dtype=np.uint8,
            buffer=self.camera_stream_shmem.buf,
        )

        # Set up ZMQ SUB socket for kill switch (graceful shutdown from client_main)
        self.kill_switch = self.context.socket(zmq.SUB)
        self.kill_switch.setsockopt_string(zmq.SUBSCRIBE, "")
        self.kill_switch.bind(stream_config.zmq_kill_switch_sockname)
        LOGGER.info("CameraDataStream kill switch bound to: %s", stream_config.zmq_kill_switch_sockname)

        # Lock to synchronize access to current frame between capture thread and request handler
        self.image_feed_lock = Lock()

        # Current frame state (initialized to None until first frame captured)
        # curr_image_load_time will be set to perf_counter() when first frame arrives
        self.curr_image_load_time = None
        self.curr_image_pil = None

        # Track consecutive dropped frames to detect client starvation
        self.consecutive_dropped_frames = 0
        self.DROPPED_FRAME_WARNING_THRESHOLD = 10
        self.DROPPED_FRAME_ERROR_THRESHOLD = 500

        self.is_terminated = False

        LOGGER.info("CameraDataStream initialized successfully for service %d", self.camera_id)

    def cleanup(self) -> None:
        """Clean up all resources including spillable store, ZMQ sockets, camera, and SHM.

        Flushes any buffered data to Parquet, closes all ZMQ sockets, terminates the
        ZMQ context, releases the camera device, and closes shared memory.
        Guards each resource with hasattr to handle partially-initialized objects.
        """
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        LOGGER.info("CameraDataStream %d: Starting cleanup of resources", self.camera_id)

        if hasattr(self, 'spillable_store'):
            try:
                self.spillable_store.write_to_disk()
            except Exception as e:
                LOGGER.error("CameraDataStream %d: Error flushing spillable store: %s", self.camera_id, e)

        for attr in ('bidirectional_zmq_socket', 'kill_switch'):
            try:
                if hasattr(self, attr):
                    getattr(self, attr).close(linger=0)
            except Exception as e:
                LOGGER.error("CameraDataStream %d: Error closing %s: %s", self.camera_id, attr, e)

        if hasattr(self, 'context'):
            try:
                self.context.term()
            except Exception as e:
                LOGGER.error("CameraDataStream %d: Error terminating ZMQ context: %s", self.camera_id, e)

        if not self.mock_mode and hasattr(self, 'camera_capture'):
            try:
                self.camera_capture.release()
            except Exception as e:
                LOGGER.error("CameraDataStream %d: Error releasing camera: %s", self.camera_id, e)

        if hasattr(self, 'camera_stream_shmem'):
            try:
                self.camera_stream_shmem.close()
                self.camera_stream_shmem.unlink()
            except Exception as e:
                LOGGER.error("CameraDataStream %d: Error cleaning up shared memory: %s", self.camera_id, e)

        LOGGER.info("CameraDataStream %d: Resource cleanup complete", self.camera_id)

    def __del__(self) -> None:
        """Destructor to ensure cleanup is called even if not explicitly invoked."""
        if hasattr(self, 'camera_id'):
            try:
                self.cleanup()
            except Exception as e:
                print(f"CameraDataStream: Error during __del__ cleanup: {e}")

    def refresh_image_loop(self):
        """Background thread that continuously captures frames from the USB camera. 

        Runs in a tight loop reading frames from VideoCapture and writing them to 
        shared memory. Uses a lock to synchronize with the main request handler thread. 
        Gracefully exits when is_terminated is set. 

        In mock mode, writes the static mock image once and then sleeps until terminated. 
        """
        frame_count = 0
        LOGGER.info("refresh_image_loop started for service %d", self.camera_id)

        if self.mock_mode:
            # Write the pre-loaded mock frame into shared memory once
            with self.image_feed_lock:
                np.copyto(self.camera_stream_arr, self.mock_frame_rgb)
                self.curr_image_pil = Image.fromarray(self.camera_stream_arr)
                self.curr_image_load_time = time.perf_counter()
            LOGGER.info("Mock image written to shared memory for service %d", self.camera_id)
            # Keep thread alive until termination
            while not self.is_terminated:
                time.sleep(0.1)
            return

        while True:
            try:
                # Check termination flag and release camera device if stopping
                if self.is_terminated:
                    LOGGER.info("Termination signal received, releasing camera device")
                    self.camera_capture.release()
                    return

                # Verify camera is still open
                if not self.camera_capture.isOpened():
                    LOGGER.error(
                        "Camera device %d closed unexpectedly for service %d",
                        self.camera_id,
                        self.camera_id
                    )
                    raise ResourceError(
                        f"CameraDataStream {self.camera_id} camera device closed unexpectedly"
                    )

                # Read next frame from USB camera 
                ret, frame = self.camera_capture.read()
                frame_count += 1

                if not ret or frame is None:
                    LOGGER.warning(
                        "Failed to capture frame from camera %d (frame_count=%d, ret=%s, frame=%r)",
                        self.camera_id,
                        frame_count,
                        ret,
                        frame
                    )
                    time.sleep(1)  # Wait before retrying to avoid tight error loop
                    continue

                # Try to acquire lock with timeout to update shared frame 
                # If lock not available (client is reading), skip this frame 
                if self.image_feed_lock.acquire(timeout=0.010):
                    try:
                        # Convert BGR (OpenCV format) to RGB and write to shared memory 
                        cv2.cvtColor(
                            frame, dst=self.camera_stream_arr, code=cv2.COLOR_BGR2RGB
                        )

                        # Update current frame and timestamp 
                        self.curr_image_pil = Image.fromarray(self.camera_stream_arr)
                        self.curr_image_load_time = time.perf_counter()

                        # Reset dropped frame counter on successful frame capture
                        self.consecutive_dropped_frames = 0

                        if frame_count % 100 == 0:
                            LOGGER.debug(
                                "Captured frame %d for service %d (shape=%s)",
                                frame_count,
                                self.camera_id,
                                frame.shape
                            )
                    finally:
                        self.image_feed_lock.release()
                else:
                    # Lock contention - client is reading, skip this frame
                    self.consecutive_dropped_frames += 1

                    # Alert on high lock contention that could indicate client starvation
                    if self.consecutive_dropped_frames >= self.DROPPED_FRAME_ERROR_THRESHOLD:
                        LOGGER.error(
                            "CRITICAL: Dropped %d consecutive frames due to lock contention (service %d, frame %d). "
                            "Client may be starving. Consider implementing double-buffering.",
                            self.consecutive_dropped_frames,
                            self.camera_id,
                            frame_count
                        )
                    elif self.consecutive_dropped_frames >= self.DROPPED_FRAME_WARNING_THRESHOLD:
                        LOGGER.warning(
                            "HIGH CONTENTION: Dropped %d consecutive frames (service %d, frame %d). "
                            "Lock contention is elevated.",
                            self.consecutive_dropped_frames,
                            self.camera_id,
                            frame_count
                        )
                    else:
                        LOGGER.debug(
                            "Skipped frame %d due to lock contention (service %d, consecutive_drops=%d)",
                            frame_count,
                            self.camera_id,
                            self.consecutive_dropped_frames
                        )

                # Small sleep to avoid busy-wait (camera runs at ~10 FPS)
                time.sleep(0.005)

            except Exception as e:
                LOGGER.error(
                    "refresh_image_loop error for service %d (frame %d): %s",
                    self.camera_id,
                    frame_count,
                    str(e),
                    exc_info=True
                )
                # Continue loop to keep trying (don't crash the capture thread)
                time.sleep(1)
                continue

    def main_loop(self):
        # Serves camera frames to Clients via a 2-phase ZMQ handshake over shared memory.
        # Spawns refresh_image_loop in a background thread to continuously capture USB frames.
        # On each Client request: acquires lock, sends frame metadata (frame data is in SHM),
        # waits for Client ACK (confirming it copied the frame), then sends ACK-ACK and releases lock.
        try:
            self.thread_pool_manager.submit(CameraDataStream.refresh_image_loop, self)
            LOGGER.info(
                f"CameraDataStream{self.camera_id} is waiting for first webcam image"
            )

            LOGGER.info(f"CameraDataStream{self.camera_id} is ready! Listening.")
            while True:
                if self.thread_pool_manager.check_due():
                    self.thread_pool_manager.check_pending()

                if self.kill_switch.poll(timeout=1):
                    self.kill_switch.recv()
                    LOGGER.info(
                        "CameraDataStream %d received kill switch signal - shutting down",
                        self.camera_id
                    )
                    self.is_terminated = True
                    self.thread_pool_manager.await_all()
                    self.cleanup()
                    return
                
                if self.curr_image_pil is None:
                    time.sleep(0.5)
                    continue
                
                if not self.bidirectional_zmq_socket.poll(timeout=100):
                    continue

                # Receive request from Client
                LOGGER.info(f"CameraDataStream{self.camera_id} received request")
                recv_obj = self.bidirectional_zmq_socket.recv_pyobj()

                if isinstance(recv_obj, int):
                    LOGGER.info(
                        f"terminating cameradatastream for camera {self.camera_id}"
                    )
                    self.is_terminated = True
                    self.thread_pool_manager.await_all()
                    self.cleanup()
                    return

                if not isinstance(recv_obj, CameraDataRequest):
                    LOGGER.error(
                        "CameraDataStream %d received invalid message type: expected CameraDataRequest, got %s",
                        self.camera_id,
                        type(recv_obj).__name__
                    )
                    raise NetworkError(
                        f"Invalid message type in CameraDataStream {self.camera_id}: expected CameraDataRequest"
                    )

                # Acquire image_feed_lock — shared with refresh_image_loop thread.
                # While held, the refresh thread cannot overwrite the SHM buffer,
                # guaranteeing the Client reads a consistent frame.
                # Lock is held through the full 2-phase handshake:
                #   send response → wait for Client ACK → send ACK-ACK
                # TODO: Consider implementing double-buffering to reduce lock contention:
                # Use two SHM buffers that alternate, allowing capture thread to write to one
                # while request handler reads from the other. This would eliminate blocking.
                with self.image_feed_lock:
                    # Capture current frame state and calculate staleness (image age)
                    this_image_pil = self.curr_image_pil
                    this_image_age = time.perf_counter() - self.curr_image_load_time

                    # Log frame metadata to Parquet (synchronously to avoid memory leak)
                    # NOTE: Previously tried async logging with thread_pool_manager.submit()
                    # but it caused MASSIVE laptop-crashing memory leaks due to PIL Image references not being released
                    self.spillable_store.append_record(
                        recv_obj.context_id,
                        self.camera_id,
                        self.curr_image_load_time,
                        this_image_pil.tobytes(),
                        this_image_age,
                    )

                    # Send response metadata to Client (actual image data is in shared memory) 
                    self.bidirectional_zmq_socket.send_pyobj(
                        CameraDataResponse(
                            context_id=recv_obj.context_id, is_ack_response=False
                        )
                    )

                    LOGGER.info(f"CameraDataStream{self.camera_id} has sent response")

                    # Wait for Client ACK confirming it has copied the frame from SHM
                    if not self.bidirectional_zmq_socket.poll(timeout=3000):
                        raise NetworkError(
                            f"Failed to receive ACK from main client for service {self.camera_id}"
                        )

                    LOGGER.info(f"CameraDataStream{self.camera_id} has received ACK")
                    recv_obj = self.bidirectional_zmq_socket.recv_pyobj()

                    if isinstance(recv_obj, int):
                        LOGGER.info(
                            f"terminating cameradatastream for camera {self.camera_id}"
                        )
                        self.is_terminated = True
                        self.thread_pool_manager.await_all()
                        self.cleanup()
                        return

                    # Send ACK-ACK back to Client, completing the handshake.
                    # After this, the lock is released and refresh thread can write new frames.
                    self.bidirectional_zmq_socket.send_pyobj(
                        CameraDataResponse(
                            context_id=recv_obj.context_id, is_ack_response=True
                        )
                    )

                    if not isinstance(recv_obj, CameraDataRequest):
                        LOGGER.error(
                            "CameraDataStream %d received invalid ACK message type: expected CameraDataRequest, got %s",
                            self.camera_id,
                            type(recv_obj).__name__
                        )
                        raise NetworkError(
                            f"Invalid ACK message type in CameraDataStream {self.camera_id}: expected CameraDataRequest"
                        )

                    if recv_obj.is_ack_request:
                        continue
                    else:
                        LOGGER.warning(
                            "CameraDataStream %d received non-ACK request after ACK handshake. "
                            "Expected is_ack_request=True but got False. Continuing anyway.",
                            self.camera_id
                        )

        except Exception as e:
            LOGGER.error(
                f"CameraDataStream {self.camera_id} main_loop failed with error {e}"
            )
            LOGGER.error(e)
            raise e
