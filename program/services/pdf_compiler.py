"""Compiles generated LaTeX source to PDF using Tectonic.

Tectonic (https://tectonic-typesetting.github.io/) is a self-contained LaTeX
engine with no system TeX install required - the same tool the pre-rebuild
app used. This module locates it on PATH, and if it's genuinely missing
(checked with ``shutil.which`` - true in this sandbox, which has no root/
network access to install it), returns a clean, honest error rather than
pretending a compile happened.

Output is stored under the account's data directory, following
``paths.DATA_DIR`` conventions: ``data/generated/<email>/<queue_entry_id>/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from services.paths import DATA_DIR

TECTONIC_BINARY = "tectonic"
_COMPILE_TIMEOUT_SECONDS = 90

GENERATED_DIR: Path = DATA_DIR / "generated"


def account_output_dir(email: str, entry_id: str) -> Path:
    """Directory an account's generated documents for one queue entry live in."""
    safe_email = email.strip().lower().replace("/", "_")
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in entry_id)
    out_dir = GENERATED_DIR / safe_email / safe_id
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def compile_tex(tex_source: str, out_dir: Path, basename: str) -> tuple[Path | None, str | None]:
    """Compile one .tex document to PDF with Tectonic.

    Writes ``<basename>.tex`` into ``out_dir``, invokes Tectonic against it,
    and returns (pdf_path, None) on success or (None, error_message) on any
    failure - including Tectonic simply not being installed, which is the
    expected outcome in this sandbox.
    """
    binary = shutil.which(TECTONIC_BINARY)
    if binary is None:
        return None, (
            "Tectonic is not installed on this system (not found on PATH). "
            "Install it from https://tectonic-typesetting.github.io/ to "
            "enable PDF compilation."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{basename}.tex"
    tex_path.write_text(tex_source, encoding="utf-8")

    try:
        result = subprocess.run(
            [binary, "-X", "compile", str(tex_path), "--outdir", str(out_dir)],
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, f"Tectonic timed out after {_COMPILE_TIMEOUT_SECONDS}s compiling {basename}.tex"
    except OSError as exc:
        return None, f"Failed to launch Tectonic: {exc}"

    pdf_path = out_dir / f"{basename}.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        stderr_tail = (result.stderr or result.stdout or "").strip()[-2000:]
        return None, f"Tectonic compile failed (exit {result.returncode}): {stderr_tail}"

    return pdf_path, None


def compile_application(
    email: str, entry_id: str, cv_tex: str, cover_letter_tex: str
) -> dict:
    """Compile both the CV and cover letter for one queue entry.

    Returns a dict with keys "cv_pdf", "cover_letter_pdf" (Path | None) and
    "errors" (list[str]) - both documents are attempted independently so a
    failure in one doesn't block the other.
    """
    out_dir = account_output_dir(email, entry_id)
    errors: list[str] = []

    cv_pdf, cv_error = compile_tex(cv_tex, out_dir, "cv")
    if cv_error:
        errors.append(f"CV: {cv_error}")

    cover_pdf, cover_error = compile_tex(cover_letter_tex, out_dir, "cover_letter")
    if cover_error:
        errors.append(f"Cover letter: {cover_error}")

    return {
        "cv_pdf": cv_pdf,
        "cover_letter_pdf": cover_pdf,
        "errors": errors,
        "out_dir": out_dir,
    }
