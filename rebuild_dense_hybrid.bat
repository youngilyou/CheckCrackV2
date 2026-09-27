@echo off
REM Rebuild the *_colmap_dense.* mosaic of a FINISHED run from its existing dense workspace (minutes, not the ~6 h of
REM dense stereo). Use after a change to the hybrid merge. Needs colmap_dense\dense\fused.ply in the run folder.
REM   rebuild_dense_hybrid.bat <facade_output_dir> [facade_name]
if "%~1"=="" (
  echo usage: rebuild_dense_hybrid.bat ^<facade_output_dir^> [facade_name]
  exit /b 2
)
call "%~dp0scripts\_python.bat"
cd /d "%~dp0"
"%CC_PYTHON%" tools\rebuild_dense_hybrid.py %*
exit /b %ERRORLEVEL%
