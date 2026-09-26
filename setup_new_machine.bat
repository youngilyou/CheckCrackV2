@echo off
REM One-time setup on a NEW computer. Run the steps in order (see README section 0):
REM   1) this file    : Python packages + .NET restore  (scripts\setup_dev_machine.ps1)
REM   2) CUDA PyTorch : pip install torch --index-url https://download.pytorch.org/whl/cu126
REM   3) CUDA pycolmap: scripts\install_colmap_cuda_dense.bat   (only if check_env.bat says pycolmap has no dense stereo)
REM   4) copy the crack model weights (*.pt are not in git): training\crack_seg\models\
REM   5) check_env.bat
call "%~dp0scripts\setup_dev_machine.bat"
echo.
echo Next: install CUDA PyTorch, then run check_env.bat  (README section 0)
