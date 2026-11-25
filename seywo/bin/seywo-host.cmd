@echo off
setlocal DisableDelayedExpansion
if "%~1"=="--hook" if "%SEYWO_INTERNAL_MODEL%"=="1" exit /b 0
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "%~dp0seywo-host.ps1" %*
exit /b %errorlevel%
