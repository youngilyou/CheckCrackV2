@echo off
REM CheckCrack dev machine setup launcher.
REM Actual logic + Korean text lives in setup_dev_machine.ps1 -- classic cmd.exe
REM batch parsing is unreliable with UTF-8/Korean text (nested parens, multi-byte
REM boundaries), so this .bat is kept ASCII-only and just hands off to PowerShell,
REM which handles the UTF-8-with-BOM .ps1 file correctly regardless of the
REM console's active codepage.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_dev_machine.ps1"
