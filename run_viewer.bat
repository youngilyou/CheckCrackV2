@echo off
REM Build (if needed) and start the CheckCrackViewer. Needs the .NET 9 SDK.
REM The viewer finds the project root by walking up to CLAUDE.local.md, and finds python via
REM CHECKCRACK_PYTHON / miniconda / PATH (same order as scripts\_python.bat).
call "%~dp0scripts\_python.bat"
where dotnet >nul 2>nul
if errorlevel 1 (
  echo [error] dotnet not found. Install the .NET 9 SDK: https://dotnet.microsoft.com/download
  exit /b 1
)
cd /d "%~dp0viewer"
dotnet build CheckCrackViewer\CheckCrackViewer.csproj -c Release -v minimal
if errorlevel 1 (
  echo [error] build failed. If the viewer is already running, close it first.
  exit /b 1
)
set "CHECKCRACK_PYTHON=%CC_PYTHON%"
start "" "%~dp0viewer\CheckCrackViewer\bin\Release\net9.0-windows\CheckCrackViewer.exe"
