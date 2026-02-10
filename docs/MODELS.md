# Model Setup & Reference

This document covers the fine-tuned EfficientDet models used by TURBO for object detection, including download instructions, configuration, and inference pipeline details.

## Overview

TURBO uses five variants of [EfficientDet](https://arxiv.org/abs/1911.09070) fine-tuned on the [Waymo Open Dataset](https://waymo.com/open/) for 5-class 2D object detection. The models are implemented as [PyTorch Lightning](https://lightning.ai/) modules using the [`effdet`](https://github.com/rwightman/efficientdet-pytorch) library, and are loaded from checkpoint files (`.ckpt`) at server startup.

### Detection Classes

All models are trained to detect the following 5 classes from the Waymo Open Dataset:

| Class ID | Label       |
|----------|-------------|
| 0        | Vehicle     |
| 1        | Pedestrian  |
| 2        | Cyclist     |
| 3        | Sign        |
| 4        | Unknown     |

### Model Variants

| Variant | Base Model | Input Resolution | Role | Notes |
|---------|-----------|-----------------|------|-------|
| EfficientDet-D1 | `tf_efficientdet_d1` | 640 x 640 | Client backup (on-vehicle) | Runs locally on AV when cloud offloading is infeasible |
| EfficientDet-D2 | `tf_efficientdet_d2` | 768 x 768 | Server model | Smallest server-side model |
| EfficientDet-D4 | `tf_efficientdet_d4` | 1024 x 1024 | Server model | Mid-range accuracy/speed |
| EfficientDet-D6 | `tf_efficientdet_d6` | 1280 x 1280 | Server model | High accuracy, slower inference |
| EfficientDet-D7x | `tf_efficientdet_d7x` | 1536 x 1536 | Server model | Highest accuracy, longest inference |

Larger models produce higher detection accuracy (mAP) but require more GPU compute time, leaving less of the latency SLO budget for network transfer. See [ARCHITECTURE.md](ARCHITECTURE.md#available-models-and-their-costs) for the full accuracy/latency/bandwidth trade-off table.

## Downloading the Models

The fine-tuned model checkpoints are hosted on Google Cloud Storage. Download and extract them before running the server:

```bash
# Download the model archive (~large file, ensure sufficient disk space)
wget https://storage.googleapis.com/turbo-nines-2026/av-models.zip

# Extract to a directory of your choice
unzip av-models.zip -d ~/av-models
```

Alternatively, using `curl`:

```bash
curl -o av-models.zip https://storage.googleapis.com/turbo-nines-2026/av-models.zip
unzip av-models.zip -d ~/av-models
```

### Expected Directory Structure

After extraction, the checkpoint files should be organized as follows. Note that the `version_N/` directory varies per model (reflecting the training run used to produce the best checkpoint):

```
av-models/
├── tf_efficientdet_d1-waymo-open-dataset/
│   └── version_1/
│       └── checkpoints/
│           └── epoch=9-step=209850.ckpt
├── tf_efficientdet_d2-waymo-open-dataset/
│   └── version_2/
│       └── checkpoints/
│           └── epoch=9-step=419700.ckpt
├── tf_efficientdet_d4-waymo-open-dataset/
│   └── version_0/
│       └── checkpoints/
│           └── epoch=9-step=839400.ckpt
├── tf_efficientdet_d6-waymo-open-dataset/
│   └── version_2/
│       └── checkpoints/
│           └── epoch=9-step=3357600.ckpt
└── tf_efficientdet_d7x-waymo-open-dataset/
    └── version_1/
        └── checkpoints/
            └── epoch=8-step=1477071.ckpt
```

## Configuring Checkpoint Paths

After downloading, update the checkpoint paths in two configuration files to point to your extracted model directory.

### Server Configuration (`config/server_config_gcloud.yaml`)

The `server_model_list` section defines the models available to each ModelServer. Update each `checkpoint_path` to match your extraction directory:

```yaml
server_model_list:
  - checkpoint_path: /home/user/av-models/tf_efficientdet_d2-waymo-open-dataset/version_2/checkpoints/epoch=9-step=419700.ckpt
    num_classes: 5
    image_size: [768, 768]
    base_model: "tf_efficientdet_d2"
  - checkpoint_path: /home/user/av-models/tf_efficientdet_d4-waymo-open-dataset/version_0/checkpoints/epoch=9-step=839400.ckpt
    num_classes: 5
    image_size: [1024, 1024]
    base_model: "tf_efficientdet_d4"
  # ... etc.
```

### Model Config (`src/python/model_server/model_config.yaml`)

This file defines the model metadata used for standalone model server testing. Update the `checkpoint_path` fields under both `server_models` and `client_backup_model`:

```yaml
server_models:
  "tf_efficientdet_d2":
    base_model: "tf_efficientdet_d2"
    checkpoint_path: /home/user/av-models/tf_efficientdet_d2-waymo-open-dataset/version_2/checkpoints/epoch=9-step=419700.ckpt
    device: "cuda:0"
    num_classes: 5
    image_size: [768, 768]
  # ... update all entries similarly

client_backup_model:
  name: "edd1-imgcompNone-inpcompNone"
  checkpoint_path: /home/user/av-models/tf_efficientdet_d1-waymo-open-dataset/version_1/checkpoints/epoch=9-step=209850.ckpt
  device: "cuda:1"
  num_classes: 5
```

**Important:** The `device` field (e.g., `cuda:0`, `cuda:2`) must match the GPUs available on your server. Adjust these based on your hardware. See [CONFIGURATION.md](CONFIGURATION.md) for the full configuration reference.

## Inference Pipeline

### Model Loading (`effdet_inference.py`)

At server startup, each `ModelServer` process loads its assigned model checkpoints using `EfficientDetProfiler`:

1. **Load checkpoint** — `EfficientDetModel.load_from_checkpoint()` restores the PyTorch Lightning module from the `.ckpt` file, using the `base_model` name and `num_classes` to reconstruct the architecture.
2. **Extract model** — The inner `EfficientDet` model is unwrapped from the Lightning module and wrapped in `effdet.DetBenchPredict` for inference mode.
3. **Move to GPU** — The model is transferred to the specified CUDA device.
4. **Precompute normalization** — ImageNet mean/std tensors are precomputed on the target device for efficient inference-time normalization.

### Preprocessing

Input images are preprocessed using `effdet.data.transforms.transforms_coco_eval`, which applies:
- **Resize** to the model's native input resolution (e.g., 768x768 for D2, 1024x1024 for D4)
- **Normalization** using ImageNet mean and standard deviation values

Preprocessing can happen either client-side (input compression mode) or server-side (image compression mode), depending on the active model configuration. See [ARCHITECTURE.md](ARCHITECTURE.md#image-processing-vs-input-processing) for details on these two compression strategies.

### Inference

The `EfficientDetProfiler.predict()` method:
1. Normalizes the input tensor (subtract ImageNet mean, divide by std)
2. Runs the `DetBenchPredict` forward pass on GPU
3. Returns detection outputs as `[x_min, y_min, x_max, y_max, score, label]` tensors

---

For the full system architecture and end-to-end data flow, see [ARCHITECTURE.md](ARCHITECTURE.md). For configuration file details, see [CONFIGURATION.md](CONFIGURATION.md).
