"""Centralized path resolution for JobSync."""
from __future__ import annotations
import os
import sys
from pathlib import Path

_DATA_DIR_FROM_LAUNCHER = os.getenv("JOBSYNC_DATA_DIR", "").strip()
FROZEN = getattr(sys, "frozen", False)
if _DATA_DIR_FROM_LAUNCHER:
    BASE_DIR = Path(_DATA_DIR_FROM_LAUNCHER).expanduser().resolve()
elif FROZEN:
    BASE_DIR = Path(sys.executable).parent.resolve()
else:
    BASE_DIR = None

def is_packaged() -> bool:
    return FROZEN or bool(_DATA_DIR_FROM_LAUNCHER)

def data_dir() -> Path | None:
    return BASE_DIR
