@echo off
REM Internal helper: sets CC_PYTHON to the python.exe every CheckCrackV2 .bat should use.
REM Order: CHECKCRACK_PYTHON env var -> a miniconda/anaconda in the usual places -> "python" on PATH.
REM (The viewer looks in the same places -- viewer\CheckCrackViewer\Services\PythonEnvironment.cs.)
set "CC_PYTHON="
if defined CHECKCRACK_PYTHON if exist "%CHECKCRACK_PYTHON%" set "CC_PYTHON=%CHECKCRACK_PYTHON%"
if not defined CC_PYTHON if exist "%USERPROFILE%\miniconda3\python.exe" set "CC_PYTHON=%USERPROFILE%\miniconda3\python.exe"
if not defined CC_PYTHON if exist "%USERPROFILE%\anaconda3\python.exe" set "CC_PYTHON=%USERPROFILE%\anaconda3\python.exe"
if not defined CC_PYTHON if exist "%LOCALAPPDATA%\miniconda3\python.exe" set "CC_PYTHON=%LOCALAPPDATA%\miniconda3\python.exe"
if not defined CC_PYTHON if exist "C:\ProgramData\miniconda3\python.exe" set "CC_PYTHON=C:\ProgramData\miniconda3\python.exe"
if not defined CC_PYTHON set "CC_PYTHON=python"
set "PYTHONUTF8=1"
