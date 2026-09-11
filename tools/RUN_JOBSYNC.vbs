Option Explicit

Dim shell, fso, base, ps
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)
ps = fso.BuildPath(base, "RUN_JOBSYNC.ps1")

shell.Run "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & ps & Chr(34), 0, False

Set fso = Nothing
Set shell = Nothing
