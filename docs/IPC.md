# Inter-Process Communication (IPC) Reference

This document provides detailed documentation for all IPC channels in TURBO. For a quick start, see the main [README.md](../README.md).

## Overview

This section documents every IPC channel between components. All connections use either **ZeroMQ (ZMQ)** sockets (for lightweight control/coordination messages) or **POSIX shared memory (SHM)** regions (for zero-copy bulk image data transfer). Socket names are configured in YAML config files and resolved to `ipc://` paths at runtime.

## IPC Overview Diagram

```
                          ┌─────────────────────────────────┐
                          │          MainPlotter             │
                          │      (plotting_main.py)          │
                          └───────────▲─────────────────────┘
                                      │ ZMQ PUB/SUB
                     ┌────────────────┴──────────────┐
                     │                               │
               car-client-diagnostics          car-client-diagnostics
                     │                               │
┌────────────────────┴─────┐   ┌─────────────────────┴──────────────────────┐
│     BandwidthAllocator   │   │              Client (×N)                    │
│  (bandwidth_allocator.py)│   │            (client.py)                      │
└──┬───┬───────┬───────┬───┘   └──┬──────┬─────────┬──────────┬─────────┬──┘
   │   │       │       │          │      │         │          │         │
   │   │       │       │          │      │    ZMQ REQ/REP     │    ZMQ SUB
   │   │       │       │          │      │    + SHM           │    (kill)
   │   │       │    ZMQ PUB       │   ZMQ REQ/REP  │          │         │
   │   │       │    (per-client)  │   + SHM    ZMQ REQ/REP    │    client_main.py
   │   │       │       │          │      │    + SHM           │
   │   │       │       ▼          │      │         │          │
   │   │       │    Client ◀──────┘      │         │     ZMQ SUB
   │   │       │    (bw alloc)           │         │     (bw alloc)
   │   │       │                         │         │
   │   │    ZMQ REP/REQ            ┌─────┴──┐  ┌───┴──────────────────┐
   │   │    (bw query)             │Camera  │  │  QUIC Client (Rust)  │
   │   │       │                   │Stream  │  │                      │
   │   │       ▼                   └────────┘  └──────────┬───────────┘
   │   │   QUIC Client (Rust)                             │
   │   │   (bandwidth_refresh_loop)                   QUIC Stream
   │   │                                                  │
   │   │                                      ┌───────────▼───────────┐
   │ ZMQ REQ/REP                              │  QUIC Server (Rust)   │
   │ (RTT query)                              └──────────┬────────────┘
   │   │                                            ZMQ REQ/REP
   │   ▼                                            + SHM
   │ PingHandler                                         │
   │ (ping_handler.py)                              ┌────▼─────────────┐
   │                                                │  ModelServer (×N) │
   └────────────────────────────────────────────────│  (server.py)      │
                                                    └──────────────────┘
```

## Per-Component IPC Connections

### 1. CameraDataStream (`camera_stream/camera_data_stream.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| Client | ZMQ REP (bind) | `service{N}-camera-socket` | REQ/REP | Client requests → CameraStream responds | Client sends `CameraDataRequest`, CameraStream responds with `CameraDataResponse` containing frame metadata |
| Client | SHM (create) | `service{N}-camera-shmem` | Shared read | CameraStream writes, Client reads | Raw camera frame (1080×1920×3 NumPy array) is placed in SHM by the background capture thread; Client reads it after receiving the ZMQ response |
| client_main | ZMQ SUB (bind) | `camera-kill-{N}-switch` | PUB/SUB | client_main → CameraStream | Receive "ABORT" signal for graceful shutdown |

**Config**: `camera_stream_config_list[N]` in `config/client_config.yaml`

---

### 2. Client (`client.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| CameraStream | ZMQ REQ (connect) | `service{N}-camera-socket` | REQ/REP | Client → CameraStream | Request latest camera frame; receives metadata, reads image from SHM |
| CameraStream | SHM (attach) | `service{N}-camera-shmem` | Shared read | CameraStream → Client | Read raw camera frame placed by CameraStream |
| QUIC Client | ZMQ REQ (connect) | `car-server-outgoing-{N}` | REQ/REP | Client → QUIC Client | Send compressed image + model config for remote inference. Client writes image bytes to SHM, sends size via ZMQ, waits for ACK |
| QUIC Client | SHM (create) | `client-service{N}-incoming-shm` | Shared write | Client → QUIC Client | Image payload written here by Client, read by QUIC Client's `read_local_zmq_socket` routine |
| QUIC Client | ZMQ REP (bind) | `car-server-incoming-{N}` | REQ/REP | QUIC Client → Client | Receive inference results from server. QUIC Client writes result bytes to SHM, sends size via ZMQ, Client reads from SHM and sends ACK |
| QUIC Client | SHM (create) | `client-service{N}-outgoing-shm` | Shared read | QUIC Client → Client | Inference result payload written here by QUIC Client's `read_quic_stream` routine, read by Client |
| BandwidthAllocator | ZMQ SUB (bind) | `main-client-{N}-bw-subscriber` | PUB/SUB | BandwidthAllocator → Client | Receive updated model configuration and bandwidth allocation; Client adjusts preprocessing accordingly |
| MainPlotter | ZMQ PUB (connect) | `car-client-diagnostics` | PUB/SUB | Client → MainPlotter | Send per-request status updates (timestamp, success/failure, service ID) for dashboard visualization |
| client_main | ZMQ SUB (bind) | `client-kill-{N}-switch` | PUB/SUB | client_main → Client | Receive "ABORT" signal for graceful shutdown |

**Config**: `main_client_config_list[N]` in `config/client_config.yaml`

---

### 3. QUIC Client (`quic/quic_client/`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| Client | ZMQ REQ (connect) | `car-server-outgoing-{N}` | REQ/REP | Client → QUIC Client | `read_local_zmq_socket` receives image context + size, copies payload from SHM, sends ACK |
| Client | SHM (attach) | `client-service{N}-incoming-shm` | Shared read | Client → QUIC Client | Read image payload written by Client |
| Client | ZMQ REP (bind) | `car-server-incoming-{N}` | REQ/REP | QUIC Client → Client | `read_quic_stream` writes inference result to SHM, sends size, waits for ACK from Client |
| Client | SHM (attach) | `client-service{N}-outgoing-shm` | Shared write | QUIC Client → Client | Write inference result received from QUIC Server |
| BandwidthAllocator | ZMQ REQ (bind) | `car-server-bw-service` | REQ/REP | QUIC Client → BandwidthAllocator | `bandwidth_refresh_loop` periodically sends `{bw, rtt}` (derived from CWND/RTT), receives per-service allocation and model config |
| MainPlotter | ZMQ PUB (connect) | `car-client-diagnostics` | PUB/SUB | QUIC Client → MainPlotter | `send_loop` publishes per-service network utilization stats (send rate, receive rate, allocated limit) |
| QUIC Server | QUIC stream | TCP (s2n-quic) | Bidirectional | QUIC Client ↔ QUIC Server | Multiplexed per-service bidirectional streams carrying image data and inference results |

**Config**: `quic/quic_config_*.yaml` (zmq_pathdir, services, timing, logging)

---

### 4. QUIC Server (`quic/quic_server/`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| ModelServer | ZMQ REQ (connect) | `remote-server-outgoing-{N}` | REQ/REP | QUIC Server → ModelServer | `read_quic_stream` writes received image to SHM, sends size via ZMQ, waits for ACK from ModelServer |
| ModelServer | SHM (attach) | `server-service{N}-outgoing-shm` | Shared write | QUIC Server → ModelServer | Image payload from client, written by `read_quic_stream` |
| ModelServer | ZMQ REP (bind) | `remote-server-incoming-{N}` | REQ/REP | ModelServer → QUIC Server | `read_local_zmq_socket` receives inference result size, copies result from SHM, sends ACK |
| ModelServer | SHM (attach) | `server-service{N}-incoming-shm` | Shared read | ModelServer → QUIC Server | Inference result written by ModelServer, read by `read_local_zmq_socket` |
| QUIC Client | QUIC stream | TCP (s2n-quic) | Bidirectional | QUIC Client ↔ QUIC Server | Multiplexed per-service bidirectional streams |

**Config**: `quic/quic_config_*.yaml`

---

### 5. BandwidthAllocator (`bandwidth_allocator.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| QUIC Client | ZMQ REP (bind) | `car-server-bw-service` | REQ/REP | QUIC Client → BandwidthAllocator | Receives `{bw, rtt}` from QUIC Client's `bandwidth_refresh_loop`; responds with `{allocation_map, expected_utility, model_config_map}` |
| Client (×N) | ZMQ PUB (connect) | `main-client-{N}-bw-subscriber` | PUB/SUB | BandwidthAllocator → Client | Broadcasts updated model configuration string to each Client so they adjust preprocessing |
| PingHandler | ZMQ REQ (connect) | `ping-handler` | REQ/REP | BandwidthAllocator → PingHandler | Queries current RTT; sends "ping", receives RTT value in ms |
| MainPlotter | ZMQ PUB (connect) | `car-client-diagnostics` | PUB/SUB | BandwidthAllocator → MainPlotter | Publishes allocation updates (per-service BW, expected utility, local-only utility, available BW, RTT) |
| client_main | ZMQ SUB (bind) | `bandwidth-allocator-kill-switch` | PUB/SUB | client_main → BandwidthAllocator | Receive "ABORT" signal for graceful shutdown |

**Config**: `bandwidth_allocator_config` in `config/client_config.yaml`

---

### 6. PingHandler (`ping_handler/ping_handler.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| BandwidthAllocator | ZMQ REP (bind) | `ping-handler` | REQ/REP | BandwidthAllocator → PingHandler | Responds to RTT queries with the latest ICMP ping measurement to `DST_IP` (the cloud server) |
| client_main | ZMQ SUB (bind) | `ping-handler-kill-switch` | PUB/SUB | client_main → PingHandler | Receive "ABORT" signal for graceful shutdown |

**Config**: `ping_handler_config` in `config/client_config.yaml`

---

### 7. ModelServer (`server.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| QUIC Server | ZMQ REP (bind) | `remote-server-outgoing-{N}` | REQ/REP | QUIC Server → ModelServer | Receives image size via ZMQ, reads pickled `ModelServerRequest` from SHM, sends ACK |
| QUIC Server | SHM (create) | `server-service{N}-outgoing-shm` | Shared read | QUIC Server → ModelServer | Image data + model config written by QUIC Server's `read_quic_stream` |
| QUIC Server | ZMQ REQ (connect) | `remote-server-incoming-{N}` | REQ/REP | ModelServer → QUIC Server | Sends inference result size via ZMQ after writing pickled `ModelServerResponse` to SHM, waits for ACK |
| QUIC Server | SHM (create) | `server-service{N}-incoming-shm` | Shared write | ModelServer → QUIC Server | Inference result (bounding boxes, scores) written by ModelServer |
| server_main | ZMQ SUB (bind) | `remote-server-kill-switch-{N}` | PUB/SUB | server_main → ModelServer | Receive "ABORT" signal for graceful shutdown |

**Config**: `server_config_list[N]` in `config/server_config_gcloud.yaml`

---

### 8. MainPlotter (`util/plotting_main.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| Client, BandwidthAllocator, QUIC Client | ZMQ SUB (bind) | `car-client-diagnostics` | PUB/SUB | Multiple → MainPlotter | Aggregates diagnostic messages: client request status (plot_id=1), bandwidth allocation updates (plot_id=2), network utilization stats (plot_id=3) |

**Config**: `main_plotter_config` in `config/client_config.yaml`

---

### 9. client_main (`client_main.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| Client (×N) | ZMQ PUB (connect) | `client-kill-{N}-switch` | PUB/SUB | client_main → Client | Sends "ABORT" on SIGINT for graceful per-Client shutdown |
| CameraStream (×N) | ZMQ PUB (connect) | `camera-kill-{N}-switch` | PUB/SUB | client_main → CameraStream | Sends "ABORT" on SIGINT for graceful per-CameraStream shutdown |
| BandwidthAllocator | ZMQ PUB (connect) | `bandwidth-allocator-kill-switch` | PUB/SUB | client_main → BandwidthAllocator | Sends "ABORT" on SIGINT for BandwidthAllocator shutdown |
| PingHandler | ZMQ PUB (connect) | `ping-handler-kill-switch` | PUB/SUB | client_main → PingHandler | Sends "ABORT" on SIGINT for PingHandler shutdown |

---

### 10. server_main (`server_main.py`)

| Peer | Transport | Socket Name | Pattern | Direction | Purpose |
|------|-----------|-------------|---------|-----------|---------|
| ModelServer (×N) | ZMQ PUB (connect) | `remote-server-kill-switch-{N}` | PUB/SUB | server_main → ModelServer | Sends "ABORT" on SIGINT for graceful per-ModelServer shutdown |

---

## ZMQ Socket Protocol Summary

All ZMQ sockets use the `ipc://` transport over Unix domain sockets. The base directory is configured as `zmq_pathdir` in the QUIC config and embedded in full paths in the client/server YAML configs.

| Socket Name | Type | Binder (Role) | Connector (Role) | Payload |
|-------------|------|---------------|-------------------|---------|
| `service{N}-camera-socket` | REQ/REP | CameraStream (REP) | Client (REQ) | Frame request/response metadata |
| `car-server-outgoing-{N}` | REQ/REP | QUIC Client (REP) | Client (REQ) | Image context + SHM size, ACK |
| `car-server-incoming-{N}` | REQ/REP | Client (REP) | QUIC Client (REQ) | Result size, ACK |
| `car-server-bw-service` | REQ/REP | BandwidthAllocator (REP) | QUIC Client (REQ) | `{bw, rtt}` → `{allocation_map, model_config_map}` |
| `main-client-{N}-bw-subscriber` | PUB/SUB | Client (SUB) | BandwidthAllocator (PUB) | Model config string |
| `ping-handler` | REQ/REP | PingHandler (REP) | BandwidthAllocator (REQ) | `"ping"` → RTT value (ms) |
| `car-client-diagnostics` | PUB/SUB | MainPlotter (SUB) | Client, BW Allocator, QUIC Client (PUB) | JSON diagnostic messages |
| `remote-server-outgoing-{N}` | REQ/REP | ModelServer (REP) | QUIC Server (REQ) | Image size, ACK |
| `remote-server-incoming-{N}` | REQ/REP | QUIC Server (REP) | ModelServer (REQ) | Result size, ACK |
| `client-kill-{N}-switch` | PUB/SUB | Client (SUB) | client_main (PUB) | `"ABORT"` |
| `camera-kill-{N}-switch` | PUB/SUB | CameraStream (SUB) | client_main (PUB) | `"ABORT"` |
| `bandwidth-allocator-kill-switch` | PUB/SUB | BandwidthAllocator (SUB) | client_main (PUB) | `"ABORT"` |
| `ping-handler-kill-switch` | PUB/SUB | PingHandler (SUB) | client_main (PUB) | `"ABORT"` |
| `remote-server-kill-switch-{N}` | PUB/SUB | ModelServer (SUB) | server_main (PUB) | `"ABORT"` |

## Shared Memory Regions

All SHM regions are 50 MB POSIX shared memory files. They are memory-mapped (`mmap`) into both the Python process and the Rust QUIC process for zero-copy data transfer. The ZMQ socket paired with each SHM region carries only the data size and synchronization ACKs—the actual image/result payload is read directly from SHM.

| SHM Name | Creator | Reader | Data Flow | Content |
|----------|---------|--------|-----------|---------|
| `service{N}-camera-shmem` | CameraStream | Client | Camera → Client | Raw camera frame (NumPy array) |
| `client-service{N}-incoming-shm` | Client | QUIC Client | Client → QUIC transport | Compressed image + pickled request |
| `client-service{N}-outgoing-shm` | Client | QUIC Client | QUIC transport → Client | Pickled inference result |
| `server-service{N}-outgoing-shm` | ModelServer | QUIC Server | QUIC transport → ModelServer | Compressed image + pickled request |
| `server-service{N}-incoming-shm` | ModelServer | QUIC Server | ModelServer → QUIC transport | Pickled inference result |

## End-to-End Data Path (Single Inference Request)

```
CameraStream                                                    ModelServer
  │ (capture frame, write to SHM)                                     ▲
  │                                                                   │
  ▼ SHM: service{N}-camera-shmem                                     │
Client                                                                │
  │ (read frame from SHM, compress, pickle, write to SHM)            │
  │                                                                   │
  ▼ SHM: client-service{N}-incoming-shm                               │
  │ ZMQ: car-server-outgoing-{N} (send size → ACK)                   │
  ▼                                                                   │
QUIC Client                                                           │
  │ (memcpy from SHM, enqueue, LIFO schedule, enforce BW limit)      │
  │                                                                   │
  ▼ s2n-quic stream (BBR congestion control, TLS)                     │
  ▼                                                                   │
QUIC Server                                                           │
  │ (read from stream, memcpy to SHM)                                │
  │                                                                   │
  ▼ SHM: server-service{N}-outgoing-shm                               │
  │ ZMQ: remote-server-outgoing-{N} (send size → ACK)                │
  ▼                                                                   │
ModelServer ──── (run EfficientDet inference on GPU) ─────────────────┘
  │ (pickle result, write to SHM)
  │
  ▼ SHM: server-service{N}-incoming-shm
  │ ZMQ: remote-server-incoming-{N} (send size → ACK)
  ▼
QUIC Server → QUIC stream → QUIC Client
  │
  ▼ SHM: client-service{N}-outgoing-shm
  │ ZMQ: car-server-incoming-{N} (send size → ACK)
  ▼
Client (receives inference result, checks SLO, logs)
```
