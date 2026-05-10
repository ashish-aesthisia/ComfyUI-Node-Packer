#!/usr/bin/env python3
"""
test_runner/verify_isolation.py

After building the generated package, run this to smoke-test that the compiled
node can execute while host PIL remains importable as the normal top-level PIL.

Usage:
    python test_runner/verify_isolation.py
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PACKAGE_NAME = "comfy_node_isolation_test"
GENERATED_PACKAGE = ROOT / "dist" / PACKAGE_NAME


def get_pil_version():
    from PIL import Image
    return Image.__version__


def load_generated_package():
    init_file = GENERATED_PACKAGE / "__init__.py"
    binaries = list(GENERATED_PACKAGE.glob("nodes*.so")) + list(GENERATED_PACKAGE.glob("nodes*.pyd"))
    if not init_file.exists() or not binaries:
        print("\n[ERROR] Generated package missing. Run build first:")
        print("  python build_scripts/build.py")
        sys.exit(1)

    for key in list(sys.modules.keys()):
        if key == PACKAGE_NAME or key.startswith(f"{PACKAGE_NAME}."):
            del sys.modules[key]
        if key == "my_node" or key.startswith("my_node."):
            del sys.modules[key]

    sys.path = [p for p in sys.path if Path(p or ".").resolve() != ROOT]

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        init_file,
        submodule_search_locations=[str(GENERATED_PACKAGE)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)
    return module, binaries[0]


def run():
    print("=== Isolation Verification ===\n")

    host_version = get_pil_version()
    host_pil_before = sys.modules.get("PIL")
    print(f"1. Host PIL version before binary load: {host_version}")
    print(f"   Host PIL module id before: {id(host_pil_before)}")

    module, binary = load_generated_package()
    print(f"\n2. Loaded generated package: {module.__file__}")
    print(f"   Binary: {binary.name}")
    print(f"   Nodes: {list(module.NODE_CLASS_MAPPINGS.keys())}")

    import torch
    image = torch.rand(1, 32, 32, 3)
    node = module.NODE_CLASS_MAPPINGS["MyNode_ColorGrade"]()
    result = node.apply(image, brightness=1.5, contrast=0.9, saturation=1.1)
    print(f"\n3. Node executed OK -- output shape: {result[0].shape}")

    host_pil_after = sys.modules.get("PIL")
    print("\n4. Host PIL after binary execution")
    print(f"   Version: {get_pil_version()}")
    print(f"   Module id after: {id(host_pil_after)}")
    assert host_pil_after is host_pil_before, "Host PIL module object was replaced"
    print("   PASS -- host PIL module was not replaced")

    print("\nIsolation smoke test passed.")


if __name__ == "__main__":
    run()
