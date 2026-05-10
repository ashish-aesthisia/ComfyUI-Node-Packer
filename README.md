# ComfyUI isolated custom node (POC)

ComfyUI loads every custom node in one Python process. If each node installs into the same environment with `pip install -r requirements.txt`, versions clash and are painful to unwind.

This repo builds a normal custom-node folder: Nuitka-compiled node code plus dependencies copied under a private `_vendor` tree, so imports like `PIL` resolve to `comfy_node_isolation_test._vendor.PIL` instead of whatever the host has installed.

It is not one monolithic binary. Native packages (for example Pillow) ship as directories with their extension sidecars; Nuitka still compiles your node module.

## Output

```text
dist/comfy_node_isolation_test/
  __init__.py
  nodes.cpython-3xx-<platform>.so   # or .pyd on Windows
  _vendor/
    __init__.py
    PIL/
    humanize/
    slugify/
```

Leave the heavy stack to ComfyUI: `torch`, `numpy`, CUDA, and ComfyUI itself are not vendored here.

## Setup

From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install nuitka torch numpy
pip install -r my_node/requirements.txt
```

The second line is required for `python test_runner/test_node.py --source`, because `my_node/nodes.py` imports Pillow and the other libraries the same way ComfyUI would before you ship the built package. The Nuitka output vendors those under `_vendor`, so end users of the **built** folder do not install `my_node/requirements.txt` into ComfyUI.

- Linux: install `patchelf` before building.
- Windows: Visual Studio Build Tools for Nuitka.

`my_node/requirements.txt` currently looks like:

```text
Pillow==12.2.0
humanize
python-slugify
```

## Build and test

```bash
python test_runner/test_node.py --source
python build_scripts/build.py
python test_runner/test_node.py --binary
python test_runner/verify_isolation.py
```

`verify_isolation.py` checks that loading the built node does not replace the process-wide `PIL` module.

## Try it in ComfyUI

Copy the whole folder under `dist/comfy_node_isolation_test` into `ComfyUI/custom_nodes/` (not only the `.so` / `.pyd`). Then start ComfyUI as you usually do.

In the UI, look for **Color Grade (MyNode)** and **Sharpness (MyNode)**. A minimal chain: Load Image → those two → Preview Image.

## How the build works

`build_scripts/build.py` installs `my_node/requirements.txt` with `pip --target`, copies the resolved import packages into `_vendor`, rewrites imports in a staged copy of `nodes.py`, then runs Nuitka with `--module`. Example rewrite:

```python
from PIL import Image, ImageFilter, ImageEnhance
```

becomes:

```python
from comfy_node_isolation_test._vendor.PIL import Image, ImageFilter, ImageEnhance
```

## Adding new dependencies

1. Add the pip requirement to `my_node/requirements.txt` (pin versions if you care about reproducibility).

2. In `build_scripts/build.py`, if the **import name** is not the pip name with hyphens turned into underscores, extend `IMPORT_NAME_OVERRIDES`. Examples already there: `Pillow` → `PIL`, `python-slugify` → `slugify`.

3. Still in `build.py`, add entries to `IMPORT_REWRITES`: one exact string for each import line (or pattern) in `my_node/nodes.py` that must point at `_vendor`. The build does literal string replacement, so the key must match your source text.

4. In `my_node/nodes.py`, use normal third-party imports (the readable form). Rebuild and run the tests.

If something imports at runtime but was never listed as a top-level package to copy, you may need to vendor a transitive dependency too (see limits below).

## Limits

- Import rewriting is string-based; a real tool would use an AST or import hooks.
- Only packages derived from `requirements.txt` plus `IMPORT_NAME_OVERRIDES` are copied. Transitive imports (for example `text_unidecode` for slugify) may need to be added explicitly until vendoring follows installed metadata.
- Native wheels must be copied whole; validate on the OS and Python version you ship for.
- The compiled extension matches your build machine’s Python, OS, and CPU; rebuild for the ComfyUI runtime you target.

## Do not vendor these in the node package

Keep these on the host unless you have a very specific reason not to:

```text
torch
numpy
torchvision
torchaudio
ComfyUI modules
CUDA / runtime libraries
```

The idea is small, node-specific libraries under `_vendor`, and ComfyUI owns the GPU and core stack.
