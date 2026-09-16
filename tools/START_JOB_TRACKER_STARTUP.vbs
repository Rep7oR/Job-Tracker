Option Explicit

Dim shell, fso, toolsDir, rootDir, jobsync
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

toolsDir = fso.GetParentFolderName(WScript.ScriptFullName)
rootDir = fso.GetParentFolderName(toolsDir)
jobsync = fso.BuildPath(rootDir, "START_JOB_TRACKER.bat")

If fso.FileExists(jobsync) Then
    shell.Run Chr(34) & jobsync & Chr(34), 0, False
End If

Set fso = Nothing
Set shell = Nothing
