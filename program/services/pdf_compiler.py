from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from services.app_paths import BASE_DIR as _PACKAGED_BASE_DIR

_DEV_BASE = Path(__file__).resolve().parents[2]
BASE_DIR = _PACKAGED_BASE_DIR if _PACKAGED_BASE_DIR else _DEV_BASE

_NOT_INSTALLED_MSG = (
    "Tectonic is not installed. Install it from "
    "https://tectonic-typesetting.github.io or place tectonic.exe next to "
    "JobSync, then try again."
)


def _find_tectonic() -> str | None:
    env_path = os.environ.get("TECTONIC_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    bundled = BASE_DIR / "tools" / "tectonic.exe"
    if bundled.exists():
        return str(bundled)
    return shutil.which("tectonic")


def compile_latex(tex_path: Path, timeout: int = 40) -> tuple[Path | None, str]:
    tectonic = _find_tectonic()
    if not tectonic:
        return None, _NOT_INSTALLED_MSG

    tex_path = Path(tex_path)
    pdf_path = tex_path.with_suffix(".pdf")
    try:
        result = subprocess.run(
            [tectonic, "--outdir", str(tex_path.parent), "-Z", "continue-on-errors", str(tex_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, _NOT_INSTALLED_MSG
    except subprocess.TimeoutExpired:
        return None, f"Tectonic timed out after {timeout}s compiling {tex_path.name}."
    except OSError as exc:
        return None, f"Could not run Tectonic: {exc}"

    if result.returncode == 0 and pdf_path.exists():
        return pdf_path, ""

    tail = (result.stderr or result.stdout or "").strip()
    tail = tail[-2000:] if tail else "Tectonic failed with no output."
    return None, tail
