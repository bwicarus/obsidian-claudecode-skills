' run_hidden.vbs - run a command line with no console window.
'
' Windows Task Scheduler runs a .cmd action interactively, so cmd.exe always
' flashes a console window on the desktop even when the task itself takes
' milliseconds. The user reported that flash on 2026-09-08 ("multiple times a
' terminal box popped up for an instant"). WScript.Shell.Run with windowStyle 0
' hides it; the same trick already keeps the ReaderPC watchdog silent.
'
' Usage from a scheduled task:
'   Program : wscript.exe
'   Argument: //B //Nologo "<this file>" "<script.cmd>" [args...]
'
' ASCII only: cscript/wscript decode this file in the OEM code page, so
' non-ASCII comments turn into mojibake.
Option Explicit

Dim shell, line, i, code

If WScript.Arguments.Count = 0 Then
  WScript.Quit 2
End If

Set shell = CreateObject("WScript.Shell")

' Route through cmd.exe explicitly so windowStyle 0 applies to the console host
' itself; letting ShellExecute pick the .cmd handler is less predictable.
line = "cmd.exe /c """ & WScript.Arguments(0) & """"
For i = 1 To WScript.Arguments.Count - 1
  line = line & " """ & WScript.Arguments(i) & """"
Next

' Wait for the child so the task's LastTaskResult stays meaningful and
' MultipleInstances=IgnoreNew can actually suppress overlapping runs.
code = shell.Run(line, 0, True)
WScript.Quit code
