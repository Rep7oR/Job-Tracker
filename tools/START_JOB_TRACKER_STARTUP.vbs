Option Explicit

Dim shell, fso, toolsDir, rootDir, tracker
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

toolsDir = fso.GetParentFolderName(WScript.ScriptFullName)
rootDir = fso.GetParentFolderName(toolsDir)
tracker = fso.BuildPath(rootDir, "START_JOB_TRACKER.bat")

If fso.FileExists(tracker) Then
    shell.Run Chr(34) & tracker & Chr(34), 0, False
End If

Set fso = Nothing
Set shell = Nothing
