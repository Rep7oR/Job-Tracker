"""Filesystem path resolution shared by JobSync services.

Minimal replacement for the deleted ``app_paths.py``. Resolves a single,
writable base directory that works both when running from source (``python
-m streamlit run program/app.py``) and when running from a packaged/frozen
build, and exposes a ``data/`` subdirectory beneath it for all local
application state (accounts, profiles, and anything later phases add).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _resolve_base_dir() -> Path:
    # The packaged desktop launcher (tools/RUN_JOBSYNC_DESKTOP.py) points the
    # app at its installation root via this environment variable, since the
    # Streamlit process itself runs under a plain venv interpreter rather
    # than a frozen one. Honor it first when present.
    env_root = os.environ.get("JOBSYNC_DATA_DIR") or os.environ.get("JOBSYNC_ROOT")
    if env_root:
        return Path(env_root).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    # program/services/paths.py -> parents[0]=services, [1]=program, [2]=repo root.
    return Path(__file__).resolve().parents[2]


BASE_DIR: Path = _resolve_base_dir()
DATA_DIR: Path = BASE_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)
