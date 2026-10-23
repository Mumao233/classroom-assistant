Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & folder & "\start.ps1"""
result = shell.Run(command, 0, True)
If result <> 0 Then
  MsgBox "Startup failed. Please open start.ps1 or read data/server-error.log.", 48, "Classroom Assistant"
End If
