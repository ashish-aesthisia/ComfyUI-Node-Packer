#!/usr/bin/env python3
"""
build_scripts/build.py

Builds a ComfyUI-loadable package folder where the node implementation is a
Nuitka .so / .pyd extension and private dependencies live under the package's
own namespace.

Usage:
    python build_scripts/build.py

Output:
    dist/comfy_node_isolation_test/
        __init__.py
        nodes.cpython-312-darwin.so                 (macOS)
        nodes.cpython-312-x86_64-linux-gnu.so       (Linux)
        nodes.pyd                                    (Windows)
        _vendor/PIL/                                 (private Pillow copy)

Requirements:
    pip install nuitka Pillow torch numpy
    # Linux also needs: apt install patchelf
    # macOS: brew install ccache  (optional, speeds up rebuilds)
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
REQUIREMENTS_FILE = ROOT / "my_node" / "requirements.txt"

IMPORT_NAME_OVERRIDES = {
    "Pillow": ["PIL"],
    "humanize": ["humanize"],
    "python-slugify": ["slugify"],
}

DIST_DIR = ROOT / "dist"
BUILD_DIR = ROOT / "build" / "nuitka_stage"
VENDOR_SITE_DIR = ROOT / "build" / "vendor_site"
PACKAGE_NAME = "comfy_node_isolation_test"
PACKAGE_DIR = DIST_DIR / PACKAGE_NAME
VENDOR_DIR = PACKAGE_DIR / "_vendor"

IMPORT_REWRITES = {
    "from PIL import Image, ImageFilter, ImageEnhance": (
        f"from {PACKAGE_NAME}._vendor.PIL import Image, ImageFilter, ImageEnhance"
    ),
    "import humanize": (
        f"from {PACKAGE_NAME}._vendor import humanize"
    ),
    "from slugify import slugify": (
        f"from {PACKAGE_NAME}._vendor.slugify import slugify"
    ),
}
DIST_DIR.mkdir(exist_ok=True)

def read_private_imports() -> list[str]:
    imports = []
    for line in REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        package_name = parse_requirement_name(line)
        if package_name is None:
            continue
        imports.extend(resolve_import_names(package_name))
    return imports

def parse_requirement_name(line: str) -> str | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
        if separator in line:
            return line.split(separator, 1)[0].strip()
    return line.strip()

def resolve_import_names(package_name: str) -> list[str]:
    return IMPORT_NAME_OVERRIDES.get(package_name, [package_name.replace("-", "_")])


def install_requirements_to_vendor_site():
    """Install node requirements into an isolated build folder."""
    if VENDOR_SITE_DIR.exists():
        shutil.rmtree(VENDOR_SITE_DIR)
    VENDOR_SITE_DIR.mkdir(parents=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--target",
        str(VENDOR_SITE_DIR),
        "-r",
        str(REQUIREMENTS_FILE),
    ]
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n[FAILED] Requirement install exited with code", result.returncode)
        sys.exit(result.returncode)


def copy_private_package(package_name: str) -> Path:
    """Copy a dependency from the isolated vendor site into the package namespace."""
    source_dir = VENDOR_SITE_DIR / package_name
    if not source_dir.is_dir():
        raise RuntimeError(f"Cannot locate package to vendor: {package_name} in {VENDOR_SITE_DIR}")

    target_dir = VENDOR_DIR / package_name
    shutil.copytree(
        source_dir,
        target_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return target_dir


def make_compilation_source() -> Path:
    """Create a temporary node source that imports vendored private deps."""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    source = (ROOT / "my_node" / "nodes.py").read_text(encoding="utf-8")
    for original, rewritten in IMPORT_REWRITES.items():
        source = source.replace(original, rewritten)
    staged = BUILD_DIR / "nodes.py"
    staged.write_text(source, encoding="utf-8")
    return staged


def write_package_init():
    (PACKAGE_DIR / "__init__.py").write_text(
        '\"\"\"Generated ComfyUI entry point for the compiled my_node package.\"\"\"\n\n'
        "from .nodes import ColorGradeNode, SharpnessNode\n\n"
        "NODE_CLASS_MAPPINGS = {\n"
        '    "MyNode_ColorGrade": ColorGradeNode,\n'
        '    "MyNode_Sharpness": SharpnessNode,\n'
        "}\n\n"
        "NODE_DISPLAY_NAME_MAPPINGS = {\n"
        '    "MyNode_ColorGrade": "Color Grade (MyNode)",\n'
        '    "MyNode_Sharpness": "Sharpness (MyNode)",\n'
        "}\n\n"
        '__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]\n',
        encoding="utf-8",
    )


def build():
    # ComfyUI discovers folders with an __init__.py, not bare extension files.
    if PACKAGE_DIR.exists():
        shutil.rmtree(PACKAGE_DIR)
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    if VENDOR_SITE_DIR.exists():
        shutil.rmtree(VENDOR_SITE_DIR)
    PACKAGE_DIR.mkdir(parents=True)
    VENDOR_DIR.mkdir(parents=True)
    (VENDOR_DIR / "__init__.py").write_text("", encoding="utf-8")

    install_requirements_to_vendor_site()
    private_imports = read_private_imports()
    vendored = [copy_private_package(import_name) for import_name in private_imports]
    staged_source = make_compilation_source()

    cmd = [
        sys.executable, "-m", "nuitka",

        # .so/.pyd extension module, not a standalone executable.
        "--module",

        # Keep ComfyUI-owned ABI-heavy deps host-provided.
        "--nofollow-import-to=torch",
        "--nofollow-import-to=numpy",
        "--nofollow-import-to=torchvision",
        "--nofollow-import-to=torchaudio",

        # The private deps are copied under _vendor; do not compile them into
        # the node module because native packages like Pillow need sidecars.
        f"--nofollow-import-to={PACKAGE_NAME}._vendor.*",

        f"--output-dir={PACKAGE_DIR}",
        "--remove-output",
        "--assume-yes-for-downloads",

        str(staged_source),
    ]

    print("=" * 60)
    print(f"Building {PACKAGE_NAME} with Nuitka...")
    print("=" * 60)

    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        print("\n[FAILED] Build exited with code", result.returncode)
        sys.exit(result.returncode)

    write_package_init()

    outputs = list(PACKAGE_DIR.glob("nodes*.so")) + list(PACKAGE_DIR.glob("nodes*.pyd"))
    if outputs:
        out = outputs[0]
        print(f"\n[OK] Built package: {PACKAGE_DIR}")
        print(f"     Binary: {out.name}")
        print(f"     Size:   {out.stat().st_size / 1024:.1f} KB")
        print(f"     Vendored packages: {', '.join(p.name for p in vendored)}")
        print("\nInstall:")
        print(f"  cp -R '{PACKAGE_DIR}' /path/to/ComfyUI/custom_nodes/")
    else:
        print(f"\n[WARN] Build ok but no nodes .so/.pyd found -- check {PACKAGE_DIR}/ manually")
        for f in PACKAGE_DIR.iterdir():
            print(f"  {f.name}")


if __name__ == "__main__":
    build()
