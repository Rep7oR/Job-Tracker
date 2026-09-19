from __future__ import annotations

import os
import socket
import urllib.request
import webbrowser
import subprocess
import sys
import time
from pathlib import Path

def resolve_root() -> Path:
    """Resolve the real JobSync root in both source and PyInstaller OneDir mode."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent.parent

    exe_dir = Path(sys.executable).resolve().parent
    # OneDir launcher lives at <root>\tools\JobSync\JobSync.exe.
    candidates = [exe_dir.parent.parent, exe_dir.parent, exe_dir.parent.parent.parent]
    for candidate in candidates:
        if (candidate / "program" / "app.py").is_file():
            return candidate
    # Last-resort layout fallback.
    return exe_dir.parent.parent.parent


ROOT = resolve_root()
PROGRAM_DIR = ROOT / "program"
VENV_PY = ROOT / "runtime" / ".venv" / "Scripts" / "python.exe"
DATA_DIR = ROOT / "data"
LOG_FILE = DATA_DIR / "jobsync_launcher.log"
APP = PROGRAM_DIR / "app.py"


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def ensure_background_agents_running() -> None:
    """Launch the background job monitor + application status agent, if not already running.

    START_JOB_MONITOR.bat already contains its own "is it running?" check
    (via a CIM process query), so it's always safe to call here -- this just
    makes sure it actually gets called at all. Previously nothing in the
    startup chain (Windows sign-in task, desktop shortcut, or this launcher)
    ever invoked it, so the background agents only ever ran if a user
    manually clicked "Run now" in Settings while the app happened to be open.
    """
    monitor_bat = ROOT / "tools" / "START_JOB_MONITOR.bat"
    if not monitor_bat.is_file():
        log(f"Background agents not started: {monitor_bat} not found.")
        return
    try:
        subprocess.Popen(
            ["cmd.exe", "/c", str(monitor_bat)],
            cwd=str(ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("Ensured background agents (job monitor + application status agent) are running.")
    except Exception as exc:
        log(f"Could not start background agents: {exc}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for_app(port: int, timeout: float = 90.0) -> bool:
    """Wait until Streamlit is serving an actual HTTP document, not just a listening socket."""
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                body = response.read(8192).decode("utf-8", "ignore")
                if response.status == 200 and ("streamlit" in body.lower() or "root" in body.lower()):
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


SPLASH_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#12100c;color:#f6f1e8;font-family:Segoe UI,Arial,sans-serif}
body{display:flex;align-items:center;justify-content:center;position:relative}
.blob{position:absolute;border-radius:50%;filter:blur(70px);opacity:.32;animation:drift 9s ease-in-out infinite}
.blob-a{width:380px;height:380px;left:6%;top:10%;background:radial-gradient(circle,#e0a458,transparent 70%)}
.blob-b{width:340px;height:340px;right:8%;bottom:8%;background:radial-gradient(circle,#6fbf8b,transparent 70%);animation-delay:2.4s;animation-duration:11s}
@keyframes drift{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(28px,-22px) scale(1.1)}}
.wrap{position:relative;z-index:1;text-align:center;width:420px}
.mark{width:104px;height:104px;margin:0 auto 20px}
.mark svg{width:104px;height:104px;overflow:visible}
.orbit-outer{transform-origin:32px 32px;animation:spin 7s linear infinite}
.orbit-inner{transform-origin:32px 32px;animation:spinrev 4.6s linear infinite}
.core{transform-origin:32px 32px;animation:breathe 3.2s ease-in-out infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes spinrev{to{transform:rotate(-360deg)}}
@keyframes breathe{0%,100%{transform:scale(1);filter:drop-shadow(0 0 8px rgba(224,164,88,.45))}50%{transform:scale(1.07);filter:drop-shadow(0 0 16px rgba(111,191,139,.5))}}
h1{margin:0;font-size:30px;letter-spacing:.4px;font-weight:800}
.sub{margin-top:9px;color:#a89d8a;font-size:14px}
.bar{height:6px;background:rgba(255,255,255,.08);border-radius:10px;margin-top:30px;overflow:hidden}
.fill{height:100%;width:0;background:linear-gradient(90deg,#c97b3a,#e0a458,#7fd1a0);animation:load 5s linear forwards;box-shadow:0 0 14px rgba(224,164,88,.5)}
.status{margin-top:14px;color:#a89d8a;font-size:13px}
@keyframes load{to{width:100%}}
</style></head><body>
<div class="blob blob-a"></div><div class="blob blob-b"></div>
<div class="wrap">
  <div class="mark"><svg viewBox="0 0 64 64" aria-hidden="true">
    <defs>
      <radialGradient id="splashCore" cx="35%" cy="30%" r="75%">
        <stop offset="0%" stop-color="#fff6e8"/><stop offset="42%" stop-color="#e6ab63"/><stop offset="100%" stop-color="#6fbf8b"/>
      </radialGradient>
      <linearGradient id="splashRing" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#f0c383"/><stop offset="100%" stop-color="#7fd1a0"/>
      </linearGradient>
    </defs>
    <g class="orbit-outer">
      <ellipse cx="32" cy="32" rx="29" ry="12" fill="none" stroke="url(#splashRing)" stroke-width="1.4" opacity=".5"/>
      <circle cx="61" cy="32" r="2.3" fill="#fff"/>
    </g>
    <g class="orbit-inner">
      <circle cx="32" cy="32" r="14" fill="none" stroke="url(#splashRing)" stroke-width="1" opacity=".38"/>
      <circle cx="46" cy="32" r="1.7" fill="#fff"/>
    </g>
    <circle class="core" cx="32" cy="32" r="11.5" fill="url(#splashCore)"/>
  </svg></div>
  <h1>JobSync</h1>
  <div class="sub">Your next opportunity, in sync.</div>
  <div class="bar"><div class="fill"></div></div>
  <div class="status">Preparing your workspace…</div>
</div>
</body></html>"""

def main() -> int:
    # The installer grants Modify access to JobSync's writable trees. If an
    # older upgrade was applied without those ACLs, keep a clear diagnostic in
    # the launcher log rather than silently starting an app that cannot persist.
    for writable in (DATA_DIR, ROOT / "uploads", ROOT / "output", ROOT / "config", ROOT / "user_blueprints", ROOT / "ai"):
        try:
            writable.mkdir(parents=True, exist_ok=True)
            probe = writable / ".jobsync_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except PermissionError:
            log(f"WRITE ACCESS MISSING: {writable}. Re-run the JobSync installer as Administrator to repair local workspace permissions.")
            break
        except Exception as exc:
            log(f"Workspace probe failed for {writable}: {exc}")
    if not VENV_PY.exists():
        log(f"Virtual environment Python not found: {VENV_PY}")
        return 1
    if not APP.exists():
        log(f"Application not found: {APP}")
        return 1

    ensure_background_agents_running()

    port = free_port()
    env = os.environ.copy()
    env["JOBSYNC_ROOT"] = str(ROOT)
    env["JOBSYNC_DATA_DIR"] = str(ROOT)
    env["STREAMLIT_CONFIG_DIR"] = str(PROGRAM_DIR / ".streamlit")
    env["PYTHONUNBUFFERED"] = "1"
    # Keep the local Ollama model cache with the JobSync installation.
    env["OLLAMA_MODELS"] = str(ROOT / "ai" / "models")
    (ROOT / "ai" / "models").mkdir(parents=True, exist_ok=True)

    log(f"Starting Streamlit on 127.0.0.1:{port}")
    try:
        server = subprocess.Popen(
            [
                str(VENV_PY), "-m", "streamlit", "run", str(APP),
                "--server.headless", "true",
                "--server.address", "127.0.0.1",
                "--server.port", str(port),
                "--server.enableCORS", "false",
                "--server.enableXsrfProtection", "false",
                "--browser.gatherUsageStats", "false",
                "--server.enableCORS", "false",
                "--server.enableXsrfProtection", "false",
            ],
            cwd=str(PROGRAM_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log(f"Failed to start Streamlit: {exc}")
        return 1

    try:
        if not wait_for_app(port, 90):
            log("Streamlit did not become HTTP-ready within 90 seconds.")
            return 1

        import webview  # type: ignore

        # Keep a copy of Streamlit output so a packaged launch failure is diagnosable.
        def drain_output() -> None:
            try:
                if server.stdout is not None:
                    for line in server.stdout:
                        log("STREAMLIT: " + line.rstrip())
            except Exception as exc:
                log(f"Streamlit output reader stopped: {exc}")

        import threading
        threading.Thread(target=drain_output, daemon=True).start()

        url = f"http://127.0.0.1:{port}"
        log(f"Opening JobSync splash screen before app: {url}")
        # Always show the branded loading screen for five seconds. The actual
        # Streamlit page is loaded only after that minimum splash duration.
        window = webview.create_window(
            "JobSync",
            html=SPLASH_HTML,
            width=1400,
            height=900,
            min_size=(1000, 700),
            resizable=True,
            text_select=False,
            confirm_close=False,
        )

        def on_closed() -> None:
            log("JobSync window closed; stopping Streamlit.")
            try:
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
            except Exception as exc:
                log(f"Error stopping Streamlit: {exc}")

        window.events.closed += on_closed
        try:
            splash_started = time.monotonic()
            def show_app_after_splash() -> None:
                remaining = 5.0 - (time.monotonic() - splash_started)
                if remaining > 0:
                    time.sleep(remaining)
                # The server is already started in the background. Wait briefly
                # for HTTP readiness without blocking the splash UI thread.
                if not wait_for_app(port, 90):
                    log("Streamlit did not become ready after splash.")
                    return
                log("Splash complete; loading JobSync workspace.")
                try:
                    window.load_url(url)
                except Exception as exc:
                    log(f"Could not switch splash to app: {exc}")
            import threading
            threading.Thread(target=show_app_after_splash, daemon=True).start()
            webview.start(gui="edgechromium", debug=False)
            return 0
        except Exception as exc:
            # If WebView2/pywebview is unavailable, do not silently fail. Open the
            # already-running local app in the default browser as a safe fallback.
            log(f"Native WebView failed: {exc}; falling back to default browser.")
            webbrowser.open(url)
            return 0
    except Exception as exc:
        log(f"Desktop window failed: {exc}")
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
        return 1
    finally:
        try:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
