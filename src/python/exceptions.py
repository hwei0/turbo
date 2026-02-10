"""Custom exception hierarchy for the AV bandwidth sharing system.

This module defines a consistent error taxonomy used throughout the system to
distinguish between different failure modes and enable appropriate error handling
strategies at each layer.
"""

class AVBandwidthError(Exception):
    """Base exception for all AV bandwidth sharing system errors.

    All custom exceptions in this system inherit from this base class,
    allowing catch-all error handling when needed while still maintaining
    specific error types for precise handling.
    """
    pass


class ConfigurationError(AVBandwidthError):
    """Invalid or missing configuration.

    Raised when:
    - Required configuration fields are missing
    - Configuration values are out of valid range
    - Configuration files cannot be parsed
    - Service IDs, paths, or other config parameters are invalid

    This is typically a fatal error that should halt process startup.
    """
    pass


class NetworkError(AVBandwidthError):
    """Network or IPC communication failure.

    Raised when:
    - ZMQ socket operations fail (bind, connect, send, recv)
    - QUIC connection/stream operations fail
    - Shared memory operations fail
    - TCP/UDP socket errors occur

    May be transient (retryable) or fatal depending on context.
    """
    pass


class SLOViolation(AVBandwidthError):
    """Service-level objective (latency) constraint violated.

    Raised when:
    - End-to-end latency exceeds configured SLO timeout
    - Frame processing takes longer than deadline
    - Queue wait times exceed threshold

    This is typically a non-fatal warning that results in frame dropping.
    """
    pass


class ResourceError(AVBandwidthError):
    """System resource unavailable or exhausted.

    Raised when:
    - Camera device cannot be opened (USB camera unavailable)
    - GPU memory exhausted
    - Shared memory allocation fails
    - Thread pool or process pool capacity exceeded

    May be retryable after resource cleanup or fatal if resources cannot be acquired.
    """
    pass


class SerializationError(AVBandwidthError):
    """Data serialization or deserialization failure.

    Raised when:
    - Pickle loads/dumps fail
    - Malformed messages received over IPC
    - Image compression/decompression fails
    - Parquet file writing fails

    Usually indicates data corruption or protocol mismatch.
    """
    pass


class ModelError(AVBandwidthError):
    """Model loading or inference failure.

    Raised when:
    - EfficientDet checkpoint cannot be loaded
    - Model inference fails (shape mismatch, device error)
    - Preprocessing transforms fail
    - Invalid model configuration string

    Typically fatal as inference cannot proceed without a working model.
    """
    pass


class GracefulShutdown(AVBandwidthError):
    """Process received a kill signal during initialization.

    Raised when:
    - A kill switch signal is detected during a blocking __init__ handshake wait
      (e.g., Client or ModelServer waiting for QUIC peer to connect)

    This is NOT an error — it indicates a normal shutdown path. The run_* wrapper
    functions in client_main.py and server_main.py catch this exception and return
    normally so that the Pool's error_callback is not triggered.
    """
    pass


class AllocationError(AVBandwidthError):
    """Bandwidth allocation solver failure.

    Raised when:
    - LP solver fails to find optimal allocation
    - Utility curves are invalid or empty
    - Bandwidth constraints cannot be satisfied
    - Solver returns infeasible solution

    Should fall back to safe default allocation (e.g., equal share or local-only).
    """
    pass
