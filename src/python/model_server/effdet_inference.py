"""EfficientDet model loading, preprocessing, inference, and image compression utilities.

Provides EfficientDetProfiler, which loads a trained EfficientDet checkpoint (from the
Waymo Open Dataset), moves it to a specified GPU, and exposes preprocess/predict methods.
Also provides JPEG/PNG image compression (compress_image) and decompression
(decompress_image) functions used by the client and server to reduce network transport
size at configurable quality levels.

The create_preprocessing_function factory generates model-specific preprocessing
transforms (resize + normalize) that can be applied either client-side or server-side
depending on the allocated model configuration.
"""

import logging
import time
from typing import List, Optional
import cv2
import numpy as np
from pydantic import BaseModel
import torch
import effdet
from PIL import Image
from accuracy.detection.efficient_det import EfficientDetModel
from exceptions import SerializationError

LOGGER = logging.getLogger("effdet_inference")


class ModelMetadata(BaseModel):
    checkpoint_path: str
    num_classes: int
    image_size: List[int]
    base_model: str


IMAGENET_DEFAULT_MEAN = effdet.data.loader.IMAGENET_DEFAULT_MEAN
IMAGENET_DEFAULT_STD = effdet.data.loader.IMAGENET_DEFAULT_STD


def create_preprocessing_function(image_size, raw=False):
    """Factory that returns a preprocessing closure for a given model input size.

    Applies COCO-eval transforms (resize + pad to image_size, convert to channels-first np.ndarray).
    If raw=True: returns the np.ndarray directly (used when client will compress before sending).
    If raw=False: wraps in torch.Tensor with batch dim (used when sending uncompressed tensor).
    """
    def inner_preproc(image):
        inputs, _ = effdet.data.transforms.transforms_coco_eval(
            image_size, use_prefetcher=True
        )(image, {})
        if raw:
            return inputs
        else:
            return torch.from_numpy(inputs).unsqueeze_(0)

    return inner_preproc


class EfficientDetProfiler:
    """Loads a trained EfficientDet checkpoint and provides GPU inference.

    Init pipeline: load checkpoint → extract inner model → set eval mode →
    wrap in DetBenchPredict (handles NMS + bbox decoding) → move to GPU.
    Precomputes ImageNet normalization tensors on GPU for fast in-place normalize.
    """
    def __init__(
        self, checkpoint_path: str, base_model: str, device: str, num_classes: int
    ):
        # Load PyTorch Lightning checkpoint trained on Waymo Open Dataset
        checkpoint_model = EfficientDetModel.load_from_checkpoint(
            checkpoint_path=checkpoint_path,
            base_model=base_model,
            num_classes=num_classes,
        )
        image_size = checkpoint_model.model.config["image_size"]
        # Unwrap inner model from Lightning wrapper
        model = checkpoint_model.model.model
        model = model.eval()
        # DetBenchPredict wraps raw model with NMS and bounding box decoding
        model = effdet.DetBenchPredict(model)
        self.device_name = device
        self.model = model.to(device)

        self.image_transforms = effdet.data.transforms.transforms_coco_eval(
            image_size, use_prefetcher=True
        )

        # Precompute ImageNet normalization parameters (scaled to 0-255 range)
        self.image_mean = (
            torch.tensor([x * 255 for x in IMAGENET_DEFAULT_MEAN])
            .view(1, 3, 1, 1)
            .to(device)
        )
        # FIXED: Was incorrectly using IMAGENET_DEFAULT_MEAN instead of STD
        self.image_std = (
            torch.tensor([x * 255 for x in IMAGENET_DEFAULT_STD])
            .view(1, 3, 1, 1)
            .to(device)
        )

    def preprocess(self, image: Image.Image):
        inputs, _ = self.image_transforms(image, {})
        return torch.from_numpy(inputs).unsqueeze_(0)

    def to_device(self, preprocessed_inputs: torch.Tensor):
        return preprocessed_inputs.to(self.device_name)

    def predict(self, inputs):
        # In-place ImageNet normalization: (pixel - mean) / std, on GPU
        # Returns tensor of shape [batch, num_detections, 6] where each detection is
        # [x_min, y_min, x_max, y_max, score, label]
        inputs = inputs.float().sub_(self.image_mean).div_(self.image_std)
        return self.model(inputs)

    def postprocess(self, outputs):
        """Postprocess model outputs (currently unused, outputs are handled by caller)."""
        pass


def compress_image(image: Image.Image, quality: Optional[int] = None) -> np.ndarray:
    """Compresses an image using JPEG or PNG.

    Args:
        image: The image to compress (PIL Image).
        quality: The JPEG quality of the compression (0-100). Uses lossless PNG
            compression if set to None.

    Returns:
        The compressed image as a numpy array of bytes.
    """
    image_np = np.array(image)

    LOGGER.debug(
        "Compressing image: input_shape=%s, quality=%s, format=%s",
        image_np.shape,
        quality if quality is not None else "PNG",
        "JPEG" if quality is not None else "PNG"
    )

    # Encode image to JPEG or PNG format
    if quality is not None:
        success, compressed_image = cv2.imencode(
            ".jpg", image_np, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
    else:
        success, compressed_image = cv2.imencode(".png", image_np)

    if not success:
        LOGGER.error("Failed to encode image with quality=%s", quality)
        raise SerializationError(f"Image compression failed with quality={quality}")

    LOGGER.debug(
        "Image compressed successfully: original_size=%d bytes, compressed_size=%d bytes, "
        "compression_ratio=%.2f%%",
        image_np.nbytes,
        compressed_image.nbytes,
        (compressed_image.nbytes / image_np.nbytes) * 100
    )

    return compressed_image


def decompress_image(compressed_image: np.ndarray) -> Image.Image:
    """Decompresses a JPEG or PNG compressed image.

    Args:
        compressed_image: Compressed image data as a numpy array of bytes.

    Returns:
        Decompressed PIL Image.

    Raises:
        RuntimeError: If decompression fails (corrupt data or invalid format).
    """
    LOGGER.debug("Decompressing image: compressed_size=%d bytes", compressed_image.nbytes)

    # Decode compressed image (cv2.IMREAD_COLOR = 1, returns BGR format)
    decompressed_image = cv2.imdecode(compressed_image, cv2.IMREAD_COLOR)

    if decompressed_image is None:
        LOGGER.error("Failed to decode compressed image of size %d bytes", compressed_image.nbytes)
        raise SerializationError("Image decompression failed - corrupt or invalid format")

    LOGGER.debug(
        "Image decompressed successfully: output_shape=%s, size=%d bytes",
        decompressed_image.shape,
        decompressed_image.nbytes
    )

    # TODO: cv2.imdecode returns BGR format, but PIL.Image.fromarray expects RGB.
    # This may cause color channel issues downstream. Consider converting with:
    # decompressed_rgb = cv2.cvtColor(decompressed_image, cv2.COLOR_BGR2RGB)
    # Then return Image.fromarray(decompressed_rgb)
    # Need to verify if this affects detection accuracy before changing.
    return Image.fromarray(decompressed_image)
