#!/usr/bin/env python3
"""
pack.py — ComfyUI custom node isolation packer.

Point it at any ComfyUI custom node directory and it will:
  1. Read requirements.txt and install deps into an isolated build folder
  2. Auto-discover every top-level package that was installed
  3. Rewrite vendored imports in the node source via AST
  4. Compile the node module with Nuitka
  5. Assemble dist/<package_name>/ ready to drop into ComfyUI/custom_nodes/

No setup needed — nuitka is installed automatically on first run.

Usage:
    python pack.py <node_dir>
    python pack.py my_node
    python pack.py /path/to/someone_elses_node
    python pack.py my_node --name my_custom_pkg    # override output name
    python pack.py my_node --output /tmp/dist       # override output dir
    python pack.py my_node --skip-compile           # vendor only, no Nuitka

Platform extras (not installed automatically):
    Linux:   apt install patchelf
    macOS:   brew install ccache  (optional, speeds up rebuilds)
    Windows: Visual Studio Build Tools (required by Nuitka)
"""

import ast
import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Bootstrap — ensure nuitka is available, auto-install if not
# ---------------------------------------------------------------------------

def _ensure_tool_env() -> str:
    """
    Return the Python executable to use for pip and Nuitka.

    Fast path: if nuitka is already importable in the running interpreter,
    return sys.executable immediately.

    Otherwise, create .tool_env/ next to pack.py (once), install nuitka into
    it, and return that env's Python. Subsequent runs reuse the cached env.
    """
    if importlib.util.find_spec("nuitka") is not None:
        return sys.executable

    tool_env = Path(__file__).parent / ".tool_env"
    tool_python = (
        tool_env / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else tool_env / "bin" / "python"
    )

    if not tool_python.exists():
        print("[bootstrap] nuitka not found — creating .tool_env/ ...")
        subprocess.run([sys.executable, "-m", "venv", str(tool_env)], check=True)

    probe = subprocess.run(
        [str(tool_python), "-c", "import nuitka"],
        capture_output=True,
    )
    if probe.returncode != 0:
        print("[bootstrap] Installing nuitka into .tool_env/ ...")
        subprocess.run(
            [str(tool_python), "-m", "pip", "install", "nuitka"],
            check=True,
        )
        print("[bootstrap] nuitka ready.\n")

    return str(tool_python)


# Set once in main(); all subprocesses use this Python so pip-installed native
# packages and the Nuitka-compiled extension share the same ABI.
_TOOL_PYTHON: str = sys.executable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Packages that live in the ComfyUI host — never vendor these.
_HOST_PACKAGES = frozenset({
    "torch", "numpy", "torchvision", "torchaudio",
    "comfyui", "folder_paths", "nodes",
})

# pip metadata directories and non-package dirs — not importable.
_PIP_META_SUFFIXES = (".dist-info", ".data")
_PIP_SCRIPT_DIRS   = frozenset({"bin", "scripts", "Scripts"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_package_name(name: str) -> str:
    """Convert any string into a valid Python package identifier."""
    name = name.lower().replace("-", "_").replace(" ", "_")
    if name and name[0].isdigit():
        name = "_" + name
    return name


def derive_package_name(node_dir: Path) -> str:
    return _normalize_package_name(node_dir.resolve().name)


def get_vendored_packages(vendor_site: Path) -> set[str]:
    """Return all importable top-level names installed under vendor_site."""
    packages: set[str] = set()
    for item in vendor_site.iterdir():
        if item.name == "__pycache__":
            continue
        if item.name in _PIP_SCRIPT_DIRS:
            continue
        if any(item.name.endswith(s) for s in _PIP_META_SUFFIXES):
            continue
        if item.suffix in (".pth", ".egg-link"):
            continue
        if item.is_dir():
            packages.add(item.name)
        elif item.suffix == ".py" and item.stem != "__init__":
            packages.add(item.stem)
        elif item.suffix in (".so", ".pyd"):
            packages.add(item.name.split(".")[0])
    return packages - _HOST_PACKAGES


# ---------------------------------------------------------------------------
# AST import rewriting
# ---------------------------------------------------------------------------

class _ImportRewriter(ast.NodeTransformer):
    def __init__(self, vendored: set[str], package_name: str) -> None:
        self._vendored = vendored
        self._pkg = package_name

    def _vendor_path(self, module: str) -> str | None:
        if module.split(".")[0] in self._vendored:
            return f"{self._pkg}._vendor.{module}"
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
        if node.module and (new := self._vendor_path(node.module)):
            node.module = new
        return node

    def visit_Import(self, node: ast.Import) -> ast.AST:
        new_aliases = []
        for alias in node.names:
            if new_name := self._vendor_path(alias.name):
                new_aliases.append(ast.alias(
                    name=new_name,
                    asname=alias.asname or alias.name.split(".")[0],
                ))
            else:
                new_aliases.append(alias)
        node.names = new_aliases
        return node


def rewrite_imports(source: str, vendored: set[str], package_name: str) -> str:
    tree = ast.parse(source)
    new_tree = _ImportRewriter(vendored, package_name).visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


# ---------------------------------------------------------------------------
# Build steps
# ---------------------------------------------------------------------------

def install_requirements(req_file: Path, vendor_site: Path) -> None:
    if vendor_site.exists():
        shutil.rmtree(vendor_site)
    vendor_site.mkdir(parents=True)
    cmd = [
        _TOOL_PYTHON, "-m", "pip", "install",
        "--target", str(vendor_site),
        "-r", str(req_file),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[FAILED] pip install exited with code {result.returncode}")
        sys.exit(result.returncode)


def copy_vendor_packages(
    vendor_site: Path, vendor_dir: Path, packages: set[str]
) -> list[str]:
    copied: list[str] = []
    for pkg in sorted(packages):
        src_dir = vendor_site / pkg
        src_py  = vendor_site / f"{pkg}.py"
        if src_dir.is_dir():
            shutil.copytree(
                src_dir, vendor_dir / pkg,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            copied.append(pkg)
        elif src_py.exists():
            shutil.copy2(src_py, vendor_dir / f"{pkg}.py")
            copied.append(pkg)
        else:
            print(f"  [warn] package not found in vendor_site, skipping: {pkg}")
    return copied


def find_main_module(node_dir: Path) -> Path:
    """Return nodes.py, or the sole non-convention .py file in the directory."""
    # ComfyUI convention / housekeeping files — never the node logic
    _SKIP = frozenset({
        "__init__.py", "install.py", "setup.py",
        "conftest.py", "__version__.py",
    })
    candidate = node_dir / "nodes.py"
    if candidate.exists():
        return candidate
    others = [f for f in node_dir.glob("*.py") if f.name not in _SKIP]
    if len(others) == 1:
        return others[0]
    if len(others) == 0:
        raise FileNotFoundError(
            f"No compilable .py file found in {node_dir}.\n"
            "  Expected nodes.py or one other .py file (install.py is skipped)."
        )
    raise FileNotFoundError(
        f"Multiple .py files in {node_dir} — cannot pick one automatically:\n"
        + "".join(f"  {f.name}\n" for f in sorted(others))
        + "Rename the main module to nodes.py or use --module to specify it."
    )


def stage_source(
    node_dir: Path, stage_dir: Path, vendored: set[str], package_name: str
) -> Path:
    stage_dir.mkdir(parents=True, exist_ok=True)
    main = find_main_module(node_dir)
    source = main.read_text(encoding="utf-8")
    rewritten = rewrite_imports(source, vendored, package_name)
    staged = stage_dir / main.name
    staged.write_text(rewritten, encoding="utf-8")
    return staged


# Prepended to every __init__.py so all imports inside the node — including
# ones in copied source files — resolve to _vendor/ before the host packages.
_VENDOR_PATH_INJECTION = """\
# -- injected by pack.py --
import os as _p_os, sys as _p_sys
_p_sys.path.insert(0, _p_os.path.join(_p_os.path.dirname(_p_os.path.abspath(__file__)), '_vendor'))
del _p_os, _p_sys
# -- end injection --

"""


def copy_init(node_dir: Path, package_dir: Path) -> None:
    """
    Copy __init__.py and prepend a one-time sys.path injection so that every
    `import X` anywhere in the node finds _vendor/X before the host package.
    """
    src = node_dir / "__init__.py"
    original = src.read_text(encoding="utf-8") if src.exists() else '"""ComfyUI custom node package."""\n'
    (package_dir / "__init__.py").write_text(_VENDOR_PATH_INJECTION + original, encoding="utf-8")


def copy_node_source(node_dir: Path, package_dir: Path) -> list[str]:
    """
    Copy the node's own source files and directories into the dist package so
    internal imports keep working inside ComfyUI.

    Copies:
      - Any .py file at the root (e.g. install.py)
      - Every subdirectory (Python packages, js/, models/, wildcards/, locales/ …)
        Nodes can declare WEB_DIRECTORY, load models, or read data files at runtime —
        copying everything is safer than trying to guess what's needed.

    Skips:
      - __init__.py (handled by copy_init)
      - __pycache__ / *.pyc
      - requirements.txt, .git, hidden files/dirs
      - Anything already placed (e.g. a _vendor/ package with the same name)
    """
    _SKIP_NAMES = frozenset({
        "__init__.py", "__pycache__", "requirements.txt",
        ".git", ".github", ".gitignore",
    })
    copied: list[str] = []
    for item in sorted(node_dir.iterdir()):
        if item.name in _SKIP_NAMES or item.name.startswith("."):
            continue
        dst = package_dir / item.name
        if dst.exists():
            continue  # already placed (e.g. a vendored package with same name)
        if item.is_dir():
            # Copy every subdirectory — nodes can need js/ (WEB_DIRECTORY),
            # models/, wildcards/, locales/, and Python subpackages like modules/.
            shutil.copytree(
                item, dst,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            copied.append(item.name + "/")
        elif item.suffix == ".py":
            shutil.copy2(item, dst)
            copied.append(item.name)
    return copied


def run_nuitka(staged: Path, package_dir: Path, package_name: str) -> None:
    cmd = [
        _TOOL_PYTHON, "-m", "nuitka",
        "--module",
        "--nofollow-import-to=torch",
        "--nofollow-import-to=numpy",
        "--nofollow-import-to=torchvision",
        "--nofollow-import-to=torchaudio",
        f"--nofollow-import-to={package_name}._vendor.*",
        f"--output-dir={package_dir}",
        "--remove-output",
        "--assume-yes-for-downloads",
        str(staged),
    ]
    print("\n" + "=" * 60)
    print(f"Compiling {staged.name} with Nuitka...")
    print("=" * 60)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\n[FAILED] Nuitka exited with code {result.returncode}")
        sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------

def _print_summary(package_dir: Path, copied_vendor: list[str], binary: Path | None) -> None:
    print(f"\n[OK] Built package: {package_dir}")
    if binary:
        print(f"     Binary:  {binary.name}  ({binary.stat().st_size / 1024:.1f} KB)")
    else:
        print(f"     Mode:    pure Python source (no Nuitka binary)")
    print(f"     Vendor:  {', '.join(copied_vendor) or '(none)'}")
    print(f"\nInstall:\n  cp -R '{package_dir}' /path/to/ComfyUI/custom_nodes/")


# ---------------------------------------------------------------------------
# Main pack routine
# ---------------------------------------------------------------------------

def pack(
    node_dir: Path,
    output_dir: Path,
    package_name: str | None = None,
    skip_compile: bool = False,
) -> None:
    node_dir = node_dir.resolve()

    if not node_dir.is_dir():
        print(f"[ERROR] Not a directory: {node_dir}")
        sys.exit(1)

    req_file = node_dir / "requirements.txt"
    if not req_file.exists():
        print(f"[ERROR] No requirements.txt found in {node_dir}")
        sys.exit(1)

    if package_name is None:
        package_name = derive_package_name(node_dir)
    else:
        normalized = _normalize_package_name(package_name)
        if normalized != package_name:
            print(f"[note] --name '{package_name}' is not a valid Python identifier.")
            print(f"       Normalized to: '{normalized}'")
            package_name = normalized

    print(f"\nPacking '{node_dir.name}'  =>  package '{package_name}'")

    build_root  = Path("build") / "pack_stage"
    vendor_site = build_root / "vendor_site"
    stage_dir   = build_root / "nuitka_stage"
    package_dir = output_dir / package_name
    vendor_dir  = package_dir / "_vendor"

    for d in (package_dir, stage_dir, vendor_site):
        if d.exists():
            shutil.rmtree(d)
    package_dir.mkdir(parents=True)
    vendor_dir.mkdir(parents=True)
    (vendor_dir / "__init__.py").write_text("", encoding="utf-8")

    # 1. Install
    print("\n[1/4] Installing requirements...")
    install_requirements(req_file, vendor_site)

    # 2. Discover
    vendored = get_vendored_packages(vendor_site)
    print(f"\n[2/4] Discovered {len(vendored)} vendored top-level packages:")
    print(f"      {', '.join(sorted(vendored))}")

    # 3. Vendor pip packages
    print("\n[3/5] Copying pip packages into _vendor/...")
    copied_vendor = copy_vendor_packages(vendor_site, vendor_dir, vendored)
    print(f"      Copied: {', '.join(copied_vendor)}")

    # 4. Copy the node's own source (subpackages, extra .py files)
    copy_init(node_dir, package_dir)
    print("\n[4/5] Copying node source files...")
    copied_src = copy_node_source(node_dir, package_dir)
    if copied_src:
        print(f"      Copied: {', '.join(copied_src)}")
    else:
        print(f"      (nothing beyond __init__.py)")

    if skip_compile:
        print("\n[5/5] Skipping Nuitka compile (--skip-compile).")
        _print_summary(package_dir, copied_vendor, binary=None)
        return

    # 5. Find, stage, and compile the main module (nodes.py) if present
    print("\n[5/5] Compiling main module with Nuitka...")
    try:
        staged = stage_source(node_dir, stage_dir, vendored, package_name)
    except FileNotFoundError as exc:
        print(f"      [skip] {exc}")
        print(f"      No single main module found — shipping as pure Python source.")
        _print_summary(package_dir, copied_vendor, binary=None)
        return

    run_nuitka(staged, package_dir, package_name)

    binaries = list(package_dir.glob("*.so")) + list(package_dir.glob("*.pyd"))
    _print_summary(package_dir, copied_vendor, binary=binaries[0] if binaries else None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    global _TOOL_PYTHON

    parser = argparse.ArgumentParser(
        description="Pack a ComfyUI custom node for isolated deployment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("node_dir", help="Path to the custom node directory")
    parser.add_argument(
        "--output", "-o", default="dist",
        help="Output root directory (default: dist/)",
    )
    parser.add_argument(
        "--name", "-n",
        help="Override the output package name (default: derived from node_dir name)",
    )
    parser.add_argument(
        "--skip-compile", action="store_true",
        help="Vendor dependencies but skip Nuitka compilation",
    )
    args = parser.parse_args()

    if not args.skip_compile:
        _TOOL_PYTHON = _ensure_tool_env()

    pack(
        node_dir=Path(args.node_dir),
        output_dir=Path(args.output),
        package_name=args.name,
        skip_compile=args.skip_compile,
    )


if __name__ == "__main__":
    main()
