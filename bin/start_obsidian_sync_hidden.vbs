' 无窗口地启动 Obsidian Headless Sync（2026-09-14）。
' 计划任务原本直接跑 powershell -WindowStyle Hidden，但系统默认终端是 Windows Terminal 时
' "Hidden" 仍会留下一个可见的终端标签（用户："总会有一个终端被启动很碍眼"）。
' 经 WScript.Shell.Run(…, 0) 起就真正没有窗口 —— 与 ReaderPC 自启用的 start-readerpc.vbs 同一手法。
Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run "powershell.exe -ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File """ & here & "start_obsidian_sync.ps1""", 0, False
