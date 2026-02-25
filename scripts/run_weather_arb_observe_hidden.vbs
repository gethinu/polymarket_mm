' Launch weather observe runner with hidden window (no console pop-up).
Option Explicit
On Error Resume Next

Dim shell, cmd, i
Set shell = CreateObject("WScript.Shell")

cmd = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""C:\Repos\polymarket_mm\scripts\run_weather_arb_observe.ps1"""
For i = 0 To WScript.Arguments.Count - 1
  cmd = cmd & " " & WScript.Arguments(i)
Next

shell.Run cmd, 0, False
WScript.Quit Err.Number
