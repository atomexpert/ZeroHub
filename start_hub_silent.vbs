Option Explicit

' Antigravity Hub — silent background start (no console windows).
' Runs start_hub_hidden.pyw under pythonw.exe; it launches .venv\Scripts\python.exe run_server.py
' with CREATE_NO_WINDOW (invisible headless console inherited by agy.exe & co) and opens the browser.
' The port check is done inside start_hub_hidden.pyw (socket), so no cmd/netstat console is spawned here.

Dim fso, shell, baseDir, pythonwPath, launcherPath
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwPath = baseDir & "\.venv\Scripts\pythonw.exe"
launcherPath = baseDir & "\start_hub_hidden.pyw"

shell.CurrentDirectory = baseDir
shell.Run """" & pythonwPath & """ """ & launcherPath & """ --open", 0, False
