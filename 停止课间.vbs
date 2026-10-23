Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
folder = fs.GetParentFolderName(WScript.ScriptFullName)
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & folder & "\stop.ps1"""
result = shell.Run(command, 0, True)
If result <> 0 Then
  MsgBox "Could not stop the classroom server. Please run stop.ps1 for details.", 48, "Classroom Assistant"
End If
