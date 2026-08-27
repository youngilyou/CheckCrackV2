@echo off
REM Double-click entry point that downloads/installs CheckCrackDdsBridge's native FastDDS SDK
REM dependency under tools\, so a fresh checkout on another machine can reproduce it without
REM repeating the manual process:
REM   ExtraModule + FastDDSGen -> tools\Module\FastDDSGen\FastDDS (Get-FastDdsGenModule.ps1) --
REM   downloads youngilyou/Gen_IDL_DDS's ExtraModule (packaged FastDDS SDK + fastddsgen Java
REM   runtime) via `gh api`, reassembles the split zip parts, and installs it. See that script's
REM   own header comment for why this is a per-project independent copy (never a shared
REM   system-wide install like C:\eProsima -- see this repo's README "독립성 원칙").
REM
REM Only one step here (unlike FacadePreviewer/tools\Setup-Tools.bat's 3 steps) -- this project
REM has no FFmpeg/rsync dependency, just the FastDDS SDK.
REM
REM Requires: gh CLI, already authenticated (`gh auth status`). Safe to re-run -- the script
REM skips files that are already downloaded.
REM
REM Usage: just double-click, or from a shell: tools\Setup-Tools.bat

setlocal

echo ============================================================
echo ExtraModule + FastDDSGen
echo ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Get-FastDdsGenModule.ps1"
if errorlevel 1 (
    echo.
    echo Get-FastDdsGenModule.ps1 failed -- see above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo OK -- FastDDS SDK installed.
echo ============================================================
pause
