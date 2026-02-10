"""EfficientDet implementation using PyTorch Lightning.

Based on https://gist.github.com/Chris-hughes10/73628b1d8d6fc7d359b3dcbbbb8869d7.
"""

from typing import Any

import effdet
import pytorch_lightning
import torch


IMAGENET_DEFAULT_MEAN = effdet.data.loader.IMAGENET_DEFAULT_MEAN
IMAGENET_DEFAULT_STD = effdet.data.loader.IMAGENET_DEFAULT_STD


def create_model(model_name: str, num_classes: int) -> effdet.efficientdet.EfficientDet:
    config = effdet.get_efficientdet_config(model_name)
    config.update(num_classes=num_classes)
    config.update({"num_classes": num_classes})

    return effdet.EfficientDet(config, pretrained_backbone=True)


class EfficientDetModel(pytorch_lightning.LightningModule):
    def __init__(
        self,
        base_model: str,
        num_classes: int = 1,
        lr: float = 0.01,
        weight_decay: float = 0.1,
        normalize_images: bool = True,
    ):
        super().__init__()
        model = create_model(base_model, num_classes)
        self.model = effdet.DetBenchTrain(model)
        self.lr = lr
        self.weight_decay = weight_decay
        self.normalize_images = normalize_images
        if normalize_images:
            image_mean = torch.tensor([x * 255 for x in IMAGENET_DEFAULT_MEAN]).view(
                1, 3, 1, 1
            )
            self.register_buffer("image_mean", image_mean, persistent=False)
            image_std = torch.tensor([x * 255 for x in IMAGENET_DEFAULT_MEAN]).view(
                1, 3, 1, 1
            )
            self.register_buffer("image_std", image_std, persistent=False)

    def forward(self, images, targets):
        # Normalize images on-device.
        # effdet normalizes images on-device using the PrefetchLoader, which may cause
        # OOM errors with PyTorch Lightning.
        if self.normalize_images:
            images = images.float().sub_(self.image_mean).div_(self.image_std)
        return self.model(images, targets)

    def common_step(self, batch, batch_idx):
        images, annotations = batch
        if self.normalize_images:
            dtype = next(self.parameters()).dtype
            images = images.to(dtype).sub_(self.image_mean).div_(self.image_std)
        return self.model(images, annotations)

    def training_step(self, batch, batch_idx):
        loss_dict = self.common_step(batch, batch_idx)

        for k, v in loss_dict.items():
            self.log(f"train_{k}", v.item())

        return loss_dict["loss"]

    def validation_step(self, batch, batch_idx):
        # During validation, common_step includes loss and detections.
        output = self.common_step(batch, batch_idx)
        del output["detections"]

        for k, v in output.items():
            self.log(f"validation_{k}", v.item(), sync_dist=True)

        return output["loss"]

    def configure_optimizers(self):
        return torch.optim.AdamW(
            self.model.parameters(), lr=self.lr, weight_decay=self.weight_decay
        )

    @property
    def config(self) -> dict[str, Any]:
        return self.model.config

