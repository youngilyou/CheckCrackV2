@echo off
REM Delete Dense Stereo intermediates nothing reads any more (normal_maps, photometric depth maps,
REM fused.ply.vis, undistorted images) from runs that finished before the pipeline did it itself.
REM Dry run by default -- add --apply to actually delete.
REM   cleanup_dense.bat <folder> [<folder> ...] [--apply]
if "%~1"=="" (
  echo usage: cleanup_dense.bat ^<folder^> [^<folder^> ...] [--apply]
  exit /b 2
)
call "%~dp0scripts\_python.bat"
cd /d "%~dp0"
"%CC_PYTHON%" tools\cleanup_dense_intermediates.py %*
exit /b %ERRORLEVEL%
