@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0utils\diagnose.ps1" %*
set "EMBER_DIAG_STATUS=%errorlevel%"
echo.
if not defined EMBER_DIAGNOSTICS_NO_PAUSE pause
exit /b %EMBER_DIAG_STATUS%
