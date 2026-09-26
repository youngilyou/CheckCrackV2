@echo off
REM Is this computer ready to run CheckCrackV2? (ASCII only on purpose -- see scripts\setup_dev_machine.bat)
REM Python is taken from the CHECKCRACK_PYTHON environment variable if set, otherwise from PATH.
call "%~dp0scripts\_python.bat"
"%CC_PYTHON%" "%~dp0tools\check_environment.py"
exit /b %ERRORLEVEL%
