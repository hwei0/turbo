# Experiment Logging Reference

This document provides detailed documentation for the experiment logging system in TURBO. For a quick start, see the main [README.md](../README.md).

## Overview

Every component logs structured experiment data to **Parquet files** via the SpillableStore pattern (Python) or the `create_flush_parquet` utility (Rust). Records accumulate in memory up to a configurable `max_entries` threshold, then spill to a numbered Parquet file on disk. This produces sequentially numbered part files during a single experiment run.

All Python-side Parquet files are written to the `client_savedir` / `server_log_savedir` / `ping_savedir` directories specified in the YAML configs (typically `~/experiment2-out/client/` and `~/experiment2-out/server/`). Rust-side Parquet files are written to `quic_client_log_path` / `quic_server_log_path` (typically `~/experiment2-out/quic-client-out/` and `~/experiment2-out/quic-server-out/`).

## Python-Side Logs

### 1. Client Query Log — `client.py` → `ClientSpillableStore`

**Filename pattern**: `client_queries_service{N}_temp{K}.csv` (Parquet despite `.csv` extension)

One file per service. Logs every inference request the Client makes, including full latency breakdown and detection results.

| Column | Type | Description |
|--------|------|-------------|
| `service_id` | Int32 | Service/camera ID |
| `context_id` | Int32 | Monotonically increasing frame counter |
| `start_time_unix` | Float64 | Unix epoch timestamp at start of request |
| `start_time` | Float64 | `perf_counter()` timestamp at start of request |
| `camera_recv_latency` | Float64 | Time to receive camera frame metadata via ZMQ (s) |
| `camera_ack_latency` | Float64 | Time for camera ACK round-trip (s) |
| `preprocessing_delay` | Float64 | Time for image preprocessing and/or compression (s) |
| `request_start_time` | Float64 | `perf_counter()` at start of QUIC send |
| `request_serialization_delay` | Float64 | Time to pickle and write request to SHM (s) |
| `request_ack_latency` | Float64 | Time for QUIC ACK after sending request (s) |
| `response_listen_start_time` | Float64 | `perf_counter()` when Client starts listening for response |
| `good_response_listen_delay` | Float64 | Time from listen start to receiving the correct (matching context_id) response (s) |
| `good_response_deserialization_latency` | Float64 | Time to read and unpickle the response from SHM (s) |
| `response_listen_end_time` | Float64 | `perf_counter()` when response processing finishes |
| `response_overall_recv_delay` | Float64 | Total time from request start to response received (s) |
| `end_time` | Float64 | `perf_counter()` at end of entire iteration |
| `total_latency` | Float64 | End-to-end latency for this request (s) |
| `allocated_model` | String | Model configuration string (e.g., `edd4-imgcomp50-inpcompNone`) |
| `remote_request_received` | Boolean | `True` if response was received from the server; `False` if local-only (edd1) |
| `[CameraBoxComponent].box.center.x` | Float64 | Detected bounding box center X (null if local-only or no detections) |
| `[CameraBoxComponent].box.center.y` | Float64 | Detected bounding box center Y |
| `[CameraBoxComponent].box.size.x` | Float64 | Bounding box width |
| `[CameraBoxComponent].box.size.y` | Float64 | Bounding box height |
| `[CameraBoxComponent].type` | Int8 | Object class label |
| `score` | Float64 | Detection confidence score |

**Note**: When a response contains multiple bounding boxes, one row is logged per box (all sharing the same `context_id` and latency columns).

---

### 2. Camera Context Log — `camera_stream/camera_data_stream.py` → `CameraSpillableStore`

**Filename pattern**: `camera_contexts_service{N}_temp{K}.csv` (Parquet despite `.csv` extension)

One file per camera. Logs metadata about each captured frame.

| Column | Type | Description |
|--------|------|-------------|
| `context_id` | String | Frame counter |
| `camera_id` | String | Camera/service ID |
| `spawn_timestamp_secs` | Float64 | Unix epoch timestamp when frame was served |
| `camera_image_path` | String | Path to the raw image bytes file saved alongside this record |
| `image_age` | Float64 | Staleness of the frame at time of serving (s) — how long since the capture thread last refreshed the SHM buffer |

Additionally, the raw camera frame bytes are saved as `service{N}_context_{K}.bytes` files in the same directory.

---

### 3. Model Server Log — `server.py` → `ModelServerSpillableStore`

**Filename pattern**: `server_results_service{N}_temp{K}` (Parquet, no extension)

One file per service. Logs server-side processing latency for each inference request.

| Column | Type | Description |
|--------|------|-------------|
| `service_id` | String | Service ID |
| `context_id` | String | Frame counter from the client |
| `timestamp_secs` | Float64 | Unix epoch timestamp at start of processing |
| `deserialization_latency` | Float64 | Time to read and unpickle the request from SHM (s) |
| `deserialization_polling_latency` | Float64 | Total time from first ZMQ poll to having a deserialized request (includes queue draining) (s) |
| `preprocessing_latency` | Float64 | Time for server-side preprocessing (decompress + resize + normalize) (s) |
| `inference_latency` | Float64 | GPU inference time (s) |
| `serialization_latency` | Float64 | Time to pickle the response and write to SHM (s) |
| `ack_latency` | Float64 | Time for ZMQ ACK after sending response (s) |
| `overall_response_latency_without_ack` | Float64 | Total processing time excluding ACK wait (s) |
| `start_time_counter` | Float64 | `perf_counter()` at processing start |
| `end_time_counter` | Float64 | `perf_counter()` at processing end |
| `requested_processing` | String | Model configuration string requested by the client |

---

### 4. Ping Log — `ping_handler/ping_handler.py` → `PingSpillableStore`

**Filename pattern**: `ping_loop_allservices_temp{K}.csv` (Parquet despite `.csv` extension)

Single file shared across all services. Logs every ICMP ping measurement.

| Column | Type | Description |
|--------|------|-------------|
| `spawn_timestamp_secs` | Float64 | Unix epoch timestamp when the ping was sent |
| `ping_duration` | Float64 | ICMP round-trip time (s), or `1.0` if the ping timed out or failed |

---

## Rust-Side Logs (QUIC Transport Layer)

All Rust logs are written by the `quic_conn` crate's `logging/` module using Polars `ParquetWriter`. Each log type can be independently enabled/disabled via the QUIC config YAML flags.

### 5. Bandwidth Stat Log — `bandwidth_logging.rs` → `BandwidthStatLog`

**Filename pattern**: `bandwidth_stat_log_allservices_part{K}.parquet`

Single file (not per-service). Logs QUIC recovery metrics sampled during the `bandwidth_refresh_loop`. **Client-side only** (the server does not run a bandwidth refresh loop, but the config flag exists for structural symmetry).

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | Float64 | s2n-quic recovery event timestamp (seconds since connection start) |
| `instant_timestamp` | Float64 | Experiment-relative timestamp (`Instant::elapsed()`, s) |
| `rtt` | Float64 | QUIC smoothed RTT (s) |
| `cwnd` | UInt32 | QUIC congestion window (bytes) |

**Config flag**: `client_enable_bw_stat_log` / `server_enable_bw_stat_log`

---

### 6. Allocation Stat Log — `allocation_logging.rs` → `AllocationStatLog`

**Filename pattern**: `allocation_stat_log_allservices_part{K}.parquet`

Single file. Logs every bandwidth allocation update received from the BandwidthAllocator. **Client-side only.**

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | Float64 | s2n-quic recovery event timestamp (s) |
| `instant_timestamp` | Float64 | Experiment-relative timestamp (s) |
| `allocation_service{N}` | Float64 | Bandwidth allocated to service N (bytes/s) — one column per service |
| `model_config_service{N}` | String | Model configuration string for service N — one column per service |
| `expected_utility` | Float64 | Total expected utility from the solver |

**Config flag**: `client_enable_allocation_stat_log` / `server_enable_allocation_stat_log`

---

### 7. Network Stat Log — `network_logging.rs` → `NetworkStatLog`

**Filename pattern**: `network_stat_log_service{N}_part{K}.parquet`

One file per service. Logs cumulative byte counts for data sent and received on each service's QUIC stream, sampled at `logging_interval_ms`.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | Float64 | s2n-quic event timestamp (s) |
| `instant_timestamp` | Float64 | Experiment-relative timestamp (s) |
| `tx_cnt` | Int64 | Cumulative bytes transmitted on this service's stream |
| `rx_cnt` | Int64 | Cumulative bytes received on this service's stream |

**Config flag**: `client_enable_network_stat_log` / `server_enable_network_stat_log`

---

### 8. Image Context Log — `image_context_logging.rs` → `ImageContextLog`

**Filename pattern**: `image_context_log_{direction}_service{N}_part{K}` (Parquet, no extension)

Two files per service: one `outgoing` (data leaving toward the QUIC stream) and one `incoming` (data arriving from the QUIC stream). Provides fine-grained per-image tracing through each stage of the QUIC transport pipeline. Junk service frames are excluded.

| Column | Type | Description |
|--------|------|-------------|
| `image_context` | Int32 | Frame counter (context_id) |
| `begin_time_secs` | Float64 | Experiment-relative start timestamp for this stage (s); NaN for end records |
| `end_time_secs` | Float64 | Experiment-relative end timestamp for this stage (s); NaN for begin records |
| `end_time_secs_normalized` | Float64 | `end_time_secs` minus ACK delay (s); NaN for begin records |
| `begin_time_epoch_secs` | Float64 | Unix epoch start timestamp (s); NaN for end records |
| `end_time_epoch_secs` | Float64 | Unix epoch end timestamp (s); NaN for begin records |
| `ack_delay_secs` | Float64 | Time spent waiting for ZMQ ACK (s); NaN for begin records |
| `image_size_bytes` | Int32 | Image payload size in bytes (begin records only; -1 for end records) |
| `record_type` | String | Stage identifier (see below) |

**Record types** (defined in `utils.rs::RecordType`):

- `IMAGE` — overall image begin/end (full pipeline span)
- `SHM_COPY` — shared memory copy operation
- `ENQUEUE_MSG` — enqueuing message into the per-service send queue
- `FLUSH` — flushing data to the QUIC stream
- `TX_RX` — QUIC stream write/read operation
- `INTERMEDIATE` — intermediate checkpoint within a pipeline stage

Each image generates multiple begin/end record pairs as it passes through `read_local_zmq_socket` → `send_loop` (outgoing) or `read_quic_stream` (incoming), allowing precise reconstruction of per-image timing through the transport layer.

**Config flags**: `client_enable_incoming_image_context_log`, `client_enable_outgoing_image_context_log`, `server_enable_incoming_image_context_log`, `server_enable_outgoing_image_context_log`

---

## Output File Summary

| # | File Pattern | Producer | Side | Per-Service? | Key Contents |
|---|-------------|----------|------|-------------|--------------|
| 1 | `client_queries_service{N}_temp{K}.csv` | Client (Python) | Client | Yes | End-to-end latency breakdown, model config, detection bounding boxes |
| 2 | `camera_contexts_service{N}_temp{K}.csv` | CameraDataStream (Python) | Client | Yes | Frame metadata, raw image paths, image staleness |
| 3 | `server_results_service{N}_temp{K}` | ModelServer (Python) | Server | Yes | Server-side latency breakdown (deserialize, preprocess, infer, serialize) |
| 4 | `ping_loop_allservices_temp{K}.csv` | PingHandler (Python) | Client | No | ICMP RTT measurements |
| 5 | `bandwidth_stat_log_allservices_part{K}.parquet` | QUIC Client (Rust) | Client | No | QUIC recovery metrics (RTT, CWND) |
| 6 | `allocation_stat_log_allservices_part{K}.parquet` | QUIC Client (Rust) | Client | No | Per-service bandwidth allocations, model configs, expected utility |
| 7 | `network_stat_log_service{N}_part{K}.parquet` | QUIC Client/Server (Rust) | Both | Yes | Cumulative TX/RX byte counts per service |
| 8 | `image_context_log_{dir}_service{N}_part{K}` | QUIC Client/Server (Rust) | Both | Yes | Per-image timing trace through transport pipeline (SHM_COPY, ENQUEUE, FLUSH, TX_RX) |
