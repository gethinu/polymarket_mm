@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"
set "RUNNER_PS1=%SCRIPT_DIR%run_event_driven_daily_report.ps1"
set "TASK_LOG=%REPO_ROOT%\logs\event_driven_task_action.log"

if not exist "%REPO_ROOT%\logs" mkdir "%REPO_ROOT%\logs" >nul 2>&1

echo [%date% %time%] wrapper_start args=%*>>"%TASK_LOG%"

"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%RUNNER_PS1%" %* >>"%TASK_LOG%" 2>&1
set "EC=%ERRORLEVEL%"

echo [%date% %time%] wrapper_exit code=%EC%>>"%TASK_LOG%"
exit /b %EC%
