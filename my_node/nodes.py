"""
my_node/nodes.py

Actual node logic. Uses Pillow (PIL) for image processing —
this is the dep we want to isolate from the host environment.
"""

import torch
import numpy as np

# This is the "problematic" dep — version conflicts with other nodes
# After Nuitka compilation, this import resolves to the BAKED-IN Pillow,
# never the host's pip-installed version.
from PIL import Image, ImageFilter, ImageEnhance


def tensor_to_pil(tensor: torch.Tensor) -> list[Image.Image]:
    """Convert ComfyUI IMAGE tensor [B,H,W,C] float32 → list of PIL images."""
    batch = (tensor.numpy() * 255).clip(0, 255).astype(np.uint8)
    return [Image.fromarray(frame) for frame in batch]


def pil_to_tensor(images: list[Image.Image]) -> torch.Tensor:
    """Convert list of PIL images → ComfyUI IMAGE tensor [B,H,W,C] float32."""
    arrays = [np.array(img).astype(np.float32) / 255.0 for img in images]
    return torch.from_numpy(np.stack(arrays))


class ColorGradeNode:
    """
    Applies simple color grading: brightness, contrast, saturation.
    Depends on PIL.ImageEnhance — baked into the binary after Nuitka compile.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "brightness": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 3.0, "step": 0.05}),
                "contrast":   ("FLOAT", {"default": 1.0, "min": 0.1, "max": 3.0, "step": 0.05}),
                "saturation": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 3.0, "step": 0.05}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "apply"
    CATEGORY = "my_node/color"

    def apply(self, image: torch.Tensor, brightness: float, contrast: float, saturation: float):
        pil_images = tensor_to_pil(image)
        results = []
        for img in pil_images:
            img = ImageEnhance.Brightness(img).enhance(brightness)
            img = ImageEnhance.Contrast(img).enhance(contrast)
            img = ImageEnhance.Color(img).enhance(saturation)
            results.append(img)
        return (pil_to_tensor(results),)


class SharpnessNode:
    """
    Applies an unsharp mask via PIL.ImageFilter.
    Another PIL dep — also baked in.
    """

    SHARPEN_MODES = ["SHARPEN", "SMOOTH", "DETAIL", "EDGE_ENHANCE"]

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image":  ("IMAGE",),
                "mode":   (cls.SHARPEN_MODES, {"default": "SHARPEN"}),
                "passes": ("INT", {"default": 1, "min": 1, "max": 5}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "apply"
    CATEGORY = "my_node/filter"

    # Force re-execution every time (no caching)
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def apply(self, image: torch.Tensor, mode: str, passes: int):
        filter_map = {
            "SHARPEN":      ImageFilter.SHARPEN,
            "SMOOTH":       ImageFilter.SMOOTH,
            "DETAIL":       ImageFilter.DETAIL,
            "EDGE_ENHANCE": ImageFilter.EDGE_ENHANCE,
        }
        pil_filter = filter_map[mode]
        pil_images = tensor_to_pil(image)
        results = []
        for img in pil_images:
            for _ in range(passes):
                img = img.filter(pil_filter)
            results.append(img)
        return (pil_to_tensor(results),)
