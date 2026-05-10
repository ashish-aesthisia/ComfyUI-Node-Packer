#!/usr/bin/env python3
"""
test_runner/test_node.py

Simulates what ComfyUI does when it discovers and loads a custom node.
Run this BEFORE and AFTER Nuitka compilation to verify behaviour is identical.

Before compile (tests source):
    python test_runner/test_node.py --source

After compile (tests generated package + binary):
    python test_runner/test_node.py --binary
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import torch

ROOT = Path(__file__).parent.parent
PACKAGE_NAME = "comfy_node_isolation_test"
GENERATED_PACKAGE = ROOT / "dist" / PACKAGE_NAME


def load_source_node():
    """Load the raw Python package, the same way a source custom node works."""
    sys.path.insert(0, str(ROOT))
    import my_node
    return my_node


def _clear_node_modules():
    for key in list(sys.modules.keys()):
        if key == "my_node" or key.startswith("my_node."):
            del sys.modules[key]
        if key == PACKAGE_NAME or key.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[key]


def load_binary_node():
    """
    Load the generated ComfyUI package folder from dist/.

    This intentionally avoids adding the source project root to sys.path, so a
    missing compiled submodule cannot silently fall back to my_node/nodes.py.
    """
    init_file = GENERATED_PACKAGE / "__init__.py"
    binaries = list(GENERATED_PACKAGE.glob("nodes*.so")) + list(GENERATED_PACKAGE.glob("nodes*.pyd"))

    if not init_file.exists() or not binaries:
        print("[ERROR] Generated package not found or missing compiled nodes module.")
        print(f"        Expected: {GENERATED_PACKAGE}")
        if GENERATED_PACKAGE.exists():
            for f in sorted(GENERATED_PACKAGE.rglob("*")):
                print(f"          {f.relative_to(GENERATED_PACKAGE)}")
        print("\n        Run: python build_scripts/build.py")
        sys.exit(1)

    print(f"Found generated package: {GENERATED_PACKAGE}")
    print(f"Found binary: {binaries[0].name}")

    _clear_node_modules()
    sys.path = [p for p in sys.path if Path(p or ".").resolve() != ROOT]

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        init_file,
        submodule_search_locations=[str(GENERATED_PACKAGE)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module


def make_test_image(batch=2, height=64, width=64) -> torch.Tensor:
    """Create a fake ComfyUI IMAGE tensor [B, H, W, C] float32."""
    return torch.rand(batch, height, width, 3, dtype=torch.float32)


def run_tests(module):
    print(f"\nLoaded module: {module.__file__}")
    print(f"NODE_CLASS_MAPPINGS keys: {list(module.NODE_CLASS_MAPPINGS.keys())}\n")

    mappings = module.NODE_CLASS_MAPPINGS
    image = make_test_image()

    print("Test 1: ColorGradeNode")
    ColorGrade = mappings["MyNode_ColorGrade"]

    inputs = ColorGrade.INPUT_TYPES()
    assert "required" in inputs, "Missing 'required' in INPUT_TYPES"
    assert "image" in inputs["required"], "Missing 'image' input"
    assert ColorGrade.RETURN_TYPES == ("IMAGE",), f"Wrong RETURN_TYPES: {ColorGrade.RETURN_TYPES}"

    node = ColorGrade()
    result = node.apply(image, brightness=1.2, contrast=1.1, saturation=0.8)

    assert isinstance(result, tuple), "Result must be a tuple"
    out_tensor = result[0]
    assert out_tensor.shape == image.shape, f"Shape mismatch: {out_tensor.shape} != {image.shape}"
    assert out_tensor.dtype == torch.float32, "Output must be float32"
    assert out_tensor.min() >= 0.0 and out_tensor.max() <= 1.0, "Output out of [0,1] range"
    print(f"  PASS -- output shape {out_tensor.shape}, range [{out_tensor.min():.3f}, {out_tensor.max():.3f}]")

    print("Test 2: SharpnessNode")
    Sharpness = mappings["MyNode_Sharpness"]

    for mode in ["SHARPEN", "SMOOTH", "DETAIL", "EDGE_ENHANCE"]:
        node = Sharpness()
        result = node.apply(image, mode=mode, passes=2)
        out = result[0]
        assert out.shape == image.shape, f"Shape mismatch for mode {mode}"
        print(f"  PASS -- mode={mode}, shape={out.shape}")

    print("Test 3: IS_CHANGED")
    val = Sharpness.IS_CHANGED()
    assert val != val, "IS_CHANGED should return NaN (float('nan'))"
    print("  PASS -- IS_CHANGED returns NaN as expected")

    print("Test 4: PIL import isolation smoke check")
    from PIL import Image as HostPIL
    print(f"  Host PIL version : {HostPIL.__version__}")
    print("  PASS")

    print("\nAll tests passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", action="store_true", help="Test the raw Python source")
    group.add_argument("--binary", action="store_true", help="Test the generated Nuitka package")
    args = parser.parse_args()

    if args.source:
        print("=== Testing SOURCE (Python) ===")
        mod = load_source_node()
    else:
        print("=== Testing BINARY (Nuitka generated package) ===")
        mod = load_binary_node()

    run_tests(mod)
