import time
from dataclasses import dataclass
from typing import Any, Optional

import cv2
import numpy as np
from PIL import Image


def compress_image(
    image: Image.Image, quality: Optional[int] = None
) -> tuple[Image.Image, int, float, float]:
    """Compresses an image using JPEG or PNG.

    Args:
        image: The image to compress.
        quality: The JPEG quality of the compression. Uses lossless PNG compression if
            set to None.

    Returns:
        The compressed image, the byte size of the compressed image, the runtime of the
        compression algorithm, and the runtime of the decompression algorithm in
        milliseconds.
    """
    image_np = np.array(image)

    compression_start_time = time.time()
    if quality is not None:
        _, compressed_image = cv2.imencode(
            ".jpg", image_np, [cv2.IMWRITE_JPEG_QUALITY, quality]
        )
    else:
        _, compressed_image = cv2.imencode(".png", image_np)
    compression_runtime = 1e3 * (time.time() - compression_start_time)
    # Convert compressed_image back to a 3D numpy array

    decompression_start_time = time.time()
    compressed_image_original_shape = cv2.imdecode(compressed_image, 1)
    decompression_runtime = 1e3 * (time.time() - decompression_start_time)
    return (
        Image.fromarray(compressed_image_original_shape),
        compressed_image.size,
        compression_runtime,
        decompression_runtime,
    )


@dataclass(frozen=True)
class BoundingBox2D:
    """A 2D Bounding Box."""

    x_min: int
    x_max: int
    y_min: int
    y_max: int

    def __post_init__(self):
        assert self.x_min < self.x_max
        assert self.y_min < self.y_max

    @property
    def width(self):
        return self.x_max - self.x_min

    @property
    def height(self):
        return self.y_max - self.y_min

    @property
    def center(self) -> tuple[float, float]:
        center_x = (self.x_min + self.x_max) / 2
        center_y = (self.y_min + self.y_max) / 2

        return (center_x, center_y)


@dataclass(frozen=True)
class LabeledBoundingBox2D(BoundingBox2D):
    """A 2D Bounding Box with a label."""

    label: str | int


@dataclass(frozen=True)
class PredictedBoundingBox2D(LabeledBoundingBox2D):
    """A 2D Bounding Box with a label and a confidence score."""

    score: float

    @staticmethod
    def from_huggingface_pipeline_format(
        fields: dict[str, Any],
    ) -> "PredictedBoundingBox2D":
        score = fields["score"]
        label = fields["label"]
        box = fields["box"]
        x_min = box["xmin"]
        x_max = box["xmax"]
        y_min = box["ymin"]
        y_max = box["ymax"]

        return PredictedBoundingBox2D(x_min, x_max, y_min, y_max, score, label)
