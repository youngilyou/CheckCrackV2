@echo off
REM Detect cracks on an already-stitched facade (reads photos + depth maps, writes <facade>_cracks*.json).
REM   run_detect.bat <facade_output_dir> [facade_name] [--skip-v2] [--device cuda]
REM Needs <facade>_depth_mapping.json in the output dir for depth-based crack placement
REM (written automatically by new runs; for older runs use make_depth_sidecar.bat).
if "%~1"=="" (
  echo usage: run_detect.bat ^<facade_output_dir^> [facade_name] [options]
  exit /b 2
)
call "%~dp0scripts\_python.bat"
cd /d "%~dp0"
"%CC_PYTHON%" tools\detect_cracks_folder.py %*
exit /b %ERRORLEVEL%
