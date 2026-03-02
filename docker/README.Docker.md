# Docker: Building from Source & Reference

This directory contains everything needed to **build Docker images from source** — Dockerfiles, a build-oriented `compose.yaml`, and shared configuration files. It is intended for development and customization, **not** for running pre-built images.

- **To run TURBO using pre-built images** (recommended), use the root-level `compose.yaml` and `.env.example`. See the [Quick Start (Docker)](../README.md#quick-start-docker--recommended) section in the main README.
- **To build images from source**, use the `compose.yaml` and `.env.example` in this directory. See [Building from Source](#building-from-source) below.

The `docker/config/` directory contains Docker-specific YAML configs shared by both workflows.

## Building from Source

If you want to build the Docker images locally instead of using the pre-built images (e.g., for development or customization), follow these steps.

### Prerequisites

- [Docker Engine](https://docs.docker.com/engine/install/) 24.0+ with [Docker Compose V2](https://docs.docker.com/compose/install/)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) (**server only** — needed for GPU inference; not required on the client, or if using [mock inference mode](../README.md#mock-modes))
- USB webcams (**client only** — or use [mock camera mode](../README.md#mock-modes) for testing without cameras)
- Linux (tested on Ubuntu 20.04+)

See the main README's [Prerequisites](../README.md#prerequisites) section for Docker version warnings, disk space requirements, and setup verification commands.

### Setup

> **Running on separate machines?** If you are running the client and server on different hosts, perform all steps on **both** machines. Within step 1, the model download is server-only and the eval data download is client-only.

1. **Complete shared setup steps:**

   Follow **steps 1–3** from the [Quick Start Setup](../README.md#setup) in the main README to clone the repository, download model checkpoints (server only), and download evaluation data (client only).

2. **Generate SSL keys for QUIC (both client and server):**

   The QUIC binaries embed SSL certificates at compile time via Rust's `include_str!()` macro (see [SSL Certificates](#ssl-certificates)). You must generate them before building:

   ```bash
   cd src/quic
   pip install cryptography   # if not already installed
   python generate_cert.py
   cd ../..
   ```

3. **Configure the `.env` file (both client and server):**

   ```bash
   cp docker/.env.example docker/.env
   ```

   Edit `docker/.env` and update the following values to match your host system:

   | Variable | Description | Default |
   |---|---|---|
   | `HOST_UID` | Your host user ID (run `id -u`) | `1000` |
   | `HOST_GID` | Your host group ID (run `id -g`) | `1000` |
   | `EXPERIMENT_OUTPUT_DIR` | Absolute path for experiment output | (must set) |
   | `EFFDET_MODELS_DIR` | Absolute path to model checkpoints (server) | (must set) |
   | `MODEL_FULL_EVAL_DIR` | Absolute path to evaluation data (client) | (must set) |

   Most other settings (networking, ports, SSL paths) work out of the box for same-host testing. See [Additional Configuration](#additional-configuration) below for the full reference.

4. **Create the experiment output directory (both client and server):**
   ```bash
   mkdir -p ~/experiment2-out
   ```

### Build and Run

All Docker commands should be run from the `docker/` directory:
```bash
cd docker
```

**GPU setup:** If the server host has NVIDIA GPUs (required for real inference, not needed for [mock inference](../README.md#mock-modes)), include the GPU override file by adding `-f compose.gpu.yaml` to all `docker compose` commands. Non-GPU hosts can omit it.

**Build and run both client and server on the same host (with GPU):**
```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile client --profile server up --build
```

**Build and run server only** (e.g., on a cloud GPU machine):
```bash
docker compose -f compose.yaml -f compose.gpu.yaml --profile server up --build
```

**Build and run client only** (when server is running elsewhere — update `QUIC_CLIENT_ADDR` in `.env` to the server's IP):
```bash
docker compose --profile client up --build
```

**Build and run server with mock inference (no GPU needed):**
```bash
docker compose --profile server up --build
```

Once the client is running, open the monitoring dashboard at **http://localhost:5000**.

**Shut down:**
```bash
docker compose --profile client --profile server down -v
```

The `-v` flag removes ephemeral volumes (ZMQ sockets, health signals), giving you a clean slate for the next run.

**Experiment output** will be logged to Parquet files in the configured output directory (default: `~/experiment2-out/`).

## Services Overview

The Compose file defines seven services organized into two profiles (`client` and `server`):

| Service | Profile | Description |
|---|---|---|
| `rust_base` | client, server | Builds QUIC client/server Rust binaries (base image, not run directly) |
| `python_base` | client, server | Builds Python environment with dependencies (base image, not run directly) |
| `quic_client` | client | QUIC transport client (Rust) |
| `client_python_main` | client | Client orchestrator — camera streams, bandwidth allocator, LP solver |
| `client_python_monitor` | client | Web dashboard for real-time monitoring (Flask) |
| `quic_server` | server | QUIC transport server (Rust) |
| `server_python_main` | server | Model servers — runs EfficientDet inference on GPU |

Services start in dependency order via health checks: `client_python_main` must be healthy before `client_python_monitor` starts, and `client_python_monitor` must be healthy before `quic_client` starts.

## Additional Configuration

The [Setup](#setup) section above covers the required `.env` variables (`HOST_UID`, `HOST_GID`, `EXPERIMENT_OUTPUT_DIR`, `EFFDET_MODELS_DIR`, `MODEL_FULL_EVAL_DIR`). The following additional variables are also available:

**SSL (usually no changes needed):**

| Variable | Description | Default |
|---|---|---|
| `SSL_KEY_PATH` | Path to QUIC SSL key (relative to repo root) | `./src/quic/ssl_key.pem` |
| `SSL_CERT_PATH` | Path to QUIC SSL cert (relative to repo root) | `./src/quic/ssl_cert.pem` |

**Networking (usually no changes needed for same-host testing):**

| Variable | Description | Default |
|---|---|---|
| `QUIC_CLIENT_ADDR` | Address the QUIC client connects to | `10.64.89.1:12345` (Docker bridge gateway) |
| `QUIC_SERVER_ADDR` | Address the QUIC server binds to | `0.0.0.0:12345` |
| `QUIC_SERVER_PORT` | UDP port exposed for QUIC | `12345` |
| `DASHBOARD_PORT` | Host port for the web dashboard | `5000` |

When running client and server on the **same host**, the default `QUIC_CLIENT_ADDR` of `10.64.89.1:12345` routes through the Docker bridge gateway to reach the server container. When running on **separate hosts**, set `QUIC_CLIENT_ADDR` to the server machine's routable IP and port.

### Docker-specific config files

The `docker/config/` directory contains YAML config files that mirror the main `config/` files but with container-internal paths (e.g. `/app/experiment2-out` instead of `~/experiment2-out`). These are bind-mounted into each container at runtime.

You generally don't need to edit these unless you're changing service behavior (e.g. number of cameras, model variants, SLO timeouts). If you do, edit the files in `docker/config/` — not the ones in the repo root `config/` directory.

### Mock Modes

TURBO supports mock camera and mock inference modes for testing without physical cameras or GPUs. See the [Mock Modes](../README.md#mock-modes) section in the main README for full details.

In Docker, mock modes are toggled via environment variables in `.env`:

| Variable | Effect when non-empty | Default |
|---|---|---|
| `MOCK_CAMERA` | Passes `--mock-camera` to `client_main.py` — uses static images instead of USB cameras | (empty — disabled) |
| `MOCK_INFERENCE` | Passes `--mock-inference` to `server_main.py` — returns pre-recorded detections instead of GPU inference | (empty — disabled) |

The mock data files (`mock_webcam_image.jpg`, `example_effdet_d4_output.npy`) are bundled into the container at `/app/` and their paths are pre-configured in `docker/config/`.

### Additional running commands

Beyond the commands in [Build and Run](#build-and-run), these are also useful:

**Build all images without starting** (useful after Dockerfile changes):
```bash
docker compose --profile client --profile server build
```

**Shut down without removing volumes** (keeps ZMQ sockets and health signals):
```bash
docker compose --profile client --profile server down
```

**Accessing the web dashboard:**

Once the client profile is running and all health checks pass, open the monitoring dashboard at `http://localhost:5000` (or whatever port you set for `DASHBOARD_PORT` in `.env`).

## Development Workflow

The Compose file supports [Docker Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/) for hot-reload during development:

```bash
docker compose --profile client --profile server watch
```

- **Python source changes** (`src/python/`): synced into running containers without rebuild.
- **Rust source changes** (`src/quic/`): triggers a full rebuild of the Rust base image.
- **Dependency changes** (`uv.lock`, `pyproject.toml`, `Cargo.toml`): triggers a rebuild.
- **Docker config changes** (`docker/`): triggers a rebuild.

## Architecture Notes

- **IPC mode: host** — All services use `ipc: host` so that ZeroMQ IPC sockets and POSIX shared memory segments are accessible across containers. This is required for the system's inter-process communication to work.
- **GPU access** — The Python services (`client_python_main`, `server_python_main`) request all available NVIDIA GPUs via `deploy.resources`. The server config assigns specific services to specific GPU devices (e.g. `cuda:0`, `cuda:1`).
- **tmpfs volumes** — ZeroMQ socket directories and health signal files use tmpfs-backed volumes for fast, ephemeral storage.
- **Signal handling** — All services use `init: true` (tini) as PID 1 for proper signal forwarding and graceful shutdown. A 30-second grace period (`stop_grace_period`) is configured for each service. When you press Ctrl+C on `docker compose up`, Docker Compose sends **SIGTERM** (not SIGINT) to each container. Without `init: true`, the application would be PID 1, and the Linux kernel silently drops signals with default handlers for PID 1 — causing the process to ignore SIGTERM and get SIGKILL'd after the grace period. The Python orchestrators (`client_main.py`, `server_main.py`) explicitly handle both SIGTERM and SIGINT to trigger graceful shutdown (ZMQ kill-switch broadcast, shared memory unlink, Parquet flush).
- **`exec` and `python` in Dockerfiles** — The Python Dockerfile uses `exec python` directly instead of `uv run`. `uv run` spawns Python as a child process and may not forward signals, which would prevent graceful shutdown. The venv is already on `PATH` (set in `Dockerfile_turbo_python_base`), so calling `python` directly works. The `exec` replaces the shell with the actual process, avoiding a redundant `/bin/sh` parent (optional with `init: true`, but good practice).
- **Custom network** — A bridge network (`quic_net`, subnet `10.64.89.0/24`, gateway `10.64.89.1`) is used for QUIC communication. On same-host deployments, the QUIC client reaches the server through the bridge gateway (which routes to the host, where the server's UDP port is published). On separate-host deployments, `QUIC_CLIENT_ADDR` is set to the server machine's routable IP instead.
- **QUIC uses UDP** — The server's port is published with the `/udp` protocol. Firewalls on the server host must allow inbound UDP on this port.
- **Rust QUIC client requires IP addresses** — The Rust QUIC client parses addresses with `SocketAddr` and cannot resolve DNS hostnames. `QUIC_CLIENT_ADDR` must always be an `ip:port` pair (e.g. `10.64.89.1:12345`), not a hostname.
- **Health signal synchronization** — `client_python_main` writes its health signal (`/health/client_main_ready`) only after all Client subprocesses have bound their `quic_rcv_zmq_socket`. This ensures the Rust QUIC client (which depends on this health signal via `client_python_monitor`) does not start until the Python ZMQ sockets are ready to accept connections. A `multiprocessing.Manager().Queue()` is used for this cross-process synchronization.

## Path Relativity Rules

Docker Compose uses different base directories for different path types, which can be confusing:

| Path type | Relative to | Example |
|---|---|---|
| Volume `source` | **Compose file location** (`docker/`) | `./config/client_config_docker.yaml` → `docker/config/client_config_docker.yaml` |
| Build `context` | **Compose file location** (`docker/`) | `..` → repo root |
| Build `dockerfile` | **Build context** | `./docker/Dockerfile_turbo_python_binary` → (repo root)/docker/Dockerfile_turbo_python_binary |
| Build `args` (paths like `EXECUTABLE_DIR`) | N/A (baked into image) | `./src/python` → resolved inside the container |
| Watch `path` | **Build context** | `./uv.lock` → (repo root)/uv.lock |

The `.env` file paths for volume mounts (e.g. `PYTHON_DOCKER_CLIENT_CONFIG_PATH`) must be relative to the compose file location (`docker/`), **not** the repo root.

## SSL Certificates

The QUIC binaries embed the SSL certificate and key at **compile time** via Rust's `include_str!()` macro. The `.pem` files are baked into the executable during `cargo build` and are not needed at runtime. This means the QUIC container images are self-contained — no SSL volume mounts are required.

## Troubleshooting

**"permission denied" on bind-mounted files:**
Make sure `HOST_UID` and `HOST_GID` in `.env` match your host user (`id -u` and `id -g`).

**GPU not available inside containers:**
Verify the NVIDIA Container Toolkit is installed and the Docker daemon is configured to use the `nvidia` runtime. Test with:
```bash
docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi
```

**Services failing health checks:**
Check logs for a specific service to diagnose startup issues:
```bash
docker compose --profile client logs client_python_main
```

**QUIC connection failures between client and server on separate hosts:**
Make sure `QUIC_CLIENT_ADDR` in `.env` is set to the server host's routable IP (not the Docker gateway), and that the `QUIC_SERVER_PORT` UDP port is open on the server host's firewall.

**Bind mount shows "IsADirectoryError" or creates an unexpected directory:**
When Docker bind-mounts a file but the source path doesn't exist on the host, Docker silently creates a **directory** at the target path instead of failing. This causes confusing errors like `IsADirectoryError: [Errno 21] Is a directory: '/app/python_config.yaml'`. Double-check that the source path in `.env` is correct and that the file exists. Remember that volume source paths are relative to the compose file location — see [Path Relativity Rules](#path-relativity-rules). After fixing the path, you must rebuild with `--build` since the stale directory may be cached in the image layer:
```bash
docker compose --profile server up --build --force-recreate
```

**`--force-recreate` vs `--build`:**
`--force-recreate` recreates containers but does **not** rebuild images. If a problem was baked into an image during a previous build (e.g. a directory created by a bad bind mount), you need `--build` to rebuild the image. Use both when in doubt:
```bash
docker compose --profile server up --build --force-recreate
```

**Do not use `docker compose restart`:**
`docker compose restart` stops and restarts containers but does **not** re-evaluate `depends_on` health checks. All containers restart simultaneously, bypassing the startup ordering. Always use `docker compose down && docker compose up` to ensure proper sequencing.

**iptables errors ("Chain 'DOCKER-ISOLATION-STAGE-2' does not exist"):**
This is a known issue with **Docker 28.x** on newer Linux kernels where iptables uses the `nf_tables` backend. Docker 28 changed its network isolation chain setup in a way that is incompatible with `nf_tables`. **Docker 27.5 does not have this issue.** Fixes to try in order:
1. **Downgrade to Docker 27.5** — this is the most reliable fix.
2. Restart Docker: `sudo systemctl restart docker`
3. Switch to the legacy iptables backend:
   ```bash
   sudo update-alternatives --set iptables /usr/sbin/iptables-legacy
   sudo update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
   sudo systemctl restart docker
   ```

**Stale containers after changing network config:**
If you add or modify Docker networks in `compose.yaml`, existing containers won't pick up the changes. You'll see errors like `container is not connected to the network`. Fix by recreating:
```bash
docker compose --profile client --profile server down
docker compose --profile client --profile server up
```

**Stale POSIX shared memory after ungraceful shutdown:**
Because all services use `ipc: host`, POSIX shared memory segments live in the host's `/dev/shm` and survive container restarts. If a container is force-killed (SIGKILL, OOM, Docker timeout, power loss) before cleanup runs, stale segments remain and cause `FileExistsError: [Errno 17] File exists` on the next startup. To clean them up:
```bash
# Check for stale segments
ls /dev/shm/*-shm

# Remove them (server-side example)
rm /dev/shm/server-service*-shm

# Remove them (client-side example)
rm /dev/shm/client-service*-shm
```
Under normal graceful shutdown (Ctrl+C), the Python processes unlink their shared memory segments automatically.

**Stale ZeroMQ sockets from a previous run:**
ZMQ socket directories use tmpfs volumes that start empty on every `docker compose up`, so stale sockets are not normally an issue. If you see ZMQ-related errors, shut down with `-v` to remove all tmpfs volumes:
```bash
docker compose --profile client --profile server down -v
```
