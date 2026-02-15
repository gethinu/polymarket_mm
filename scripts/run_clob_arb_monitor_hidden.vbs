' Launch monitor runner with hidden window (no console pop-up).
Option Explicit

Dim shell, cmd
Set shell = CreateObject("WScript.Shell")

cmd = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""C:\Repos\polymarket_mm\scripts\run_clob_arb_monitor.ps1"""
shell.Run cmd, 0, False
