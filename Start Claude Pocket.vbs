' Starts Claude Pocket from this folder (no console window) if it is not running yet, then opens its
' window. With any argument (the Startup shortcut passes one) it starts with the tray icon only.
' To stop the office, right-click its icon in the notification area and choose Quit.
Set sh = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

Function Ask(method, path)
  ' True when the office answered
  On Error Resume Next
  Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  http.setTimeouts 1000, 1000, 2000, 2000
  http.open method, "http://127.0.0.1:4545" & path, False
  http.setRequestHeader "Content-Type", "application/json"
  http.send "{}"
  Ask = (Err.Number = 0)
  If Ask Then Ask = (http.status = 200)
End Function

If Not Ask("GET", "/config") Then
  sh.Run "pythonw """ & here & "\server.py""", 0, False
  For i = 1 To 40
    WScript.Sleep 250
    If Ask("GET", "/config") Then Exit For
  Next
End If
If WScript.Arguments.Count = 0 Then Ask "POST", "/show"
