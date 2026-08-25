@echo off
if "%~1"=="" (
    echo Drag a folder of drone photos onto this file, or run:
    echo   stitch_folder.bat "C:\path\to\photos"
    pause
    exit /b 1
)
cd /d "%~dp0.."
python tools\stitch_folder.py "%~1"
pause
