"""Quick "is this machine ready?" check for CheckCrackV2 -- run it first on a new computer
(`check_env.bat` calls it). Prints one line per requirement: OK / WARN / FAIL, plus what to do.

Exit code 0 = everything the pipeline needs is present (WARNs are optional features), 1 = at least one FAIL.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = 0


def report(level: str, name: str, detail: str = "") -> None:
    global failures
    if level == "FAIL":
        failures += 1
    print(f"[{level:4s}] {name}" + (f" -- {detail}" if detail else ""))


def check_import(module: str, hint: str, required: bool = True) -> object | None:
    try:
        return importlib.import_module(module)
    except Exception as exc:  # noqa: BLE001 - any import failure is what we are reporting
        report("FAIL" if required else "WARN", module, f"import failed ({type(exc).__name__}: {exc}). {hint}")
        return None


def main() -> None:
    print(f"python {sys.version.split()[0]}  ({sys.executable})")

    torch = check_import("torch", "CUDA build: pip install torch --index-url https://download.pytorch.org/whl/cu126")
    if torch is not None:
        if torch.cuda.is_available():
            report("OK", "torch + CUDA", f"{torch.__version__}, GPU: {torch.cuda.get_device_name(0)}")
        else:
            report("WARN", "torch CUDA", f"{torch.__version__} but no CUDA GPU visible -- LoFTR/YOLO will run on CPU (very slow)")

    for module, hint in (
        ("cv2", "pip install opencv-python"),
        ("numpy", "pip install numpy"),
        ("scipy", "pip install scipy  (needed by depth mapping / dense stereo / graph refinement)"),
        ("kornia", "pip install kornia"),
        ("ultralytics", "pip install ultralytics"),
        ("shapely", "pip install shapely"),
        ("networkx", "pip install networkx"),
        ("yaml", "pip install PyYAML"),
        ("pyproj", "pip install pyproj"),
    ):
        mod = check_import(module, hint)
        if mod is not None:
            report("OK", module, getattr(mod, "__version__", ""))

    pycolmap = check_import("pycolmap", "sparse reconstruction needs pycolmap")
    if pycolmap is not None:
        version = getattr(pycolmap, "__version__", "?")
        has_cuda = bool(getattr(pycolmap, "has_cuda", False))
        has_dense = hasattr(pycolmap, "patch_match_stereo") and hasattr(pycolmap, "stereo_fusion")
        if has_cuda and has_dense:
            report("OK", "pycolmap (CUDA + dense stereo)", version)
        else:
            report("FAIL", "pycolmap dense stereo",
                   f"{version}: has_cuda={has_cuda}, patch_match_stereo/stereo_fusion={has_dense}. The pip wheel has no CUDA "
                   "dense stereo -- build the CUDA pycolmap with scripts\\install_colmap_cuda_dense.bat "
                   "(needed for pipeline.track: reference and for depth-based crack placement)")

    # Files that are NOT in git (see .gitignore) and must be copied from the original machine.
    try:
        from src.common.config import load_config

        cfg = load_config(str(ROOT / "config" / "pipeline.yaml"))
        for key in ("model", "model_v2"):
            if key in cfg.crack:
                p = ROOT / str(cfg.crack[key])
                report("OK" if p.exists() else "FAIL", f"crack model ({key})",
                       str(p) if p.exists() else f"{p} missing -- *.pt files are gitignored; copy training/crack_seg/models/ "
                       "from the original machine")
        track = str(cfg.pipeline.track) if "pipeline" in cfg and "track" in cfg.pipeline else "full"
        print(f"       pipeline.track = {track}")
    except Exception as exc:  # noqa: BLE001
        report("FAIL", "config/pipeline.yaml", f"{type(exc).__name__}: {exc}")

    # LoFTR pretrained weights are downloaded by kornia on first use (needs internet once).
    report("OK" if (Path.home() / ".cache" / "torch" / "hub" / "checkpoints").exists() else "WARN",
           "torch hub cache", "LoFTR weights are downloaded on first run (internet needed once)")

    # Optional: hloc / SuperPoint matcher lives in its own python env.
    try:
        from src.matching.hloc_bridge import DEFAULT_HLOC_ENV_PYTHON

        p = Path(DEFAULT_HLOC_ENV_PYTHON)
        report("OK" if p.exists() else "WARN", "hloc env (optional, matcher_backend: hloc)",
               str(p) if p.exists() else f"{p} not found -- only needed if you pick hloc; set colmap.hloc_env_python in config")
    except Exception:  # noqa: BLE001
        pass

    print()
    print("RESULT:", "READY" if failures == 0 else f"{failures} problem(s) -- fix the FAIL lines above")
    sys.exit(0 if failures == 0 else 1)


if __name__ == "__main__":
    main()
