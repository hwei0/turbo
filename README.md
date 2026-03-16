# TURBO: Utility-Aware Bandwidth Allocation for Cloud-Augmented Autonomous Control
Peter Schafhalter∗, Alexander Krentsel∗, Hongbo Wei, Joseph E. Gonzalez, Sylvia Ratnasamy (UC Berkeley), Scott Shenker (UC Berkeley and ICSI), Ion Stoica (UC Berkeley).

This repository is the official codebase for the following [NINeS 2026](https://nines-conference.org) conference paper:

[**TURBO: Utility-Aware Bandwidth Allocation for Cloud-Augmented Autonomous Control.**](https://nines-conference.org/papers/p018-Schafhalter.pdf)
Peter Schafhalter∗, Alexander Krentsel∗, Hongbo Wei, Joseph E. Gonzalez, Sylvia Ratnasamy (UC Berkeley), Scott Shenker (UC Berkeley and ICSI), Ion Stoica (UC Berkeley).

For a talk given on this research project, see this [video presentation](https://www.youtube.com/watch?v=s0t4W24dEN8).


This repository contains a research prototype system that optimizes object detection accuracy for autonomous vehicles by dynamically allocating bandwidth and selecting model configurations across multiple perception services based on real-time network conditions.


Developed by students at [UC Berkeley NetSys Lab](https://netsys.cs.berkeley.edu/).

## Overview

![alt text](docs/runtime_allocation_example.png "Example of Runtime Bandwidth Allocation")

An autonomous vehicle running multiple camera-based perception services faces a fundamental challenge: **how to maximize detection accuracy when offloading inference to the cloud over a bandwidth-constrained network**?

This system solves that problem through:

- **High-performance QUIC transport**: s2n-quic with BBR congestion control enables efficient, multiplexed data transfer between AV and cloud
- **Utility-based bandwidth allocation**: Linear programming solver runs every 500ms to optimally allocate bandwidth across services, maximizing total detection accuracy
- **Adaptive model selection**: Dynamically switches between EfficientDet variants (D1-D7x) and compression strategies based on real-time network conditions
- **SLO-aware processing**: LIFO queue management and timeout enforcement ensure only fresh, timely detection results are used for driving decisions

**Key Features:**

✅ **Multi-camera support** — Simultaneous perception from multiple USB cameras (FRONT, FRONT_LEFT, FRONT_RIGHT)

✅ **LP-based bandwidth allocation** — Utility optimization solver runs every 500ms to maximize detection accuracy

✅ **High-performance QUIC transport** — s2n-quic (Rust) with BBR congestion control for efficient network utilization

✅ **LIFO queue management** — Prioritizes fresh frames, dropping stale data to meet latency SLOs

✅ **Zero-copy IPC** — Shared memory + ZeroMQ for efficient data transfer between components

✅ **Adaptive model selection** — Dynamically switches between 5 EfficientDet variants (D1-D7x) and compression strategies

✅ **Real-time monitoring** — Web dashboard with bandwidth allocation, service status, and network utilization plots

✅ **Comprehensive logging** — Structured Parquet output for experiment analysis and reproducibility

## System Architecture

TURBO is a distributed system with two main components:

### Client Side (Autonomous Vehicle)

Running on the AV's onboard computer (e.g., NVIDIA Jetson):

- **Camera Streams** — Capture frames from multiple USB cameras (FRONT, FRONT_LEFT, FRONT_RIGHT)
- **Client Processes** — One per camera, handles image preprocessing and compression based on allocated model configuration
- **Bandwidth Allocator** — Runs a linear programming solver every 500ms to determine optimal bandwidth allocation and model selection for each service
- **QUIC Client** — High-performance Rust binary that manages per-service bidirectional streams, enforces bandwidth limits, and implements LIFO queue management
- **Ping Handler** — Measures network RTT to the cloud server using ICMP pings

### Server Side (Cloud)

Running on a GPU-equipped cloud instance (e.g., H100):

- **QUIC Server** — Rust binary that receives image data over multiplexed QUIC streams
- **Model Servers** — One per service, runs EfficientDet inference on GPU and returns detection results

### How They Work Together

```
┌─────────────── AV (Client) ───────────────┐       ┌────── Cloud (Server) ──────┐
│                                            │       │                            │
│  Camera → Client → QUIC Client             │       │  QUIC Server → ModelServer │
│  Camera → Client → QUIC Client             │──QUIC─│  QUIC Server → ModelServer │
│  Camera → Client → QUIC Client             │       │  QUIC Server → ModelServer │
│              ↑                             │       │                            │
│         Bandwidth Allocator                │       └────────────────────────────┘
│         (LP Solver + RTT)                  │
│                                            │
└────────────────────────────────────────────┘
```

**Key workflow:**
1. Cameras continuously capture frames and place them in shared memory
2. Each Client reads frames, applies preprocessing/compression according to its assigned model configuration, and sends to QUIC Client
3. QUIC Client manages per-service streams with bandwidth enforcement and LIFO queuing, transmitting over QUIC to the cloud
4. QUIC Server receives images and forwards to ModelServers for GPU inference
5. ModelServers return detection results (bounding boxes, scores) back through QUIC
6. Bandwidth Allocator monitors network conditions (bandwidth from QUIC, RTT from pings) and runs LP solver to update model configurations

For detailed architecture, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick Start (Docker with Pre-Built Images) — Recommended

The recommended way to run TURBO is with Docker using pre-built container images published on [DockerHub](https://hub.docker.com/u/hbwei). The Docker setup automatically orchestrates all processes — 2 on the server (QUIC server + model servers) and 3 on the client (client orchestrator + web dashboard + QUIC client) — handling startup ordering, ZMQ socket management, and inter-process communication for you.

> **Other setup methods:** To build Docker images from source instead of using pre-built images, see [Alternative 1: Docker Building from Source](#alternative-1-docker-building-from-source). To run without Docker at all, see [Alternative 2: Manual Setup](#alternative-2-manual-setup-without-docker).

### Prerequisites

- [Docker Engine](https://docs.docker.com/engine/install/) 24.0+ with the [Docker Compose V2 plugin](https://docs.docker.com/compose/install/linux/) (see notes below). **Avoid Docker 28** — it seems to be incompatible with `nf_tables` for iptables, which can cause networking issues with container-to-container communication. Docker 27 is recommended.
- [NVIDIA GPU drivers](https://www.nvidia.com/en-us/drivers/) and [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) (**server only** — needed for GPU inference; not required on the client, or if using [mock inference mode](#mock-modes))
- USB webcams (**client only** — or use [mock camera mode](#mock-modes) for testing without cameras)
- Linux (tested on Ubuntu 20.04+)
- Disk space:
  - **Client:** ~50 GB (7 GB for evaluation data; 10 GB for Docker images; ~30 GB recommended minimum for experiment output). Note: evaluation data is required even in [mock camera mode](#mock-modes) — the bandwidth allocator always needs it for utility curve computation.
  - **Server:** ~13 GB (2 GB for model checkpoints; 10 GB for Docker images; 1 GB recommended for experiment output). With [mock inference mode](#mock-modes), model checkpoints are not needed, reducing this to ~11 GB.

> **No GPU?** You can run the server side without a GPU by setting `MOCK_INFERENCE=true` in your `.env` file and omitting the `-f compose.gpu.yaml` override. This skips GPU model loading and returns pre-recorded detection results. See [Mock Modes](#mock-modes) for details. If using mock inference, you can skip the NVIDIA Container Toolkit prerequisite and the model checkpoint download (step 2), but you must still set `EFFDET_MODELS_DIR` to a valid (possibly empty) directory since Docker bind-mounts it unconditionally — e.g., `mkdir -p ~/av-models`.

> **Docker Compose V2:** This project requires the Docker Compose V2 *plugin* (the `docker compose` subcommand), not the legacy standalone `docker-compose` binary. If `docker compose version` shows an error or is not found, install the plugin following the [official instructions](https://docs.docker.com/compose/install/linux/). On Ubuntu: `sudo apt-get install docker-compose-plugin`.

Verify your setup:
```bash
docker compose version   # should be v2.20+

# Server only — skip these if running client only or using mock inference
nvidia-smi               # should show your GPU(s)
docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi  # GPU in Docker
```

### Setup

> **Running on separate machines?** If you are running the client and server on different hosts, perform steps 1, 4, and 5 on **both** machines. Step 2 is server-only and step 3 is client-only.

1. **Clone the repository (both client and server):**
   ```bash
   git clone https://github.com/NetSys/turbo.git
   cd turbo
   ```

2. <a id="model-setup"></a>**Download fine-tuned EfficientDet model checkpoints (server only):**

   The system uses custom EfficientDet models (D1, D2, D4, D6, D7x) fine-tuned on the [Waymo Open Dataset](https://waymo.com/open/) for 5-class object detection (vehicle, pedestrian, cyclist, sign, unknown).

   Our fine-tuned models can be downloaded and extracted as follows:

   ```bash
   # Download the model archive
   wget https://storage.googleapis.com/turbo-nines-2026/av-models.zip

   # Extract to your home directory (creates ~/av-models/)
   unzip av-models.zip -d ~
   ```

   See [docs/MODELS.md](docs/MODELS.md) for detailed model information.

   > **IMPORTANT — Waymo Open Dataset License Notice**
   >
   > The fine-tuned EfficientDet model weights provided above were developed using the [Waymo Open Dataset](https://waymo.com/open/) and are released under the [Waymo Dataset License Agreement for Non-Commercial Use](https://waymo.com/open/terms/). By downloading or using these model weights, you agree that:
   >
   > 1. These models are for **non-commercial use only**. Any use, modification, or redistribution is subject to the terms of the [Waymo Dataset License Agreement for Non-Commercial Use](https://waymo.com/open/terms/), including the non-commercial restrictions therein.
   > 2. Any further downstream use or modification of these models is subject to the same agreement.
   > 3. A statement of the applicable Waymo Dataset License terms is included in this repository at [WAYMO_LICENSE](WAYMO_LICENSE). The full agreement is available at [waymo.com/open/terms](https://waymo.com/open/terms/).
   >
   > These models were made using the Waymo Open Dataset, provided by Waymo LLC.

3. **Download pre-computed evaluation data (client only):**

   The client requires pre-computed full evaluation data for utility curve computation. Download and extract as follows:

   ```bash
   # Download the evaluation data archive
   wget https://storage.googleapis.com/turbo-nines-2026/full-eval.zip

   # Extract to your home directory (creates ~/full-eval/)
   unzip full-eval.zip -d ~
   ```

4. **Configure the `.env` file (both client and server):**

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and update the following values to match your host system:

   | Variable | Description | Quickstart value |
   |---|---|---|
   | `HOST_UID` | Your host user ID (run `id -u`) | `1000` |
   | `HOST_GID` | Your host group ID (run `id -g`) | `1000` |
   | `EXPERIMENT_OUTPUT_DIR` | Absolute path for experiment output | `~/experiment2-out` |
   | `EFFDET_MODELS_DIR` | Absolute path to model checkpoints (server) | `~/av-models` |
   | `MODEL_FULL_EVAL_DIR` | Absolute path to evaluation data (client) | `~/full-eval` |

   If you followed the download steps above, set `EFFDET_MODELS_DIR=~/av-models` and `MODEL_FULL_EVAL_DIR=~/full-eval`. Most other settings (networking, ports) work out of the box for same-host testing. See [docker/README.Docker.md](docker/README.Docker.md#additional-configuration) for the full configuration reference.

   > **Hardware-specific YAML config changes:** Depending on your hardware, you may also need to edit the Docker YAML config files in [`docker/config/`](docker/config/):
   > - **GPU device assignment (server):** [`docker/config/server_config_gcloud_docker.yaml`](docker/config/server_config_gcloud_docker.yaml) assigns each of the 3 model services to a separate GPU (`cuda:0`, `cuda:1`, `cuda:2`). If you have fewer than 3 GPUs, update the `device` fields to match your setup (e.g., set all to `"cuda:0"` for a single-GPU machine).
   > - **USB camera IDs (client):** [`docker/config/client_config_docker.yaml`](docker/config/client_config_docker.yaml) maps cameras to USB device IDs (`usb_id: 0`, `4`, `8`). If using real cameras (not mock mode), update these to match your system's device IDs.

5. **Create the experiment output directory (both client and server):**
   ```bash
   mkdir -p ~/experiment2-out
   ```

### Running

**Pull the pre-built images:**
```bash
docker compose --profile client --profile server pull
```

**GPU setup:** If the server host has NVIDIA GPUs (required for real inference, not needed for [mock inference](#mock-modes)), include the GPU override file by adding `-f compose.gpu.yaml` to all `docker compose` commands. Non-GPU hosts can omit it.

**Run both client and server on the same host (with GPU):**
```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile client --profile server up
```

**Run server only** (e.g., on a cloud GPU machine):
```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile server up
```

**Run client only** (when server is running elsewhere — update `QUIC_CLIENT_REMOTE_ADDR` in `.env` to the server's IP):
```bash
docker compose --profile client up
```

**Run server with mock inference (no GPU needed):**
```bash
docker compose --profile server up
```

Once the client is running, open the monitoring dashboard at **http://localhost:5000**.

**Shut down:**
```bash
docker compose --profile client --profile server down -v
```

The `-v` flag removes ephemeral volumes (ZMQ sockets, health signals), giving you a clean slate for the next run.

**Experiment output** will be logged to Parquet files in the configured output directory (default: `~/experiment2-out/`).

For troubleshooting, architecture details, and development workflows, see [docker/README.Docker.md](docker/README.Docker.md).

---

## Alternative 1: Docker Building from Source

If you want to build the Docker images locally instead of using the pre-built images (e.g., for development or customization), see [docker/README.Docker.md](docker/README.Docker.md#building-from-source) for full setup, build, and run instructions. The `docker/` directory contains its own `compose.yaml`, `.env.example`, and Dockerfiles.

---

## Alternative 2: Manual Setup (without Docker)

> **This approach is discouraged.** Manual setup requires installing all dependencies (Python, Rust, system libraries) by hand on both client and server machines, carefully managing process startup order, and manually cleaning up ZMQ sockets and shared memory between runs. The Docker-based methods above handle all of this automatically. Use manual setup only if you have a specific reason to avoid Docker.

<details>
<summary>Click to expand manual setup instructions</summary>

Follow the steps below to run each process directly on your host without Docker.

### Prerequisites

**Client (AV) side:**
- Python 3.10; preferably managed via [uv](https://docs.astral.sh/uv/) (alternatively, via [Anaconda](https://anaconda.org/), specifically the [`Miniconda3-py310_25.11.1-1` release version on this page](https://repo.anaconda.com/miniconda/))
- [Rust](https://rust-lang.org/tools/install/) 1.85+ (for QUIC transport)
- USB webcams (or video sources)
- Needed dependencies for `OpenCV` -- (e.g. `sudo apt-get update && sudo apt-get install ffmpeg libsm6 libxext6`)

**Server (Cloud) side:**
- Python 3.10; preferably managed via [uv](https://docs.astral.sh/uv/) (alternatively, via [Anaconda](https://anaconda.org/), specifically the [`Miniconda3-py310_25.11.1-1` release version on this page](https://repo.anaconda.com/miniconda/))
- CUDA-capable GPU (tested on H100, A100)
- PyTorch 2.0+
- [Rust](https://rust-lang.org/tools/install/) 1.85+ (for QUIC transport)
- Needed dependencies for `OpenCV` -- (e.g. `sudo apt-get update && sudo apt-get install ffmpeg libsm6 libxext6`)

### Installation

1. **Install dependencies:**
   ```bash
   cd turbo
   uv sync
   ```

   <details>
   <summary>Alternative: using pip</summary>

   ```bash
   pip install .
   ```
   </details>

2. **Download model checkpoints and evaluation data** — follow steps 2 and 3 from the [Quick Start](#quick-start-docker-with-pre-built-images--recommended) above.

   After extraction, update the checkpoint paths in your server configuration file (`config/server_config_gcloud.yaml`) and model config (`src/python/model_server/model_config.yaml`) to point to the extracted checkpoint files. Also ensure the `full_eval_dir` path in `config/client_config.yaml` points to the extracted `~/full-eval/` directory.

3. **Generate SSL Keys for QUIC:**
   ```bash
   cd src/quic
   uv run generate_cert.py
   ```

   <details>
   <summary>Alternative: using pip-installed environment</summary>

   ```bash
   python generate_cert.py
   ```
   </details>

   Make sure the same outputted files are copied to both your client and server hosting locations.

4. **Build QUIC binaries:**
   ```bash
   cd src/quic
   cargo build --release
   cd ..
   ```
   You may install the latest version of Rust [here](https://rust-lang.org/tools/install/).

5. **Configure the system:**
   - Edit [config/client_config.yaml](config/client_config.yaml) for client-side settings
   - Edit [config/server_config_gcloud.yaml](config/server_config_gcloud.yaml) for server-side settings
   - Edit [config/quic_config_client.yaml](config/quic_config_client.yaml) for QUIC transport settings

   See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for detailed configuration guide.

### Running the System

**On the server (cloud) side:**

0. Do the following pre-run steps:
   - If previous runs were done:
      - Clear all previous zeromq socket files from any previous runs, if they exist. In this example, just remove all contents of the directory containing the zeromq files:
         ```bash
            rm ~/experiment2-out/zmq/*
         ```
      - Stash the previous log outputs from any previous runs, if they exist, and make sure the directories for storing the log outputs produced by all parts of this system are empty.
   - If this is the first run:
      - Make output directories to store each of the log outputs for your current run.
      - Make output directories to store `ZeroMQ` IPC socket files.

      For reference, the author's output directory structure was created as follows:

      ```bash
         mkdir ~/experiment2-out
         mkdir ~/experiment2-out/zmq
         mkdir ~/experiment2-out/client
         mkdir ~/experiment2-out/server
         mkdir ~/experiment2-out/quic-client-out
         mkdir ~/experiment2-out/quic-server-out
      ```

1. Start the QUIC server:
   ```bash
   cd src/quic
   RUST_LOG=info cargo run --release --bin server ../../config/quic_config_gcloud.yaml ${YOUR_SERVER_INTERNAL_IP}:12345
   ```

   (or, if debugging an error, use RUST_BACKTRACE=1 instead of RUST_LOG=...)

2. Start the model servers (in a separate terminal):
   ```bash
   cd src/python
   uv run server_main.py -c ../../config/server_config_gcloud.yaml
   ```
   (or `python server_main.py ...` if using a pip-installed environment)

**On the client (AV) side:**

0. Do the following pre-run steps:
   - If previous runs were done:
      - Clear all previous zeromq socket files from any previous runs, if they exist. In this example, just remove all contents of the directory containing the zeromq files:
      ```bash
         rm ~/experiment2-out/zmq/*
      ```
      - Stash the previous log outputs from any previous runs, if they exist, and make sure the directories for storing the log outputs produced by all parts of this system are empty.

   - If this is the first run:
      - Allow ping requests (our PingHandler module needs to send pings from user-land):
         ```bash
            sudo sysctl net.ipv4.ping_group_range='0 2147483647'
         ```
      - Make output directories to store each of the log outputs for your current run.
      - Make output directories to store `ZeroMQ` IPC socket files.

**IMPORTANT:** The ordering of the following steps matters due to a behavior in ZeroMQ socket binding. See [docs/IPC.md](docs/IPC.md) for details.

1. Start the client processes (in a separate terminal):
   ```bash
   cd src/python
   uv run client_main.py -c ../../config/client_config.yaml -s <SERVER_IP:PORT>
   ```
   (or `python client_main.py ...` if using a pip-installed environment)

2. Start the web dashboard for real-time monitoring:
   ```bash
   cd src/python/web_frontend
   uv run start_web_dashboard.py --config ../../../config/client_config.yaml
   ```
   (or `python start_web_dashboard.py ...` if using a pip-installed environment)
   Then open `http://0.0.0.0:5000` in your browser.


3. Wait 20 seconds (or until you see log messages of the form `Client 2: Python waiting for Rust QUIC client handshake`), then start the QUIC client:
   ```bash
   cd src/quic
   RUST_LOG=info cargo run --release --bin client ../../config/quic_config_client.yaml ${YOUR_SERVER_EXTERNAL_IP}:12345
   ```

   (or, if debugging an error, use RUST_BACKTRACE=1 instead of RUST_LOG=...)

**Experiment output** will be logged to Parquet files in the configured output directories (default: `~/experiment2-out/`).

</details>

## Mock Modes

TURBO supports two independent mock modes for testing and development without requiring physical cameras or GPUs:

- **Mock Camera** (client-side): Replaces live USB camera capture with a static image. Note that the `full-eval` evaluation data is still required — the bandwidth allocator uses it for utility curve computation regardless of camera mode.
- **Mock Inference** (server-side): Skips GPU model loading and returns pre-recorded detection results. Optionally simulates per-model inference latency using a CSV of benchmark timings. The `av-models` model checkpoints are **not needed** in this mode (model loading is completely bypassed), though the Docker bind mount for `EFFDET_MODELS_DIR` still requires an existing directory — an empty one is fine.

### Enabling Mock Modes

Follow the instructions below that correspond to the setup method you used — [Quick Start](#quick-start-docker-with-pre-built-images--recommended), [Alternative 1](#alternative-1-docker-building-from-source), or [Alternative 2](#alternative-2-manual-setup-without-docker).

#### Quick Start: Docker with Pre-Built Images

Set environment variables in `.env` (at the repo root):

```bash
# Set to any non-empty value (e.g. "true") to enable, leave empty to disable
MOCK_CAMERA=true
MOCK_INFERENCE=true
```

Then run as usual — for example, to run both client and server in full mock mode (no cameras, no GPU):

```bash
docker compose --profile client --profile server up
```

Omit `-f compose.gpu.yaml` when using mock inference, since no GPU is needed.

#### Alternative 1: Docker Building from Source

Set environment variables in `docker/.env`:

```bash
# Set to any non-empty value (e.g. "true") to enable, leave empty to disable
MOCK_CAMERA=true
MOCK_INFERENCE=true
```

Then build and run from the `docker/` directory:

```bash
cd docker
docker compose --profile client --profile server up --build
```

See [docker/README.Docker.md](docker/README.Docker.md#mock-modes) for details.

#### Alternative 2: Manual Setup (without Docker)

Pass CLI flags to the Python entry points:

```bash
# Client-side: mock camera
uv run client_main.py -c ../../config/client_config.yaml -s <SERVER_IP:PORT> --mock-camera

# Server-side: mock inference
uv run server_main.py -c ../../config/server_config_gcloud.yaml --mock-inference
```

### Mock File Configuration

The YAML config files specify which mock files to use; the CLI flag (or Docker env var) controls whether they are actually applied.

- **Mock camera image:** Configured per camera in `camera_stream_config_list` via the `mock_camera_image_path` key. A sample mock image is included at `src/python/camera_stream/mock_webcam_image.jpg`. Without mock camera enabled, this path is ignored and real USB cameras are used.
- **Mock inference output:** Configured per server in `server_config_list` via `mock_inference_output_path` (numpy array of detections) and `mock_model_latency_csv_path` (per-model latency benchmarks). A sample mock output is included at `src/python/camera_stream/example_effdet_d4_output.npy`. Without mock inference enabled, these paths are ignored and real GPU inference is used.

### Combining Mock Modes

The two mock modes are fully independent — you can use any combination:

| Camera Mock | Inference Mock | Use Case | Data Required |
|---|---|---|---|
| Off | Off | **Production** — real cameras, real GPU inference | `full-eval` + `av-models` |
| On | Off | Test the full pipeline without cameras (still needs GPU) | `full-eval` + `av-models` |
| Off | On | Test camera capture and transport without GPU | `full-eval` only |
| On | On | **Full mock** — test the entire system without cameras or GPU | `full-eval` only |

## Documentation

- **[Model Setup & Reference](docs/MODELS.md)** - EfficientDet model download, configuration, and inference details
- **[System Architecture](docs/ARCHITECTURE.md)** - Detailed technical architecture, problem setup, bandwidth solver, and end-to-end walkthrough
- **[Configuration Guide](docs/CONFIGURATION.md)** - Complete configuration file reference
- **[Experiment Logging](docs/LOGGING.md)** - Parquet output file formats and logging reference
- **[IPC Reference](docs/IPC.md)** - Inter-process communication protocols (ZMQ, shared memory)

### Key Concepts

**Model Configurations:**
Each configuration is identified by a string like `edd4-imgcomp50-inpcompNone`, specifying:
- EfficientDet variant (D1, D2, D4, D6, D7x)
- Image compression strategy (JPEG quality, PNG, or none)
- Input preprocessing compression

See [docs/ARCHITECTURE.md#model-configurations](docs/ARCHITECTURE.md#model-configurations) for details.

**Utility Curves:**
The system pre-computes step functions mapping available bandwidth → achievable detection accuracy (mAP) for each model configuration under given network conditions.

**Bandwidth Solver:**
An LP-based allocator runs every 500ms to select the optimal (model, compression) configuration for each service, maximizing total utility subject to bandwidth and SLO constraints.

## Directory Structure

```
turbo/
├── compose.yaml                     # Docker Compose for pre-built images (recommended)
├── .env.example                     # Template for configurable paths and settings
├── docker/                          # Building Docker images from source
│   ├── compose.yaml                 # Docker Compose for building from source
│   ├── .env.example                 # Template for build-from-source settings
│   ├── config/                      # Docker-specific YAML configs (shared by both workflows)
│   ├── Dockerfile_turbo_*           # Multi-stage Dockerfiles
│   └── README.Docker.md             # Build-from-source docs, configuration reference, troubleshooting
├── src/
│   ├── python/
│   │   ├── client_main.py           # Client-side process orchestrator
│   │   ├── client.py                # Per-service client (preprocessing, QUIC I/O)
│   │   ├── server_main.py           # Server-side process orchestrator
│   │   ├── server.py                # Per-service model server (EfficientDet inference)
│   │   ├── bandwidth_allocator.py   # LP-based bandwidth allocation solver
│   │   ├── utility_curve_stream/    # Utility curve computation framework
│   │   ├── camera_stream/           # USB camera capture
│   │   ├── ping_handler/            # ICMP RTT measurement
│   │   ├── model_server/            # EfficientDet model loading
│   │   ├── util/                    # Shared utilities (plotting, logging)
│   │   └── web_frontend/            # Real-time web dashboard
│   └── quic/                        # QUIC transport layer (Rust)
│       ├── quic_client/             # Client binary
│       ├── quic_server/             # Server binary
│       └── quic_conn/               # Shared library (bandwidth management, logging)
├── config/                          # YAML configuration files (manual setup)
└── docs/                            # Detailed documentation
```

## Technologies

- **QUIC Transport:** s2n-quic (Rust) with BBR congestion control
- **IPC:** ZeroMQ for control messages; POSIX shared memory for image data
- **Object Detection:** EfficientDet (D1-D7x) trained on Waymo Open Dataset
- **Optimization:** PuLP linear programming solver
- **Logging:** Polars DataFrames with Parquet output
- **Visualization:** Flask + WebSocket dashboard with matplotlib


## Roadmap

Planned features and improvements, in addition to accepted GitHub Issues/PRs:

- [x] Docker deployment configuration
- [x] Graceful termination of python services
- [x] Graceful handling of Ctrl-C in rust processes (to kill all zmq sockets and shm files, and avoid parquet data loss)
- [ ] Migration to full Rust implementation with Rust Python+numpy bindings;
      - eliminate ZeroMQ sockets and replace with more robust IPC
- [ ] Camera streams are sometimes laggy and unreliable; migrate from OpenCV and replace with low-latency alternative
- [ ] Camera streams are often miscalibrated w.r.t. brightness/exposure; fix is pending investigation
- [ ] Logging for some sub-processes is broken and/or unclear in Rust and Python; fix is pending investigation

## Contributing

We welcome contributions from the community! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Ways to contribute:**
- Report bugs and request features via [GitHub Issues](https://github.com/NetSys/turbo/issues)
- Submit pull requests for bug fixes and enhancements
- Improve documentation and add tutorials
- Share your deployment experiences and use cases

## License

This project's source code is licensed under the Apache 2.0 License — see the [LICENSE](LICENSE) file for details.

The fine-tuned EfficientDet model weights distributed with this project were developed using the [Waymo Open Dataset](https://waymo.com/open/) and are subject to the [Waymo Dataset License Agreement for Non-Commercial Use](https://waymo.com/open/terms/). These model weights are provided for **non-commercial purposes only**. Any use, modification, or redistribution of the model weights must comply with the Waymo Dataset License Agreement, including the non-commercial restrictions of Section 4. A statement of the applicable Waymo Dataset License terms is included at [WAYMO_LICENSE](WAYMO_LICENSE); the full agreement is available at [waymo.com/open/terms](https://waymo.com/open/terms/).

These models were made using the Waymo Open Dataset, provided by Waymo LLC.

## Citation

If you use this system in your research, please cite:

```bibtex
@article{Schafhalter_Krentsel_Wei_Gonzalez_Ratnasamy_Shenker_Stoica_2026, title={TURBO: Utility-Aware Bandwidth Allocation for Cloud-Augmented Autonomous Control}, journal={New Ideas in Networked Systems Conference}, author={Schafhalter, Peter and Krentsel, Alex and Wei, Hongbo and Gonzalez, Joseph E and Ratnasamy, Sylvia and Shenker, Scott and Stoica, Ion}, year={2026}} 
```
## Contact

For questions and feedback, open a [GitHub Issue](https://github.com/NetSys/turbo/issues).


