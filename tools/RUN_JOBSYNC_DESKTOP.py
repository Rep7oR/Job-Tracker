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
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#071126;color:#eef3ff;font-family:Segoe UI,Arial,sans-serif}
body{display:flex;align-items:center;justify-content:center}
.wrap{text-align:center;width:420px}.mark{width:92px;height:92px;margin:0 auto 22px;border-radius:26px;background:linear-gradient(135deg,#18d8ff,#5968ff 55%,#b24cff);box-shadow:0 0 45px rgba(75,120,255,.42);display:flex;align-items:center;justify-content:center;font-size:48px;font-weight:800;animation:pulse 1.6s ease-in-out infinite}
h1{margin:0;font-size:30px;letter-spacing:.4px}.sub{margin-top:9px;color:#91a1c3;font-size:14px}.bar{height:6px;background:#152340;border-radius:10px;margin-top:30px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#18d8ff,#8b5cf6);animation:load 5s linear forwards}.status{margin-top:14px;color:#aab8d4;font-size:13px}@keyframes load{to{width:100%}}@keyframes pulse{50%{transform:scale(1.05);box-shadow:0 0 65px rgba(75,120,255,.58)}}
</style></head><body><div class="wrap"><div class="mark">J</div><h1>JobSync</h1><div class="sub">Your next opportunity, in sync.</div><div class="bar"><div class="fill"></div></div><div class="status">Preparing your workspace…</div></div></body></html>"""

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
