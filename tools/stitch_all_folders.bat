@echo off
if "%~1"=="" (
    echo Drag the parent folder ^(containing left/right/front/back/top subfolders^) onto this file, or run:
    echo   stitch_all_folders.bat "C:\path\to\MyBuilding"
    pause
    exit /b 1
)
cd /d "%~dp0.."
python tools\stitch_all_folders.py "%~1"
pause
