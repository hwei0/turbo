# System Architecture

This document provides a detailed technical overview of TURBO's architecture, including the problem formulation, model configurations, network measurement, bandwidth allocation solver, and end-to-end data flow. For operational details and setup instructions, see the [main README](../README.md).

## Problem Setup

An autonomous vehicle runs multiple perception services simultaneously—for example, three camera-based object detection services covering the FRONT, FRONT_LEFT, and FRONT_RIGHT fields of view. Each service must continuously detect objects (vehicles, pedestrians, cyclists, signs) from its camera feed to support safe driving decisions.

Running high-accuracy object detection models on-vehicle is limited by the AV's onboard compute (e.g., a Jetson Orin). While a small model like EfficientDet-D1 can run locally, it produces lower detection accuracy (mAP) compared to larger models like EfficientDet-D6 or D7x that require cloud GPU resources (e.g., an H100). Offloading inference to a remote server introduces a new constraint: **network bandwidth**. Sending full-resolution camera frames over a cellular or wireless uplink is expensive, and the link is shared across all services. Additionally, each service must meet a **latency SLO** (service-level objective)—the total round-trip time from image capture to receiving detection results must stay under a deadline (e.g., 150–200 ms), otherwise the result is too stale to be useful.

The core problem is: **given the current available bandwidth and network RTT, how should the system allocate bandwidth across the N services, and what model configuration (detection model + compression level) should each service use, to maximize total detection accuracy while meeting the latency SLO?**

This system solves that problem by:
1. Providing a menu of **model configurations** that trade off accuracy for bandwidth (see below).
2. Pre-computing **utility curves** that map bandwidth to achievable detection accuracy for each configuration.
3. Running an **LP-based bandwidth solver** at runtime that optimally allocates bandwidth and selects configurations based on live network conditions.

## Model Configurations

Each model configuration is identified by a string of the form:

```
edd{N}-imgcomp{X}-inpcomp{Y}
```

where:
- **`edd{N}`** is the EfficientDet variant: `edd1` (D1), `edd2` (D2), `edd4` (D4), `edd6` (D6), or `edd7x` (D7x). Larger models are more accurate but have longer inference runtimes.
- **`imgcomp{X}`** is the **image compression** setting, applied to the raw camera frame *before* model-specific resizing. `X` can be:
  - `None` — no image-level compression (send the raw or uncompressed image)
  - `50`, `75`, `90`, `95` — JPEG compression at that quality level
  - `PNG` — lossless PNG compression
- **`inpcomp{Y}`** is the **input compression** setting, applied *after* model-specific preprocessing (resize + normalize). `Y` has the same options as `X`.

**Constraint**: exactly one of `imgcomp` or `inpcomp` is active (non-`None`) at a time—they cannot both be set, because compression is applied at a single point in the pipeline.

### Image Processing vs. Input Processing

There are two compression strategies, differing in *where* compression is applied in the pipeline:

**Image compression (`imgcomp`)** — compress the raw camera frame:
1. Capture raw frame from camera (1080×1920×3).
2. Apply JPEG or PNG compression to the raw image → produces a compressed byte buffer.
3. Send the compressed buffer over the network.
4. On the server: decompress → resize to model input size → normalize → run inference.

**Input compression (`inpcomp`)** — compress the preprocessed model input:
1. Capture raw frame from camera (1080×1920×3).
2. Resize to the model's native input size (e.g., 768×768 for D2, 1024×1024 for D4) and normalize (ImageNet mean/std).
3. Apply JPEG or PNG compression to the resized tensor (transposed back to image layout) → compressed byte buffer.
4. Send the compressed buffer over the network.
5. On the server: decompress → convert back to tensor → run inference.

Image compression produces much smaller payloads (the raw image is compressed before any upscaling), but the server must do the full resize+normalize preprocessing. Input compression sends a larger payload (the model-resolution image), but the server skips the preprocessing step, which can reduce server-side latency.

### Worked Examples

**`edd2-imgcompNone-inpcomp50`** — EfficientDet-D2 with input preprocessing and JPEG quality 50:
1. Client captures a 1080×1920×3 raw camera frame.
2. Client resizes to 768×768 (D2's native input size) and normalizes using ImageNet mean/std.
3. Client transposes the tensor back to image layout and applies JPEG compression at quality 50.
4. The compressed buffer (~0.39 Mb) is sent over QUIC to the server.
5. Server decompresses the JPEG, converts back to a normalized tensor, and runs D2 inference (~53 ms runtime).
6. **Total transport size: 0.39 Mb.** This is very bandwidth-efficient but lossy compression may degrade detection quality.

**`edd4-imgcompPNG-inpcompNone`** — EfficientDet-D4 with lossless PNG image compression:
1. Client captures a 1080×1920×3 raw camera frame.
2. Client applies lossless PNG compression to the raw 1080×1920 image (no resizing yet).
3. The compressed buffer (~33.82 Mb) is sent over QUIC to the server.
4. Server decompresses the PNG, resizes to 1024×1024 (D4's native input size), normalizes, and runs D4 inference (~284 ms runtime).
5. **Total transport size: 33.82 Mb.** Lossless compression preserves full image quality but produces a large payload, requiring substantial bandwidth to meet the SLO.

### Available Models and Their Costs

The table below summarizes every model configuration's network transport size and server-side runtime (from `experiment_model_info.csv`). The `edd1` baseline runs locally on-vehicle and requires no network bandwidth.

| Model | Transport Size (Mb) | Runtime (ms) | Notes |
|-------|-------------------|-------------|-------|
| `edd1-imgcompNone-inpcompNone` | 0.00 | 135.6 | On-vehicle baseline (no offloading) |
| `edd2-imgcomp50-inpcompNone` | 1.89 | 80.9 | JPEG Q50 on raw image |
| `edd2-imgcompNone-inpcomp50` | 0.39 | 53.0 | JPEG Q50 on preprocessed input |
| `edd2-imgcompNone-inpcompNone` | 14.16 | 43.2 | Uncompressed preprocessed input |
| `edd4-imgcomp50-inpcompNone` | 1.89 | 92.4 | JPEG Q50 on raw image |
| `edd4-imgcompPNG-inpcompNone` | 33.82 | 284.0 | Lossless PNG on raw image |
| `edd4-imgcompNone-inpcomp50` | 0.65 | 72.7 | JPEG Q50 on preprocessed input |
| `edd6-imgcomp50-inpcompNone` | 1.89 | 113.9 | JPEG Q50 on raw image |
| `edd6-imgcompNone-inpcomp50` | 0.96 | 118.0 | JPEG Q50 on preprocessed input |
| `edd7x-imgcomp50-inpcompNone` | 1.89 | 156.7 | JPEG Q50 on raw image |
| `edd7x-imgcompNone-inpcomp50` | 1.31 | 170.2 | JPEG Q50 on preprocessed input |

The full set of configurations is in [experiment_model_info.csv](../experiment_model_info.csv). There is a fundamental trade-off: larger models (D6, D7x) produce higher detection accuracy but have longer runtimes, leaving less time budget within the SLO for network transfer. Heavier compression (JPEG Q50) dramatically reduces transport size but degrades detection quality.

## Network Measurement

The system measures two network quantities in real time:

### Available Bandwidth

The QUIC transport layer (Rust, using s2n-quic with BBR congestion control) exposes **congestion window (CWND)** and **smoothed RTT** from the QUIC recovery layer. A custom `RecoverySubscriber` captures these metrics from s2n-quic's event system and stores them in an atomic snapshot (`RecoverySnapshot`).

The `bandwidth_refresh_loop` in the QUIC client periodically reads these values and computes available bandwidth as:

```
available_bandwidth_Mbps = (CWND_bytes / RTT_seconds) * 8 / 1,000,000
```

This is the estimated throughput capacity of the QUIC connection, derived directly from the BBR congestion controller's state. It is sent to the BandwidthAllocator every `bw_update_interval_ms` (default: 500 ms).

### Round-Trip Time (RTT)

The `PingHandler` process runs ICMP pings to the cloud server IP every 250 ms using the `ping3` library. It maintains the latest RTT measurement, which the BandwidthAllocator queries via ZMQ before each allocation decision.

This ICMP RTT is used (rather than QUIC's smoothed RTT) because it measures the raw network latency independent of QUIC's congestion state. The bandwidth solver needs RTT as a separate input to compute how much of the latency SLO budget is consumed by network delay versus inference time.

## Bandwidth Solver

### Utility Curves

For each perception service and each model configuration, the system pre-computes a **utility curve**: a step function mapping available bandwidth (Mbps) to achievable detection accuracy (mean average precision, mAP).

The key insight is that a model configuration requires a **minimum bandwidth** to meet the latency SLO. Given a configuration with transport size `S` (Mb) and server runtime `T_exec` (ms), and current network RTT `T_RTT` (ms), the time available for network transfer is:

```
T_transfer = T_SLO - T_RTT - T_exec
```

The minimum bandwidth required is:

```
BW_min = S / (T_transfer / 1000)   [Mbps]
```

If `T_transfer <= 0`, the configuration cannot meet the SLO regardless of bandwidth (the RTT + runtime already exceeds the deadline).

The utility curve is constructed by sorting all feasible configurations by their `BW_min` and keeping only those that improve upon the best utility seen so far (creating a monotonically non-decreasing step function). The result is a `ConcreteUtilityCurve` where each step corresponds to a (model, compression) configuration: at bandwidth `BW_min`, the system unlocks that configuration and achieves its detection accuracy.

### LP Formulation

The `LPAllocator` formulates bandwidth allocation as a linear program solved by PuLP (CBC solver):

- **Decision variables**: For each service `i` and each step `j` in its utility curve, a binary variable `y[i][j]` indicating whether step `j` is selected.
- **Objective**: Maximize total utility: `Σ_i Σ_j y[i][j] * utility[i][j]`
- **Bandwidth constraint**: The sum of minimum bandwidths for selected steps must not exceed total available bandwidth: `Σ_i Σ_j y[i][j] * BW_min[i][j] <= total_BW`
- **Selection constraint**: Each service selects exactly one step: `Σ_j y[i][j] = 1` for all `i`

The solver runs in <10 ms and produces, for each service:
- **`bandwidth_allocated`**: the minimum bandwidth needed for the selected configuration (Mbps)
- **`expected_utility`**: the predicted detection accuracy (mAP) at that configuration
- **`model_config_name`**: the configuration string (e.g., `edd4-imgcomp50-inpcompNone`)

### How Allocations Are Used

The BandwidthAllocator broadcasts the solver output to three consumers:

1. **QUIC Client** (via `car-server-bw-service` ZMQ REQ/REP): Receives `allocation_map` — a mapping from service ID to allocated bandwidth in Mbps. The QUIC client's `BandwidthManager` converts these to bytes/sec and enforces per-service rate limits in the `send_loop`. Any leftover bandwidth (total available minus sum of allocations) is assigned to the **junk service** (capped at 25 Mbps), which sends dummy data to keep the QUIC connection probing its true capacity.

2. **Client processes** (via `main-client-{N}-bw-subscriber` ZMQ PUB/SUB): Each Client receives `model_config_map` — a mapping from service ID to configuration string. The Client sets `self.current_model` to this string and uses it on the next iteration to determine:
   - Which EfficientDet variant to target (`base_model`)
   - Whether to apply image-level or input-level preprocessing
   - What compression quality to use (if any)
   - If the allocation is `edd1-imgcompNone-inpcompNone`, the Client skips remote offloading entirely and logs the result as locally served.

3. **Web dashboard** (via `car-client-diagnostics` ZMQ PUB/SUB): Receives the full allocation update including per-service bandwidths, expected utility, local-only utility, available bandwidth, and RTT for real-time visualization.

## End-to-End Walkthrough: One Image Through the Pipeline

This section traces a single image frame through the full system using model configuration `edd4-imgcompNone-inpcomp50` (EfficientDet-D4 with input preprocessing and JPEG quality 50 compression). Assume this is Service 1 (FRONT camera), the SLO is 200 ms, and the BandwidthAllocator has already assigned this configuration to Service 1.

### Happy Path (Successful Inference)

**Step 1 — Camera Capture** (`CameraDataStream`)

The CameraDataStream background thread continuously reads frames from the USB webcam (`cv2.VideoCapture`) and writes the latest frame into shared memory (`service1-camera-shmem`). This happens independently of the Client — the SHM buffer always holds the most recent frame.

**Step 2 — Client Requests Frame** (`Client`, `client.py`)

The Client sends a `CameraDataRequest` to CameraDataStream via ZMQ (`service1-camera-socket`). CameraDataStream responds with a `CameraDataResponse` containing metadata. The Client then reads the raw 1080×1920×3 frame directly from the shared memory region `service1-camera-shmem`. A two-phase ACK handshake follows to ensure the CameraDataStream doesn't overwrite the buffer while the Client is still reading.

**Step 3 — Client Preprocessing** (`Client`, `client.py`)

The Client parses the current model string `edd4-imgcompNone-inpcomp50`:
- `edd4` → base model is `tf_efficientdet_d4`, native input size is 1024×1024
- `imgcompNone` → no image-level compression
- `inpcomp50` → input-level JPEG compression at quality 50

Since `inpcomp` is active, the Client does the following:
1. **Resize + normalize**: Applies `effdet.data.transforms.transforms_coco_eval` at 1024×1024 to produce a `(3, 1024, 1024)` NumPy array (using the `raw=True` variant, which returns a NumPy array rather than a PyTorch tensor).
2. **Transpose**: Converts from `(3, 1024, 1024)` (channels-first) to `(1024, 1024, 3)` (channels-last) so it can be treated as a regular image for JPEG compression.
3. **JPEG compress**: Calls `cv2.imencode(".jpg", ..., quality=50)`, producing a compact byte buffer (~0.65 Mb for D4's input resolution).

**Step 4 — Client Sends to QUIC** (`Client`, `client.py`)

The Client constructs a `ModelServerRequest` object containing the compressed byte buffer, context ID, base model name, and processing flags. It pickles the request and writes the pickle bytes into shared memory `client-service1-incoming-shm`. Then it sends a ZMQ multipart message `[context_id, pickle_length]` on socket `car-server-outgoing-1` and waits for an ACK.

**Step 5 — QUIC Client Reads from SHM and Enqueues** (`read_local_zmq_socket`, Rust)

The `read_zmq_socket_loop` receives the ZMQ multipart message, parses `image_context` and `image_size`, then does a `memcpy` of `image_size` bytes from SHM `client-service1-incoming-shm` into a heap-allocated `Vec<u8>`. It sends an "ack" back to the Client via ZMQ, then calls `enqueue_msg()`.

`enqueue_msg()` implements **LIFO (newest-first) queue management**:
- If the queue is empty, the item is pushed directly.
- If the queue has items, all items *except* the one currently mid-transmission (`tx_idx > 0`) are dropped. The new item is pushed to the back (it will be dequeued next).

This ensures the QUIC layer always prioritizes the most recent frame, dropping older queued frames that haven't started transmitting yet.

**Step 6 — QUIC Client Transmits Over QUIC** (`send_loop`, Rust)

The `send_loop` runs on a 1 ms tick. Each iteration:
1. Polls the `BandwidthManager` for the current per-service allocation (bytes/sec). For regular services (not junk), `tx_bytes` is set to `i64::MAX` (no rate limit — the QUIC congestion controller handles pacing).
2. Pops items from the front of the queue. Before sending, checks the **SLO timeout**: if `item.timestamp.elapsed() >= slo_timeout` and the item hasn't started transmitting (`tx_idx == 0`), it is silently dropped.
3. For items that pass the timeout check, writes a 4-byte big-endian `image_context`, 4-byte big-endian `data_length`, then the payload bytes to the QUIC stream. If the item is larger than the remaining byte budget, it splits the item (updates `tx_idx`) and re-enqueues the remainder.
4. Calls `flush()` on the QUIC stream to push data to the wire.

The image travels over the s2n-quic connection (BBR congestion control, TLS encrypted) to the cloud server.

**Step 7 — QUIC Server Receives and Writes to SHM** (`read_quic_stream`, Rust)

The server-side `read_stream_loop` reads from the QUIC receive stream:
1. Reads 4-byte `image_context`, 4-byte `target_len`.
2. Reads `target_len` bytes of payload into a buffer.
3. Does a `memcpy` of the payload into SHM `server-service1-outgoing-shm`.
4. Sends the data size as a string via ZMQ on `remote-server-outgoing-1`.
5. Waits for "ACK" from the ModelServer before processing the next image.

**Step 8 — ModelServer Runs Inference** (`ModelServer`, `server.py`)

The ModelServer receives the size string via ZMQ, reads `size` bytes from SHM `server-service1-outgoing-shm`, and unpickles a `ModelServerRequest`. Because `enable_input_processing=True` and `enable_compression=True`:
1. **Decompress**: `cv2.imdecode` reconstructs the image from the JPEG buffer → PIL Image.
2. **Convert**: PIL → NumPy array `(1024, 1024, 3)` → transpose to `(3, 1024, 1024)` → `torch.from_numpy().unsqueeze_(0)` → `(1, 3, 1024, 1024)` tensor.
3. **Inference**: Moves the tensor to GPU, subtracts ImageNet mean, divides by std, runs `DetBenchPredict` → output tensor of `[x_min, y_min, x_max, y_max, score, label]` rows.

The ModelServer pickles a `ModelServerResponse(context_id, result_array)`, writes it to SHM `server-service1-incoming-shm`, and sends the pickle size via ZMQ on `remote-server-incoming-1`. It waits for ACK from the QUIC server.

**Step 9 — Return Trip** (QUIC Server → QUIC Client → Client)

The response travels the reverse path:
- QUIC server's `read_zmq_socket_loop` reads the response from SHM, enqueues it, and `send_loop` transmits it back over QUIC.
- QUIC client's `read_stream_loop` receives it, writes to SHM `client-service1-outgoing-shm`, and sends the size via ZMQ on `car-server-incoming-1`.

**Step 10 — Client Receives Result** (`Client`, `client.py`)

The Client is polling `quic_rcv_zmq_socket` within its SLO timeout window. It receives the size, reads and unpickles the `ModelServerResponse` from SHM `client-service1-outgoing-shm`, sends "ACK" back, and checks that `response.context_id` matches the current `context_id_ctr`. Since it matches, the response is accepted.

The Client extracts bounding boxes from the response array, logs the full latency breakdown (camera, preprocessing, serialization, network, deserialization) and detection results to the `ClientSpillableStore`, and emits a diagnostic message to the web dashboard. The frame is complete.

---

### Failure Case 1: SLO Timeout at the Client

**Scenario**: The network is congested, RTT is high, or inference is slow, and the total round-trip time exceeds `SLO_TIMEOUT_MS` (e.g., 200 ms).

At Step 10, the Client's response-listening loop checks elapsed time (excluding camera latency) against `SLO_TIMEOUT_MS` on each iteration. If the deadline is exceeded, or if `quic_rcv_zmq_socket.poll(timeout=remaining_ms)` returns false, the loop breaks with `response = None`.

The Client still logs the request to the `ClientSpillableStore` with `remote_request_received=False` and `None` for all bounding box fields. The diagnostic message reports the request as a failure. The Client immediately moves on to the next frame with an incremented `context_id_ctr`.

The stale inference result may arrive later. On the *next* iteration's response-listening phase, the Client will receive it, see that `response.context_id != self.context_id_ctr` (it's from the old frame), discard it, and continue listening for the correct response. This **queue draining** happens at ~1 ms per stale response and ensures old results don't accumulate.

---

### Failure Case 2: Frame Dropped in the QUIC Send Queue (SLO Timeout)

**Scenario**: The Client sends images faster than the QUIC layer can transmit them (e.g., bandwidth is heavily constrained).

At Step 6, when the `send_loop` pops an item from the queue, it checks:
```
if item.timestamp.elapsed() >= slo_timeout && item.tx_idx == 0
```

If the image has been sitting in the queue longer than the SLO timeout and hasn't started transmitting (`tx_idx == 0`), it is **silently dropped** — the send loop moves to the next item via `continue`. The image is never sent over the network.

From the Client's perspective, this looks the same as Failure Case 1: no response arrives within the SLO window, the Client times out and moves on. The frame is logged as failed.

Note: if the frame *has* started transmitting (`tx_idx > 0` — some bytes were already written to the QUIC stream), it is **not** dropped. Partially-sent frames are always completed, because the QUIC stream is a byte-ordered channel and dropping mid-stream would corrupt the framing protocol.

---

### Failure Case 3: Frame Superseded by a Newer Frame (LIFO Eviction)

**Scenario**: While frame N is sitting in the queue waiting to be sent, the Client sends frame N+1.

At Step 5, when `enqueue_msg()` is called for frame N+1:
1. If frame N is still queued and has `tx_idx == 0` (hasn't started transmitting), it is **evicted** from the queue. Frame N+1 takes its place. Frame N is never sent.
2. If frame N has started transmitting (`tx_idx > 0`), frame N is kept (the send loop will finish it), but any *other* queued frames are evicted. Frame N+1 is added behind frame N.

The queue maintains at most two items: the currently-transmitting frame (if any) and the newest enqueued frame. This implements **LIFO (last-in, first-out)** prioritization — the system always prefers the freshest data.

The evicted frame is simply dropped from the queue with no notification. From the Client's perspective, it may or may not time out waiting for a response (depending on whether a newer response arrives in time — if frame N+1's response arrives with the correct `context_id`, the Client accepts it). If no matching response arrives in time, it's another SLO timeout (Failure Case 1).

---

### Failure Case Summary

| Failure | Where | Trigger | Outcome |
|---------|-------|---------|---------|
| **SLO timeout** | Client (`client.py`) | Total round-trip exceeds `SLO_TIMEOUT_MS` | Client logs failure, moves to next frame, drains stale responses later |
| **Queue timeout drop** | QUIC `send_loop` (Rust) | Frame age in queue exceeds `slo_timeout` before transmission starts | Frame silently dropped, never sent; Client eventually times out |
| **LIFO eviction** | QUIC `enqueue_msg` (Rust) | Newer frame arrives while older frame is still queued (not yet transmitting) | Older frame evicted from queue; at most 1 queued + 1 in-flight frame at any time |

In all failure cases, the system self-recovers: the Client increments its `context_id_ctr`, the queue drains stale responses, and the next iteration starts fresh with the latest camera frame. The latency SLO ensures that stale results never propagate to the perception output — only timely detections are used.

## High-Level Architecture

```
┌─────────────────────────── CLIENT (AV) ────────────────────────────┐
│                                                                     │
│  ┌──────────────┐    ┌─────────────┐    ┌────────────────────────┐  │
│  │ CameraStream │───▶│   Client    │───▶│   QUIC Client (Rust)   │──┼──┐
│  │  (per-cam)   │ ZMQ│  (client.py)│ SHM│  bandwidth-aware tx,   │  │  │
│  │              │    │  preprocess │    │  per-service queuing,  │  │  │
│  └──────────────┘    │  compress   │    │  LIFO frame dropping   │  │  │
│                      └──────┬──────┘    └──────────┬─────────────┘  │  │
│                             │                      │                │  │
│                      ┌──────┴──────┐        ┌──────┴─────────┐     │  │
│                      │  Bandwidth  │◀──ZMQ──│  Bandwidth     │     │  │
│                      │  Allocation │        │  Refresh Loop  │     │  │
│                      │  (from BW   │        │  (in QUIC      │     │  │
│                      │  Allocator) │        │   client)      │     │  │
│                      └─────────────┘        └────────────────┘     │  │
│                                                                     │  │
│  ┌──────────────┐    ┌──────────────────┐                          │  │
│  │ PingHandler  │───▶│ BandwidthAllocator│                         │  │
│  │ (RTT probe)  │ ZMQ│ (LP solver for   │                         │  │
│  └──────────────┘    │  per-service BW   │                         │  │
│                      │  & model config)  │                         │  │
│                      └──────────────────┘                          │  │
└─────────────────────────────────────────────────────────────────────┘  │
                                                                        │
                              QUIC (s2n-quic, BBR congestion control)   │
                                                                        │
┌─────────────────────────── SERVER (Cloud) ──────────────────────────┐  │
│                                                                     │  │
│  ┌────────────────────────┐    ┌──────────────────┐                │  │
│  │   QUIC Server (Rust)   │◀───│                  │                │◀─┘
│  │   receives images,     │ SHM│   ModelServer     │                │
│  │   forwards to server   │───▶│   (server.py)    │                │
│  │   returns responses    │◀───│   EfficientDet   │                │
│  └────────────────────────┘    │   inference      │                │
│                                └──────────────────┘                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────── MONITORING ─────────────────────────────┐
│  ┌──────────────────┐                                              │
│  │   WebFrontend     │  Flask + SocketIO dashboard                 │
│  │   (plotting_main) │  Real-time bandwidth allocation,           │
│  │                   │  service status, and utilization plots      │
│  └──────────────────┘                                              │
└─────────────────────────────────────────────────────────────────────┘
```

## How It Works

1. **Camera feeds** are captured from USB webcams by `CameraDataStream` and placed in shared memory for each perception service.

2. Each **Client** instance (one per perception service) reads camera frames, applies image preprocessing and/or JPEG compression based on its currently assigned model configuration, and forwards the processed image to the QUIC client via shared memory + ZMQ.

3. The **QUIC transport layer** (Rust, using s2n-quic with BBR congestion control) manages per-service bidirectional streams. It enforces per-service bandwidth limits, queues frames in LIFO order (newest first), and drops frames that exceed the latency SLO timeout. It also runs a "junk service" to probe available bandwidth capacity.

4. On the server side, the **QUIC server** receives images and passes them to the **ModelServer**, which runs EfficientDet inference (d1 through d7x) on GPU. Results are serialized back through the QUIC connection to the client.

5. The **BandwidthAllocator** continuously monitors network conditions:
   - Receives **available bandwidth** estimates from the QUIC client (derived from CWND and RTT).
   - Receives **RTT** measurements from the `PingHandler`.
   - Solves a **linear program** (via PuLP) that allocates bandwidth across services to maximize total detection utility, selecting the optimal (model, compression) configuration per service.
   - Broadcasts the allocation to both the QUIC client (to enforce bandwidth limits) and the Python clients (to adjust preprocessing).

6. The **utility curve framework** (`utility_curve_stream/`) pre-computes step-function utility curves from offline evaluation data. Each curve maps available bandwidth to the best achievable detection accuracy for a given (model, compression, RTT, SLO) combination.

7. The **web frontend** subscribes to diagnostic ZMQ messages and renders real-time matplotlib plots (bandwidth allocation, service status, network utilization) via a Flask + WebSocket dashboard.

## Graceful Shutdown

### Why ZMQ Kill Switches Instead of SIGINT

The system uses **ZMQ PUB/SUB kill-switch sockets** for graceful shutdown rather than relying on SIGINT (Ctrl-C) propagation to child processes. Each orchestrator (`client_main.py`, `server_main.py`) creates ZMQ PUB sockets and, on SIGINT, broadcasts an `"ABORT"` message to all child processes. Each child process ignores SIGINT entirely (`signal.SIG_IGN` is set before the constructor runs) and instead polls a ZMQ SUB socket for the kill signal.

This design is motivated by three problems with SIGINT-based shutdown:

1. **ZMQ REQ/REP state machine corruption.** ZMQ REQ/REP sockets enforce a strict alternating send-recv-send-recv protocol. If a `KeyboardInterrupt` fires while a process is between a send and a recv (e.g., after sending a request but before receiving the reply), the socket enters an invalid state. Any subsequent send or recv call on that socket will raise `zmq.error.ZMQError`. This makes it impossible to perform a clean shutdown that involves communicating over the same socket — for example, the BandwidthAllocator needs to send a `-1` termination sentinel to PingHandler before shutting down.

2. **Interruption at arbitrary points.** SIGINT can arrive during any blocking operation — `pickle.dumps()`, shared memory writes, Parquet file flushes, or even mid-frame in `cv2.VideoCapture.read()`. This can produce corrupted output files, partially-written shared memory regions, or leaked POSIX SHM segments.

3. **Non-deterministic cross-process timing.** With SIGINT, there is no guarantee about the order in which child processes receive and handle the signal. One process might clean up and close a shared ZMQ socket while another is still trying to use it. The kill-switch approach ensures each process detects the signal at a well-defined point in its main loop (the `kill_switch.poll()` call), guaranteeing that all in-flight operations complete before cleanup begins.

### Shutdown Sequence

**Client side** (`client_main.py`):

1. User presses Ctrl-C → `client_main` catches SIGINT in its signal handler.
2. `client_main` sends `"ABORT"` on all kill-switch PUB sockets (one per CameraDataStream, Client, BandwidthAllocator, and PingHandler).
3. Each child process detects the signal on its next `kill_switch.poll()` iteration.
4. Each child flushes accumulated data to Parquet files, closes ZMQ sockets (with `linger=0` to avoid blocking), releases hardware resources (cameras, SHM), and exits.
5. `client_main` waits for all `AsyncResult` handles to complete, then exits.

**Server side** (`server_main.py`):

1. User presses Ctrl-C → `server_main` catches SIGINT.
2. `server_main` sends `"ABORT"` on each ModelServer's kill-switch PUB socket.
3. Each ModelServer detects the signal, flushes Parquet logs, closes ZMQ sockets and SHM regions, and exits.
4. `server_main` waits for all processes to complete, then exits.

### SIGINT Suppression in Child Processes

Each child process sets `signal.signal(signal.SIGINT, signal.SIG_IGN)` **before** its constructor runs (in the `run_*` wrapper function). This is important because several constructors block on ZMQ recv calls (e.g., `Client.__init__` waits for the first bandwidth allocation from QUIC). If SIGINT were not suppressed, a Ctrl-C during this blocking phase would raise `KeyboardInterrupt` inside the constructor, before `main_loop()` ever runs — meaning the process would crash without any cleanup logic executing.

---

For more information on configuration files, logging output, running the system, and the full directory structure, see the [main README](../README.md).
