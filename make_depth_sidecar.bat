@echo off
REM Create <facade>_depth_mapping.json for a stitched run that finished BEFORE the pipeline wrote it by itself
REM (needs the run's colmap_stage1\ and colmap_dense\ folders still on disk).
REM   make_depth_sidecar.bat <facade_output_dir> <images_folder> [facade_name]
if "%~2"=="" (
  echo usage: make_depth_sidecar.bat ^<facade_output_dir^> ^<images_folder^> [facade_name]
  exit /b 2
)
call "%~dp0scripts\_python.bat"
cd /d "%~dp0"
"%CC_PYTHON%" tools\make_depth_mapping_sidecar.py %*
exit /b %ERRORLEVEL%
