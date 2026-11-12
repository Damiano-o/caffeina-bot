' Avvia il watchdog in background, senza finestra (log in data\watchdog.log)
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
shell.Run "pythonw watchdog.py", 0, False
