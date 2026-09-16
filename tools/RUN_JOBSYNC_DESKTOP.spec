# JobSync native desktop launcher - intentionally checked in so the installer
# can invoke PyInstaller without running the makespec phase. This avoids the
# PyInstaller 6.x WinError 3 path='' failure seen in elevated installer runs.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

BASE = Path(SPECPATH).resolve()
SCRIPT = BASE / "RUN_JOBSYNC_DESKTOP.py"
ICON = BASE / "JobSync.ico"

datas, binaries, hiddenimports = collect_all("webview")

a = Analysis(
    [str(SCRIPT)],
    pathex=[str(BASE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="JobSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    a.zipfiles,
    a.zipped_data,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JobSync",
)
