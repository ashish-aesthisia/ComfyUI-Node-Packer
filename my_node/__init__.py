"""
my_node/__init__.py

A realistic ComfyUI custom node that:
  - Has its own dependency (Pillow) for image manipulation
  - Exposes NODE_CLASS_MAPPINGS so ComfyUI can discover it
  - Does real tensor work (IMAGE is [B,H,W,C] float32 torch tensor)

When compiled with Nuitka, Pillow gets baked in and never touches
the host pip environment.
"""

from .nodes import ColorGradeNode, SharpnessNode

NODE_CLASS_MAPPINGS = {
    "MyNode_ColorGrade": ColorGradeNode,
    "MyNode_Sharpness": SharpnessNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MyNode_ColorGrade": "Color Grade (MyNode)",
    "MyNode_Sharpness": "Sharpness (MyNode)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
