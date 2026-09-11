from __future__ import annotations

import html
import subprocess
import sys
import tkinter as tk


def _tk_popup(title: str, message: str, seconds: int = 8) -> bool:
    """Reliable bottom-right Windows popup using Python's standard Tk UI."""
    try:
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#111418")

        width, height = 380, 145
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = sw - width - 24
        y = sh - height - 54
        root.geometry(f"{width}x{height}+{x}+{y}")

        frame = tk.Frame(root, bg="#111418", highlightthickness=1, highlightbackground="#303640")
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="🔔  " + str(title), bg="#111418", fg="#f5f7fa",
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(frame, text=str(message), bg="#111418", fg="#c8ced6",
                 font=("Segoe UI", 9), anchor="nw", justify="left", wraplength=345).pack(
                     fill="both", expand=True, padx=16, pady=(0, 12)
                 )
        root.after(seconds * 1000, root.destroy)
        root.mainloop()
        return True
    except Exception:
        return False


def _powershell_toast(title: str, message: str) -> bool:
    if sys.platform != "win32":
        return False
    title = html.escape(str(title)).replace("'", "''")
    message = html.escape(str(message)).replace("'", "''")
    ps = f"""
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$template = @'
<toast duration="short"><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>
'@
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JobSync').Show($toast)
"""
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return completed.returncode == 0
    except Exception:
        return False


def desktop_notify(title: str, message: str) -> bool:
    """Display a notification in the user's local Windows session."""
    if sys.platform == "win32" and _powershell_toast(title, message):
        return True
    return _tk_popup(title, message)
