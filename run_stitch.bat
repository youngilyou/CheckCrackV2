@echo off
REM Stitch one folder of photos into one facade (COLMAP + dense stereo per config\pipeline.yaml, pipeline.track).
REM   run_stitch.bat <images_folder> [facade_name] [--in-place] [--matcher-backend loftr|hloc] [--structure-type APARTMENT|DAM|FACTORY]
REM Result: <images_folder>\output\Vnnn\  (with --in-place)  or  facades\<facade_name>\output\
REM A full run of ~400 photos takes about 6 hours on an RTX 4080 (mostly dense stereo).
if "%~1"=="" (
  echo usage: run_stitch.bat ^<images_folder^> [facade_name] [--in-place] [options]
  exit /b 2
)
call "%~dp0scripts\_python.bat"
cd /d "%~dp0"
"%CC_PYTHON%" tools\stitch_folder.py %*
exit /b %ERRORLEVEL%
