# Configuration Guide

This document provides detailed configuration documentation for TURBO. For a quick start, see the main [README.md](../README.md).

## Overview

The system uses four YAML configuration files:

| File | Used by | Purpose |
|------|---------|---------|
| `config/client_config.yaml` | Python client processes | Camera streams, clients, bandwidth allocator, ping handler, plotter |
| `config/server_config_gcloud.yaml` | Python server processes | Model servers and GPU assignments |
| `config/quic_config_client.yaml` | Rust QUIC client binary | QUIC client transport, logging, junk service |
| `config/quic_config_gcloud.yaml` | Rust QUIC server binary | QUIC server transport, logging |

The client and server QUIC configs share the same schema but may have different values (e.g., different `junk_tx_loop_interval_ms` for local vs cloud).

### Naming Conventions

The config files make heavy use of **YAML anchors** (`&anchor-name`) and **references** (`*anchor-name`) to avoid duplication. For example, `SLO_TIMEOUT: &slo-timeout 200` defines the value once, and each per-service entry references it with `SLO_TIMEOUT_MS: *slo-timeout`.

### IPC Socket Path Convention

All ZMQ IPC socket paths follow the pattern `ipc://<zmq_pathdir>/<socket-name>`. The `zmq_pathdir` is set in the QUIC config, and all socket paths in the client/server configs must use the same base directory. See [IPC.md](IPC.md) for the full socket reference.

---

## 1. Client Configuration — `config/client_config.yaml`

Configures the client-side (AV) components: camera streams, perception service clients, bandwidth allocator, ping handler, web dashboard, and diagnostic plotter.

### Global Parameters

```yaml
logging_config_filepath: /path/to/turbo/config/logging_config.yaml

DST_IP: &dst-ip <YOUR_SERVER_IP>    # Cloud server IP for ICMP pings
SLO_TIMEOUT: &slo-timeout 200       # Client-side SLO timeout (ms), shared via anchor
QUIC_SHM_SIZE: &quic-shm-size 50000000  # Shared memory region size (bytes, ~50 MB)
MAX_LOG_ENTRIES: &max-log-entries 100    # Max records in memory before spilling to Parquet
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `logging_config_filepath` | Yes | Absolute path to the Python logging config file |
| `DST_IP` | **Yes** | Public IP of your cloud server. Used by PingHandler for RTT measurement |
| `SLO_TIMEOUT` | Maybe | Service-level objective timeout in ms. Frames exceeding this are dropped. Default `200` is suitable for most setups |
| `QUIC_SHM_SIZE` | No | Size of each POSIX shared memory region in bytes. `50000000` (50 MB) is sufficient for HD frames |
| `MAX_LOG_ENTRIES` | No | Number of log records buffered in memory before flushing to Parquet. Higher = fewer I/O flushes, more memory |

### Web Dashboard Configuration

```yaml
web_dashboard_config:
  refresh_rate_seconds: 6
  plotting_loop_sleep_seconds: 2
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `refresh_rate_seconds` | No | How often the web dashboard refreshes plots (seconds) |
| `plotting_loop_sleep_seconds` | No | Sleep interval for the plotting loop (seconds) |

### Model Image Sizes

```yaml
model_image_size_map: &model_imagesizes
  tf_efficientdet_d1: [640, 640]
  tf_efficientdet_d2: [768, 768]
  tf_efficientdet_d4: [1024, 1024]
  tf_efficientdet_d6: [1280, 1280]
  tf_efficientdet_d7x: [1536, 1536]
```

Maps each EfficientDet variant to its native input resolution. **Do not change** unless using custom-trained models with different input sizes.

### Per-Service Client Configuration (`main_client_config_list`)

One entry per perception service. Add or remove entries to match your number of cameras/services.

```yaml
main_client_config_list:
  - service_id: 1
    client_savedir: /path/to/experiment-out/client
    max_entries: *max-log-entries
    thread_concurrency: 10
    camera_bidirectional_zmq_sockname: ipc:///path/to/zmq/service1-camera-socket
    camera_stream_shmem_filename: service1-camera-shmem
    bandwidth_allocation_incoming_zmq_sockname: ipc:///path/to/zmq/main-client-1-bw-subscriber
    quic_rcv_zmq_sockname: ipc:///path/to/zmq/car-server-outgoing-1
    quic_snd_zmq_sockname: ipc:///path/to/zmq/car-server-incoming-1
    outgoing_zmq_diagnostic_sockname: ipc:///path/to/zmq/car-client-diagnostics
    camera_np_size: [1080, 1920, 3]
    model_name_imagesize_map: *model_imagesizes
    zmq_kill_switch_sockname: ipc:///path/to/zmq/client-kill-1-switch
    quic_snd_shm_filename: client-service1-incoming-shm
    quic_rcv_shm_filename: client-service1-outgoing-shm
    quic_shm_size: *quic-shm-size
    SLO_TIMEOUT_MS: *slo-timeout
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `service_id` | Yes (if adding/removing services) | Unique integer ID for this service. Must match across client, server, and QUIC configs |
| `client_savedir` | **Yes** | Directory for client-side Parquet log output. Must exist and be writable |
| `max_entries` | No | Log buffer size before spilling to Parquet (uses global anchor) |
| `thread_concurrency` | No | Number of threads in the client's thread pool. Default `10` is suitable for most setups |
| `camera_bidirectional_zmq_sockname` | Yes (path) | ZMQ REQ/REP socket to the CameraDataStream for this service. Replace path prefix with your `zmq_pathdir` |
| `camera_stream_shmem_filename` | No | POSIX SHM region name for raw camera frames. Must match the corresponding camera stream entry |
| `bandwidth_allocation_incoming_zmq_sockname` | Yes (path) | ZMQ SUB socket to receive bandwidth allocation updates from the BandwidthAllocator |
| `quic_rcv_zmq_sockname` | Yes (path) | ZMQ socket for sending compressed images to the QUIC client |
| `quic_snd_zmq_sockname` | Yes (path) | ZMQ socket for receiving inference results from the QUIC client |
| `outgoing_zmq_diagnostic_sockname` | Yes (path) | ZMQ PUB socket for sending diagnostic messages to the plotter. All services share the same socket name |
| `camera_np_size` | Maybe | Camera frame dimensions as `[height, width, channels]`. Change if your cameras produce a different resolution |
| `model_name_imagesize_map` | No | Reference to the global model image size map (uses anchor) |
| `zmq_kill_switch_sockname` | Yes (path) | ZMQ SUB socket for receiving graceful shutdown signals from `client_main.py` |
| `quic_snd_shm_filename` | No | POSIX SHM region name for outgoing images to QUIC |
| `quic_rcv_shm_filename` | No | POSIX SHM region name for incoming results from QUIC |
| `quic_shm_size` | No | Size of the QUIC shared memory regions (uses global anchor) |
| `SLO_TIMEOUT_MS` | No | Per-service SLO deadline in ms (uses global anchor) |

**When adding a new service**, duplicate an existing entry, increment `service_id`, and update all socket names and SHM filenames to use the new service number (e.g., replace `-1` with `-4`).

### Bandwidth Allocator Configuration (`bandwidth_allocator_config`)

```yaml
bandwidth_allocator_config:
  service_id_list: [1, 2, 3]
  t_SLO: 150
  parquet_eval_dir: /path/to/full-eval
  model_info_csv_path: /path/to/turbo/experiment_model_info.csv
  outgoing_zmq_diagnostic_sockname: ipc:///path/to/zmq/car-client-diagnostics
  outgoing_zmq_client_socknames:
    - ipc:///path/to/zmq/main-client-1-bw-subscriber
    - ipc:///path/to/zmq/main-client-2-bw-subscriber
    - ipc:///path/to/zmq/main-client-3-bw-subscriber
  bidirectional_zmq_quic_sockname: ipc:///path/to/zmq/car-server-bw-service
  zmq_kill_switch_sockname: ipc:///path/to/zmq/bandwidth-allocator-kill-switch
  bidirectional_zmq_ping_handler_sockname: ipc:///path/to/zmq/ping-handler
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `service_id_list` | Yes (if adding/removing services) | List of active service IDs to allocate bandwidth across. Must not include the junk service |
| `t_SLO` | Maybe | Latency SLO constraint used by the LP solver (ms). Should be slightly tighter than `SLO_TIMEOUT` to account for processing overhead |
| `parquet_eval_dir` | **Yes** | Path to directory containing pre-computed utility curve Parquet files. These are generated offline and encode the bandwidth-to-accuracy mapping for each model configuration |
| `model_info_csv_path` | **Yes** | Path to `experiment_model_info.csv`, which maps model configuration strings to transport size (Mb) and runtime (ms) |
| `outgoing_zmq_diagnostic_sockname` | Yes (path) | ZMQ PUB socket for allocation diagnostics. Should match the diagnostic socket used by clients |
| `outgoing_zmq_client_socknames` | Yes (path) | List of ZMQ PUB sockets, one per service. Each must match the corresponding client's `bandwidth_allocation_incoming_zmq_sockname` |
| `bidirectional_zmq_quic_sockname` | Yes (path) | ZMQ REP socket for receiving bandwidth/RTT updates from the QUIC client |
| `zmq_kill_switch_sockname` | Yes (path) | ZMQ SUB socket for graceful shutdown |
| `bidirectional_zmq_ping_handler_sockname` | Yes (path) | ZMQ REQ socket for querying RTT from the PingHandler |

### Camera Stream Configuration (`camera_stream_config_list`)

One entry per USB camera. Must have a matching entry in `main_client_config_list`.

```yaml
camera_stream_config_list:
  - camera_id: 1                     # Must match service_id
    camera_savedir: /path/to/experiment-out/client
    usb_id: 0                        # USB device index
    max_entries: *max-log-entries
    thread_concurrency: 10
    bidirectional_zmq_sockname: ipc:///path/to/zmq/service1-camera-socket
    camera_stream_shmem_filename: service1-camera-shmem
    shmem_buf_size: *quic-shm-size
    camera_np_size: [1080, 1920, 3]
    zmq_kill_switch_sockname: ipc:///path/to/zmq/camera-kill-1-switch
    mock_camera_image_path: null
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `camera_id` | Yes (if adding/removing services) | Must match the `service_id` of the corresponding client entry |
| `camera_savedir` | **Yes** | Directory for camera Parquet log output |
| `usb_id` | **Yes** | USB camera device index for `cv2.VideoCapture`. Run `ls /dev/video*` to identify your cameras. These indices vary by system |
| `max_entries` | No | Log buffer size (uses global anchor) |
| `thread_concurrency` | No | Thread pool size for camera operations |
| `bidirectional_zmq_sockname` | Yes (path) | ZMQ REP socket for client communication. Must match the corresponding client's `camera_bidirectional_zmq_sockname` |
| `camera_stream_shmem_filename` | No | SHM region name. Must match the corresponding client's `camera_stream_shmem_filename` |
| `shmem_buf_size` | No | SHM buffer size in bytes (uses global anchor) |
| `camera_np_size` | Maybe | Camera frame dimensions `[height, width, channels]`. Must match the client entry |
| `zmq_kill_switch_sockname` | Yes (path) | ZMQ SUB socket for graceful shutdown |
| `mock_camera_image_path` | Maybe | Set to a file path (e.g., `mock_webcam_image.jpg`) to use a static image instead of a live webcam. Useful for testing without hardware. Set to `null` for live camera |

### Ping Handler Configuration (`ping_handler_config`)

```yaml
ping_handler_config:
  dst_ip: *dst-ip
  ping_savedir: /path/to/experiment-out/client
  max_entries: 100
  thread_concurrency: 5
  bidirectional_zmq_sockname: ipc:///path/to/zmq/ping-handler
  zmq_kill_switch_sockname: ipc:///path/to/zmq/ping-handler-kill-switch
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `dst_ip` | **Yes** | IP address to ping for RTT measurement. Should be your cloud server's public IP (uses global `DST_IP` anchor) |
| `ping_savedir` | **Yes** | Directory for ping log Parquet output |
| `max_entries` | No | Log buffer size |
| `thread_concurrency` | No | Thread pool size |
| `bidirectional_zmq_sockname` | Yes (path) | ZMQ REP socket. Must match the bandwidth allocator's `bidirectional_zmq_ping_handler_sockname` |
| `zmq_kill_switch_sockname` | Yes (path) | ZMQ SUB socket for graceful shutdown |

### Diagnostic Plotter Configuration (`main_plotter_config`)

Configures the real-time matplotlib diagnostic plots. This section is optional — the plotter is started separately and can be omitted if you only use the web dashboard.

```yaml
main_plotter_config:
  plotting_loop_sleep_seconds: 2
  zmq_incoming_diagnostic_name: ipc:///path/to/zmq/car-client-diagnostics
  bandwidth_allocation_plot_config:
    service_id_list: [1, 2, 3]
    window_size_x: 40               # X-axis window size (seconds of data shown)
    bw_min_y: -10                    # Bandwidth plot Y-axis minimum
    bw_max_y: null                   # Bandwidth plot Y-axis maximum (null = auto)
    utility_min_y: -0.1              # Utility plot Y-axis minimum
    utility_max_y: 1.1               # Utility plot Y-axis maximum
    # Tick mark spacing (major/minor) for each subplot:
    bw_x_major_loc: 15
    bw_x_minor_loc: 5
    bw_y_major_loc: 100
    bw_y_minor_loc: 20
    utility_x_major_loc: 15
    utility_x_minor_loc: 5
    utility_y_major_loc: 0.2
    utility_y_minor_loc: 0.1

  service_status_plot_config:        # One entry per service
    - service_id: 1
      window_size_x: 40
      cnt_min_y: -3                  # Request count Y-axis minimum
      cnt_max_y: null
      rate_min_y: -0.5               # Success rate Y-axis minimum
      rate_max_y: 1.05
      # Tick mark spacing for count and rate subplots:
      cnt_x_major_loc: 15
      cnt_x_minor_loc: 5
      cnt_y_major_loc: 30
      cnt_y_minor_loc: 10
      rate_x_major_loc: 15
      rate_x_minor_loc: 5
      rate_y_major_loc: 0.2
      rate_y_minor_loc: 0.05

  service_utilization_plot_config:   # One entry per service (including junk)
    - service_id: 1
      window_size_x: 40
      min_y: -10                     # Utilization Y-axis minimum
      max_y: null
      x_major_loc: 15
      x_minor_loc: 5
      y_major_loc: 100
      y_minor_loc: 20
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `plotting_loop_sleep_seconds` | No | Sleep interval between plot updates |
| `zmq_incoming_diagnostic_name` | Yes (path) | ZMQ SUB socket for receiving diagnostics from clients, bandwidth allocator, and QUIC. Must match `outgoing_zmq_diagnostic_sockname` used by other components |
| `bandwidth_allocation_plot_config` | No | Plot layout for the bandwidth allocation overview. Adjust `window_size_x` to show more or less history |
| `service_status_plot_config` | Yes (if adding/removing services) | One entry per service. Adjust to match your `service_id_list` |
| `service_utilization_plot_config` | Yes (if adding/removing services) | One entry per service including junk service (service 4). Adjust to match your services list |

---

## 2. Server Configuration — `config/server_config_gcloud.yaml`

Configures the server-side (cloud) components: model servers and GPU assignments.

### Global Parameters

```yaml
logging_config_filepath: /path/to/turbo/config/logging_config.yaml

MAX_LOG_ENTRIES: &max-log-entries 100
SHM_FILESIZE: &shm-filesize 50000000
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `logging_config_filepath` | Yes | Absolute path to the Python logging config file |
| `MAX_LOG_ENTRIES` | No | Log buffer size before spilling to Parquet |
| `SHM_FILESIZE` | No | Shared memory region size in bytes (~50 MB) |

### Model Metadata (`server_model_list`)

Defines all available EfficientDet model checkpoints. These are shared across all model servers via YAML anchor.

```yaml
server_model_list: &model-list-ref
  - checkpoint_path: /path/to/av-models/tf_efficientdet_d2-waymo-open-dataset/version_2/checkpoints/epoch=9-step=419700.ckpt
    num_classes: 5
    image_size: [768, 768]
    base_model: "tf_efficientdet_d2"
  - checkpoint_path: /path/to/av-models/tf_efficientdet_d4-waymo-open-dataset/version_0/checkpoints/epoch=9-step=839400.ckpt
    num_classes: 5
    image_size: [1024, 1024]
    base_model: "tf_efficientdet_d4"
  - checkpoint_path: /path/to/av-models/tf_efficientdet_d6-waymo-open-dataset/version_2/checkpoints/epoch=9-step=3357600.ckpt
    num_classes: 5
    image_size: [1280, 1280]
    base_model: "tf_efficientdet_d6"
  - checkpoint_path: /path/to/av-models/tf_efficientdet_d7x-waymo-open-dataset/version_1/checkpoints/epoch=8-step=1477071.ckpt
    num_classes: 5
    image_size: [1536, 1536]
    base_model: "tf_efficientdet_d7x"
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `checkpoint_path` | **Yes** | Absolute path to each PyTorch Lightning `.ckpt` file. Update after downloading models (see [MODELS.md](MODELS.md)) |
| `num_classes` | No | Number of detection classes. `5` for the provided Waymo-trained models |
| `image_size` | No | Native input resolution `[height, width]` for each model variant. Must match the `model_image_size_map` in the client config |
| `base_model` | No | Model variant name matching the `effdet` library naming convention |

### Per-Service Server Configuration (`server_config_list`)

One entry per perception service. Add or remove entries to match your number of services.

```yaml
server_config_list:
  - service_id: 1
    server_log_savedir: /path/to/experiment-out/server
    max_entries: *max-log-entries
    model_metadata_list: *model-list-ref
    device: "cuda:0"
    incoming_zmq_sockname: ipc:///path/to/zmq/remote-server-outgoing-1
    incoming_shm_filename: server-service1-outgoing-shm
    outgoing_zmq_sockname: ipc:///path/to/zmq/remote-server-incoming-1
    outgoing_shm_filename: server-service1-incoming-shm
    thread_concurrency: 10
    shm_filesize: *shm-filesize
    zmq_kill_switch_sockname: ipc:///path/to/zmq/remote-server-kill-switch-1
    mock_inference_output_path: null
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `service_id` | Yes (if adding/removing services) | Unique integer ID. Must match the corresponding client-side service |
| `server_log_savedir` | **Yes** | Directory for server-side Parquet log output. Must exist and be writable |
| `max_entries` | No | Log buffer size (uses global anchor) |
| `model_metadata_list` | No | Reference to `server_model_list` (uses YAML anchor). All servers share the same model list |
| `device` | **Yes** | PyTorch device string (e.g., `cuda:0`, `cuda:1`, `cpu`). Assign a different GPU to each service for parallelism. Run `nvidia-smi` to see available GPUs |
| `incoming_zmq_sockname` | Yes (path) | ZMQ REP socket for receiving images from the QUIC server |
| `incoming_shm_filename` | No | SHM region name for incoming image data |
| `outgoing_zmq_sockname` | Yes (path) | ZMQ REQ socket for sending inference results back to the QUIC server |
| `outgoing_shm_filename` | No | SHM region name for outgoing inference results |
| `thread_concurrency` | No | Thread pool size for server operations |
| `shm_filesize` | No | SHM region size in bytes (uses global anchor) |
| `zmq_kill_switch_sockname` | Yes (path) | ZMQ SUB socket for graceful shutdown from `server_main.py` |
| `mock_inference_output_path` | Maybe | Set to a `.npz` file path (e.g., `example_effdet_d4_output.npz`) to skip model loading and return pre-recorded detections. Useful for testing without a GPU. Set to `null` for real inference |

---

## 3. QUIC Configuration — `config/quic_config_client.yaml` / `config/quic_config_gcloud.yaml`

Configures the QUIC transport layer (Rust binaries: `quic_client` and `quic_server`). You need two copies of this config: one for the client machine and one for the server machine. They share the same schema but may differ in timing parameters and log paths.

### Paths and Timing

```yaml
zmq_pathdir: "/path/to/experiment-out/zmq"
slo_timeout_ms: 100
junk_tx_loop_interval_ms: 100
logging_interval_ms: 500
init_allocation: 100000000.0
bw_update_interval_ms: 500
bw_polling_interval_ms: 200
max_junk_payload_Mb: 0.5
junk_restart_interval_ms: 500
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `zmq_pathdir` | **Yes** | Base directory for all ZMQ IPC socket files. Must match the path prefix used in all `ipc://` socket paths in client and server configs. This directory must exist before starting |
| `slo_timeout_ms` | Maybe | Timeout for dropping stale queued frames in the QUIC send loop (ms). Should be aligned with `SLO_TIMEOUT` in the client config. Frames older than this are silently dropped from the LIFO queue |
| `junk_tx_loop_interval_ms` | No | Interval between junk service transmissions (ms). Lower values probe bandwidth more aggressively. Cloud config uses `7`, client config uses `100` |
| `logging_interval_ms` | No | How often network statistics are logged to Parquet (ms) |
| `init_allocation` | No | Initial per-service bandwidth allocation in bytes/sec, used before the first LP solver run completes. Default `100000000` (100 MB/s) is intentionally high to avoid dropping early frames |
| `bw_update_interval_ms` | No | How often the QUIC client sends bandwidth/RTT updates to the BandwidthAllocator (ms) |
| `bw_polling_interval_ms` | No | How often the BandwidthAllocator polls for new updates (ms) |
| `max_junk_payload_Mb` | No | Bandwidth limit for the junk service in Mb/s (not payload size per send). Caps the bandwidth consumed by probing traffic |
| `junk_restart_interval_ms` | No | How long the junk service waits before restarting transmission after going idle (ms) |

### Service List

```yaml
services: [1, 2, 3, 4]
enable_junk_service: True
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `services` | Yes (if adding/removing services) | List of all service IDs. If `enable_junk_service` is `True`, the highest ID is the junk service and should not appear in the client's `service_id_list` |
| `enable_junk_service` | Maybe | If `True`, the last service in the list sends dummy data to probe available bandwidth. Recommended `True` for accurate bandwidth estimation |

### Logging Paths

```yaml
quic_client_log_path: "/path/to/experiment-out/quic-client-out"
quic_server_log_path: "/path/to/experiment-out/quic-server-out"
```

| Parameter | Must customize? | Description |
|-----------|:-:|---|
| `quic_client_log_path` | **Yes** | Output directory for QUIC client Parquet logs. Must exist and be writable |
| `quic_server_log_path` | **Yes** | Output directory for QUIC server Parquet logs. Must exist and be writable |

### Logging Flags (Client-side QUIC)

```yaml
client_enable_bw_stat_log: True
client_enable_allocation_stat_log: True
client_enable_network_stat_log: True
client_enable_incoming_image_context_log: True
client_enable_outgoing_image_context_log: True
```

Controls which Parquet log files the QUIC client produces. Set to `False` to disable specific logs and reduce disk I/O.

### Logging Flags (Server-side QUIC)

```yaml
server_enable_bw_stat_log: True
server_enable_allocation_stat_log: True
server_enable_network_stat_log: True
server_enable_incoming_image_context_log: True
server_enable_outgoing_image_context_log: True
```

**Note:** `server_enable_bw_stat_log` and `server_enable_allocation_stat_log` exist for structural symmetry but do not produce logs on the server side (bandwidth refresh and allocation only run on the client).

### Log Buffer Capacities

```yaml
image_context_log_capacity: 100
bw_stat_log_capacity: 100
allocation_stat_log_capacity: 100
network_stat_log_capacity: 100
```

Max records held in memory before flushing to Parquet, per log type. Higher values reduce I/O frequency at the cost of more memory and potential data loss on crash.

---

## Configuration Checklist

When deploying the system, work through this checklist to ensure all paths and parameters are set correctly.

### 1. Choose an output directory

All components write logs and IPC socket files to a shared output directory tree. Create it first:

```bash
mkdir -p ~/experiment-out/{zmq,client,server,quic-client-out,quic-server-out}
```

Then update all path references in all four config files to use your chosen directory.

### 2. Client-side (`client_config.yaml`)

- [ ] `logging_config_filepath` points to `config/logging_config.yaml` (absolute path)
- [ ] `DST_IP` is set to your cloud server's public IP address
- [ ] All `ipc://` socket paths use your output directory (e.g., `ipc:///home/you/experiment-out/zmq/...`)
- [ ] `client_savedir` and `camera_savedir` point to writable log directories
- [ ] `parquet_eval_dir` points to the directory containing pre-computed utility curve Parquet files
- [ ] `model_info_csv_path` points to the `experiment_model_info.csv` file
- [ ] `camera_stream_config_list[].usb_id` matches your USB camera device IDs (run `ls /dev/video*`)
- [ ] `camera_np_size` matches your camera's resolution (default `[1080, 1920, 3]` for 1080p)
- [ ] Number of entries in `main_client_config_list`, `camera_stream_config_list`, and `bandwidth_allocator_config.outgoing_zmq_client_socknames` all match
- [ ] `service_id` values are consistent across client, camera, plotter, bandwidth allocator, and QUIC configs
- [ ] If testing without cameras: set `mock_camera_image_path` to a JPEG file path

### 3. Server-side (`server_config_gcloud.yaml`)

- [ ] `logging_config_filepath` points to `config/logging_config.yaml` (absolute path)
- [ ] `server_model_list[].checkpoint_path` points to valid EfficientDet `.ckpt` files (see [MODELS.md](MODELS.md))
- [ ] `server_config_list[].device` assigns a unique GPU to each service (e.g., `cuda:0`, `cuda:1`, `cuda:2`)
- [ ] All `ipc://` socket paths use the same base directory as the QUIC config's `zmq_pathdir`
- [ ] `server_log_savedir` points to a writable log directory
- [ ] Number of entries in `server_config_list` matches the number of real services (not including junk)
- [ ] If testing without a GPU: set `mock_inference_output_path` to a `.npz` file path

### 4. QUIC configs (`quic_config_client.yaml` and `quic_config_gcloud.yaml`)

- [ ] `zmq_pathdir` matches the base directory used in all `ipc://` paths in client and server configs
- [ ] `services` list includes all active service IDs plus the junk service (e.g., `[1, 2, 3, 4]`)
- [ ] `quic_client_log_path` and `quic_server_log_path` point to writable directories
- [ ] `slo_timeout_ms` is aligned with `SLO_TIMEOUT` in the client config
- [ ] Both client and server QUIC configs have the same `services` list

### 5. Cross-config consistency

- [ ] The `zmq_pathdir` in QUIC configs matches the path prefix of all `ipc://` sockets in client and server configs
- [ ] The `services` list in QUIC configs includes all `service_id` values from client and server configs, plus the junk service
- [ ] The `SLO_TIMEOUT` (client) and `slo_timeout_ms` (QUIC) are aligned
- [ ] SHM filenames in client config match what the QUIC binaries expect (follow the naming convention `client-service{N}-incoming-shm` / `client-service{N}-outgoing-shm`)
- [ ] SHM filenames in server config follow the convention `server-service{N}-outgoing-shm` / `server-service{N}-incoming-shm`
