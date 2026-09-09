@echo off
REM ============================================================================
REM  install_colmap_cuda_dense.bat
REM
REM  One-stop installer/builder for a CUDA-enabled COLMAP + pycolmap on a
REM  fresh Windows machine, reproducing exactly the working build produced in
REM  this session (COLMAP 4.3.0.dev0 with CUDA patch_match_stereo/
REM  stereo_fusion, and matching pycolmap 4.3.0.dev0 python bindings).
REM
REM  WHAT THIS SCRIPT DOES AUTOMATICALLY:
REM    1. Verifies prerequisites (git, cmake, ninja, python, CUDA, MSVC).
REM    2. Clones + bootstraps vcpkg.
REM    3. Clones COLMAP from GitHub.
REM    4. Configures + builds + installs COLMAP with CUDA (Ninja, x64-windows-static).
REM    5. Applies the one required source patch (PoseLib MSVC template-overload
REM       fix -- see colmap_cuda_patches\jacobian_accumulator.patched.h).
REM    6. Configures + builds + installs the matching pycolmap python bindings.
REM    7. Verifies both colmap.exe and pycolmap report CUDA support.
REM
REM  WHAT THIS SCRIPT DOES NOT INSTALL (must already be present -- these are
REM  normal GUI/EXE installers, not something safe to silently automate):
REM    - Visual Studio 2022 (any edition) with the
REM      "Desktop development with C++" workload.
REM      https://visualstudio.microsoft.com/downloads/
REM    - NVIDIA GPU driver + CUDA Toolkit 12.x (any recent 12.x is fine).
REM      https://developer.nvidia.com/cuda-downloads
REM    - Git for Windows.          https://git-scm.com/download/win
REM    - CMake (3.24+).            https://cmake.org/download/  (or: winget install Kitware.CMake)
REM    - Ninja build.              https://github.com/ninja-build/ninja/releases (or: winget install Ninja-build.Ninja)
REM    - Python 3.10+ (Miniconda/Anaconda is fine). https://www.anaconda.com/download
REM  The script checks for every one of these and tells you exactly what is
REM  missing before doing anything else -- it will not run halfway and fail.
REM
REM  USAGE:
REM    1. Edit the "USER-CONFIGURABLE SETTINGS" block below if needed
REM       (default install path is C:\temp per spec).
REM    2. Double-click this .bat (or run from a plain cmd.exe -- it does NOT
REM       need to be run from a "Developer Command Prompt", it sets up the
REM       MSVC environment itself).
REM    3. It is safe to re-run: each stage is skipped if its output already
REM       exists, so a failed/interrupted run can just be re-launched.
REM
REM  Total build time on the original dev machine: roughly 1.5-2 hours
REM  (dominated by vcpkg building Boost/Ceres/CGAL/etc. from source the first
REM  time; COLMAP itself builds in a few minutes, pycolmap in under a minute).
REM ============================================================================

setlocal enabledelayedexpansion

REM ---------------------------- USER-CONFIGURABLE SETTINGS --------------------
set "INSTALL_ROOT=C:\temp"
REM Exact COLMAP / vcpkg commits this script was verified against (built and
REM tested successfully end to end on 2026-09-09). Pinned deliberately: a
REM plain `git clone` of each project's default branch would instead grab
REM whatever upstream happens to be on the day this script actually runs,
REM which can silently break in entirely new ways that have nothing to do
REM with the target machine (renamed CMake options, changed vcpkg feature
REM names, a new required dependency, etc.) -- exactly the kind of
REM "works today, fails on another machine/another day" gap this script
REM exists to close. Only change these if you deliberately want to track
REM newer upstream code (and are prepared to debug it).
set "COLMAP_COMMIT=a0e0e56fbc179f475341014e5745b85948f5c7a9"
set "VCPKG_COMMIT=5cd4931b18d49c77074703cf293a7837ac7506fa"
REM Python executable used for the pycolmap "pip install -ve ." step and for
REM the final verification. Change this if you want to target a specific
REM conda env, e.g. "C:\Users\me\miniconda3\envs\myenv\python.exe".
set "PYTHON_EXE=python"
REM CUDA architecture(s) to build for. "native" (CMake 3.24+) auto-detects the
REM GPU(s) physically installed in THIS machine at configure time, which is
REM exactly right when you build directly on the target machine (this script's
REM use case). If "native" ever fails on an unusual multi-GPU box, set this to
REM your GPU's compute capability instead, e.g.:
REM   89 = RTX 40-series (Ada)   86 = RTX 30-series (Ampere)
REM   75 = RTX 20-series/GTX 16xx (Turing)   90 = H100/GH200 (Hopper)
set "CUDA_ARCH=native"
REM ------------------------------------------------------------------------------

set "PATCH_DIR=%~dp0colmap_cuda_patches"
set "VCPKG_DIR=%INSTALL_ROOT%\vcpkg"
set "COLMAP_DIR=%INSTALL_ROOT%\colmap"
set "COLMAP_BUILD_DIR=%COLMAP_DIR%\build"
set "COLMAP_INSTALL_DIR=%COLMAP_DIR%\install"
set "INSTALL_ROOT_FWD=%INSTALL_ROOT:\=/%"
set "VCPKG_DIR_FWD=%VCPKG_DIR:\=/%"
set "LOG_DIR=%INSTALL_ROOT%\_install_logs"

echo ============================================================
echo  COLMAP + pycolmap CUDA dense-reconstruction installer
echo  Install root: %INSTALL_ROOT%
echo ============================================================
echo.

if not exist "%INSTALL_ROOT%" (
    mkdir "%INSTALL_ROOT%"
    if errorlevel 1 goto :error
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM ============================================================
REM STAGE 0: prerequisite checks
REM ============================================================
echo [0/8] Checking prerequisites...

where git >nul 2>nul
if errorlevel 1 (
    echo [ERROR] git.exe not found in PATH.
    echo         Install Git for Windows: https://git-scm.com/download/win
    goto :error
)

where cmake >nul 2>nul
if errorlevel 1 (
    echo [ERROR] cmake.exe not found in PATH.
    echo         Install CMake ^(3.24+^): https://cmake.org/download/
    echo         or run:  winget install Kitware.CMake
    goto :error
)
REM CMAKE_CUDA_ARCHITECTURES=native (this script's default CUDA_ARCH) needs
REM CMake 3.24+. Checking this explicitly up front, instead of letting it
REM fail deep inside a 30-60 minute configure log, is the difference between
REM a one-line actionable error and a confusing failure someone has to dig
REM for.
for /f "tokens=3" %%v in ('cmake --version ^| findstr /r "^cmake version"') do set "CMAKE_VER=%%v"
for /f "tokens=1,2 delims=." %%a in ("%CMAKE_VER%") do (
    set "CMAKE_VER_MAJOR=%%a"
    set "CMAKE_VER_MINOR=%%b"
)
set /a "CMAKE_VER_NUM=CMAKE_VER_MAJOR*100+CMAKE_VER_MINOR" 2>nul
if not defined CMAKE_VER_NUM (
    echo [WARN] Could not parse cmake --version output ^("%CMAKE_VER%"^) -- continuing anyway.
) else if %CMAKE_VER_NUM% LSS 324 (
    if /I "%CUDA_ARCH%"=="native" (
        echo [ERROR] cmake %CMAKE_VER% found, but CUDA_ARCH=native requires CMake 3.24+.
        echo         Either upgrade CMake ^(https://cmake.org/download/, or
        echo         winget install Kitware.CMake^), or edit CUDA_ARCH at the top
        echo         of this script to your GPU's explicit compute capability
        echo         ^(e.g. 89 for RTX 40-series, 86 for RTX 30-series^).
        goto :error
    )
)

where ninja >nul 2>nul
if errorlevel 1 (
    echo [ERROR] ninja.exe not found in PATH.
    echo         Install Ninja: https://github.com/ninja-build/ninja/releases
    echo         or run:  winget install Ninja-build.Ninja
    goto :error
)

where %PYTHON_EXE% >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python executable "%PYTHON_EXE%" not found in PATH.
    echo         Install Python 3.10+ ^(Miniconda is fine^): https://www.anaconda.com/download
    echo         or edit the PYTHON_EXE setting at the top of this script.
    goto :error
)

where nvcc >nul 2>nul
if errorlevel 1 (
    echo [ERROR] nvcc.exe ^(CUDA Toolkit^) not found in PATH.
    echo         Install a CUDA Toolkit 12.x matching your NVIDIA driver:
    echo         https://developer.nvidia.com/cuda-downloads
    goto :error
)

where nvidia-smi >nul 2>nul
if errorlevel 1 (
    echo [WARN] nvidia-smi not found. No NVIDIA GPU / driver detected.
    echo        The build will proceed, but CUDA_ARCH=native detection may fail
    echo        and colmap will not actually be able to run dense reconstruction.
) else (
    echo [INFO] nvidia-smi found -- GPU:
    nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>nul
)

REM vcpkg building Boost/Ceres/CGAL/etc. from source, plus COLMAP's own
REM build tree, realistically needs 30-40GB+ of free space -- exactly the
REM kind of thing that fails with a confusing low-level compiler/linker
REM error hours into a run instead of a clear message, if it runs out
REM mid-build. Check up front instead.
for %%d in ("%INSTALL_ROOT%") do set "INSTALL_DRIVE=%%~dd"
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "(Get-PSDrive -Name '%INSTALL_DRIVE:~0,1%').Free"`) do set "FREE_BYTES=%%s"
if defined FREE_BYTES (
    set /a "FREE_GB=FREE_BYTES/1073741824" 2>nul
    if defined FREE_GB (
        echo [INFO] Free space on %INSTALL_DRIVE%: ~%FREE_GB% GB
        if %FREE_GB% LSS 40 (
            echo [WARN] Less than 40GB free on %INSTALL_DRIVE% -- vcpkg building
            echo        Boost/Ceres/CGAL/etc. from source plus the COLMAP build
            echo        tree can need more than this. Continuing anyway, but a
            echo        failure partway through may simply be disk space.
        )
    )
)

echo [INFO] Tip: if this machine has antivirus real-time scanning enabled, adding
echo       %INSTALL_ROOT% to its exclusion list will noticeably speed up the
echo       build (large from-source C++ builds create/scan huge numbers of
echo       temporary object files) and avoid rare file-lock/access-denied
echo       errors on freshly written build output. This script does not
echo       change antivirus settings itself.
echo.

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo [ERROR] Visual Studio Installer ^(vswhere.exe^) not found.
    echo         Install Visual Studio 2022 with the "Desktop development with
    echo         C++" workload: https://visualstudio.microsoft.com/downloads/
    goto :error
)

for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS_PATH=%%i"
if not defined VS_PATH (
    echo [ERROR] No Visual Studio 2022 installation with the C++ ^(VC.Tools.x86.x64^)
    echo         component was found. In the Visual Studio Installer, add the
    echo         "Desktop development with C++" workload and re-run this script.
    goto :error
)
set "VCVARSALL=%VS_PATH%\VC\Auxiliary\Build\vcvarsall.bat"
if not exist "%VCVARSALL%" (
    echo [ERROR] vcvarsall.bat not found at "%VCVARSALL%".
    goto :error
)

REM Pick the highest installed MSVC toolset version under this VS install, and
REM pin every vcvarsall call in this script to it. On the original dev machine
REM two different toolsets were installed side by side, and letting vcvarsall
REM silently default to whichever one it liked caused vcpkg-built libraries
REM and the main COLMAP compile to link against different MSVC STL versions
REM (unresolved __std_search_1-style externals). Pinning explicitly, even when
REM only one toolset exists on this machine, is always correct and costs
REM nothing -- so we always do it.
set "MSVC_TOOLS_DIR=%VS_PATH%\VC\Tools\MSVC"
set "HIGHEST_TOOLSET="
for /f "delims=" %%d in ('dir /b /ad /o-n "%MSVC_TOOLS_DIR%" 2^>nul') do (
    if not defined HIGHEST_TOOLSET set "HIGHEST_TOOLSET=%%d"
)
if not defined HIGHEST_TOOLSET (
    echo [ERROR] No MSVC toolset found under "%MSVC_TOOLS_DIR%".
    goto :error
)
for /f "tokens=1,2 delims=." %%a in ("%HIGHEST_TOOLSET%") do set "VCVARS_VER=%%a.%%b"
echo [INFO] Visual Studio: %VS_PATH%
echo [INFO] MSVC toolset:  %HIGHEST_TOOLSET%  (pinning vcvars_ver=%VCVARS_VER%)
echo.

REM ============================================================
REM STAGE 1: vcpkg
REM ============================================================
REM Verify any existing checkout is actually the pinned commit (not just
REM "the directory exists") before trusting it -- a network hiccup partway
REM through a previous run's `git clone` can leave a `.git` folder and even
REM vcpkg.exe present but the checkout incomplete/wrong, which would
REM otherwise be silently treated as "already done" and produce confusing
REM failures much later instead of a clean re-clone here.
set "VCPKG_HEAD="
if exist "%VCPKG_DIR%\vcpkg.exe" for /f "delims=" %%h in ('git -C "%VCPKG_DIR%" rev-parse HEAD 2^>nul') do set "VCPKG_HEAD=%%h"
if /I "%VCPKG_HEAD%"=="%VCPKG_COMMIT%" (
    echo [1/8] vcpkg already present at %VCPKG_DIR% and pinned to %VCPKG_COMMIT%, skipping clone+bootstrap.
) else (
    echo [1/8] Cloning + bootstrapping vcpkg ^(pinned to %VCPKG_COMMIT%^)...
    git config --global core.longpaths true
    if exist "%VCPKG_DIR%" rmdir /s /q "%VCPKG_DIR%"
    git clone https://github.com/microsoft/vcpkg.git "%VCPKG_DIR%" || goto :error
    git -C "%VCPKG_DIR%" checkout %VCPKG_COMMIT% || goto :error
    call "%VCPKG_DIR%\bootstrap-vcpkg.bat" -disableMetrics || goto :error
)
echo.

REM ============================================================
REM STAGE 2: clone COLMAP
REM ============================================================
set "COLMAP_HEAD="
if exist "%COLMAP_DIR%\.git" for /f "delims=" %%h in ('git -C "%COLMAP_DIR%" rev-parse HEAD 2^>nul') do set "COLMAP_HEAD=%%h"
if /I "%COLMAP_HEAD%"=="%COLMAP_COMMIT%" (
    echo [2/8] COLMAP repo already present at %COLMAP_DIR% and pinned to %COLMAP_COMMIT%, skipping clone.
) else (
    echo [2/8] Cloning COLMAP ^(pinned to %COLMAP_COMMIT%^)...
    git config --global core.longpaths true
    if exist "%COLMAP_DIR%" rmdir /s /q "%COLMAP_DIR%"
    git clone https://github.com/colmap/colmap.git "%COLMAP_DIR%" || goto :error
    git -C "%COLMAP_DIR%" checkout %COLMAP_COMMIT% || goto :error
    pushd "%COLMAP_DIR%"
    git submodule update --init --recursive
    popd
)
echo.

REM ============================================================
REM STAGE 3: configure COLMAP (CUDA, Ninja, x64-windows-static)
REM ============================================================
if exist "%COLMAP_INSTALL_DIR%\bin\colmap.exe" (
    echo [3/8] COLMAP already built+installed at %COLMAP_INSTALL_DIR%, skipping configure+build.
    goto :stage6
)

echo [3/8] Configuring COLMAP with CUDA... (log: %LOG_DIR%\colmap_configure.log)
echo       This step also downloads/builds vcpkg dependencies (Boost, Ceres,
echo       Eigen, CGAL, etc.) the first time -- can take 30-60+ minutes.
call "%VCVARSALL%" x64 -vcvars_ver=%VCVARS_VER% || goto :error
if not exist "%COLMAP_BUILD_DIR%" mkdir "%COLMAP_BUILD_DIR%"
pushd "%COLMAP_BUILD_DIR%"
cmake .. -GNinja ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DCMAKE_INSTALL_PREFIX=../install ^
    -DCMAKE_TOOLCHAIN_FILE=%VCPKG_DIR_FWD%/scripts/buildsystems/vcpkg.cmake ^
    -DVCPKG_TARGET_TRIPLET=x64-windows-static ^
    -DVCPKG_MANIFEST_NO_DEFAULT_FEATURES=ON ^
    -DVCPKG_MANIFEST_FEATURES=cuda ^
    -DGUI_ENABLED=OFF ^
    -DCUDA_ENABLED=ON ^
    -DONNX_ENABLED=OFF ^
    -DCMAKE_CUDA_ARCHITECTURES=%CUDA_ARCH% ^
    -DCMAKE_CXX_FLAGS="/bigobj" ^
    -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded ^
    -DCMAKE_CXX_FLAGS_RELEASE="/MT /O2 /Ob2 /DNDEBUG" ^
    -DCMAKE_C_FLAGS_RELEASE="/MT /O2 /Ob2 /DNDEBUG" ^
    > "%LOG_DIR%\colmap_configure.log" 2>&1
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [ERROR] COLMAP configure failed. See %LOG_DIR%\colmap_configure.log
    goto :error
)
popd
echo [OK] Configure done.
echo.

REM ============================================================
REM STAGE 4: apply the PoseLib MSVC source patch
REM ============================================================
echo [4/8] Applying PoseLib MSVC template-overload patch...
set "POSELIB_JAC_HEADER=%COLMAP_BUILD_DIR%\_deps\poselib-src\PoseLib\robust\optim\jacobian_accumulator.h"
if not exist "%POSELIB_JAC_HEADER%" (
    echo [ERROR] Expected file not found: %POSELIB_JAC_HEADER%
    echo         COLMAP's PoseLib dependency layout may have changed upstream.
    goto :error
)
if not exist "%PATCH_DIR%\jacobian_accumulator.patched.h" (
    echo [ERROR] Missing companion patch file: %PATCH_DIR%\jacobian_accumulator.patched.h
    echo         Make sure this .bat and the colmap_cuda_patches\ folder next to
    echo         it were copied together.
    goto :error
)
copy /Y "%PATCH_DIR%\jacobian_accumulator.patched.h" "%POSELIB_JAC_HEADER%" >nul || goto :error
echo [OK] Patch applied. (Without this, MSVC fails with C2672/deduction-failure
echo      errors at PoseLib call sites -- GCC/Clang, COLMAP's normal CI
echo      compilers, accept the original code fine; only MSVC's template
echo      partial-ordering rejects it.)
echo.

REM ============================================================
REM STAGE 5: build + install COLMAP
REM ============================================================
echo [5/8] Building + installing COLMAP... (log: %LOG_DIR%\colmap_build.log)
echo       This takes several minutes once vcpkg dependencies are ready.
call "%VCVARSALL%" x64 -vcvars_ver=%VCVARS_VER% || goto :error
pushd "%COLMAP_BUILD_DIR%"
ninja > "%LOG_DIR%\colmap_build.log" 2>&1
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [ERROR] COLMAP build failed. See %LOG_DIR%\colmap_build.log
    goto :error
)
ninja install >> "%LOG_DIR%\colmap_build.log" 2>&1
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [ERROR] COLMAP install failed. See %LOG_DIR%\colmap_build.log
    goto :error
)
popd
echo [OK] COLMAP built and installed to %COLMAP_INSTALL_DIR%
echo.

:stage6
REM ============================================================
REM STAGE 6: prepare python/ vcpkg manifest files for the pycolmap sub-build
REM ============================================================
%PYTHON_EXE% -c "import pycolmap, sys; sys.exit(0 if pycolmap.__version__.startswith('4.3.0') else 1)" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [6-7/8] pycolmap 4.3.0.dev0 already installed in "%PYTHON_EXE%", skipping rebuild.
    goto :verify
)
echo [6/8] Preparing python/ vcpkg manifest files...
REM The pycolmap sub-build's CMAKE_SOURCE_DIR is python/ (per pyproject.toml's
REM [tool.scikit-build] cmake.source-dir), so vcpkg manifest-mode auto-detection
REM looks for vcpkg.json/vcpkg-configuration.json THERE, not at the repo root.
REM Without copies here, Boost/CGAL/CURL etc. silently fail to resolve.
copy /Y "%COLMAP_DIR%\vcpkg.json" "%COLMAP_DIR%\python\vcpkg.json" >nul || goto :error
copy /Y "%COLMAP_DIR%\vcpkg-configuration.json" "%COLMAP_DIR%\python\vcpkg-configuration.json" >nul || goto :error
REM Fix the copied file's overlay-ports path: it is relative to the file's own
REM directory, and python/ is one level deeper than the repo root, so the
REM original "cmake/vcpkg/ports" must become "../cmake/vcpkg/ports".
set "PYCOLMAP_VCPKG_CONFIG_FWD=%COLMAP_DIR%\python\vcpkg-configuration.json"
set "PYCOLMAP_VCPKG_CONFIG_FWD=%PYCOLMAP_VCPKG_CONFIG_FWD:\=/%"
powershell -NoProfile -Command "(Get-Content -Raw '%PYCOLMAP_VCPKG_CONFIG_FWD%') -replace '\"cmake/vcpkg/ports\"', '\"../cmake/vcpkg/ports\"' | Set-Content -NoNewline '%PYCOLMAP_VCPKG_CONFIG_FWD%'" || goto :error
echo [OK] python\vcpkg.json and python\vcpkg-configuration.json ready.
echo.

REM ============================================================
REM STAGE 7: build + install pycolmap
REM ============================================================
echo [7/8] Building + installing pycolmap python bindings... (log: %LOG_DIR%\pycolmap_build.log)
REM pycolmap's build is PEP 517 (pyproject.toml, scikit-build-core) -- an old
REM system pip on a machine that has never had reason to touch Python much
REM can fail to resolve/isolate that build correctly. Upgrading first is
REM cheap and safe (pip only, not touching any other package).
"%PYTHON_EXE%" -m pip install --upgrade pip > "%LOG_DIR%\pycolmap_build.log" 2>&1
call "%VCVARSALL%" x64 -vcvars_ver=%VCVARS_VER% || goto :error
set "colmap_DIR=%COLMAP_INSTALL_DIR%"
REM Forcing the Ninja generator (instead of pycolmap/scikit-build-core's
REM default Visual-Studio-generator) is the only way found to make this
REM sub-build actually honor our pinned MSVC toolset version -- neither
REM -DCMAKE_GENERATOR_TOOLSET=version=... nor -G "Visual Studio 17 2022"
REM -T "version=..." inside CMAKE_ARGS had any effect (scikit-build-core
REM ignores/overrides them). With CMAKE_GENERATOR=Ninja it just uses
REM whatever cl.exe vcvarsall already put on PATH, which is the pinned one.
set "CMAKE_GENERATOR=Ninja"
set CMAKE_ARGS=-DCMAKE_TOOLCHAIN_FILE=%VCPKG_DIR_FWD%/scripts/buildsystems/vcpkg.cmake -DVCPKG_TARGET_TRIPLET=x64-windows-static -DVCPKG_MANIFEST_NO_DEFAULT_FEATURES=ON -DVCPKG_MANIFEST_FEATURES="cuda;cgal;download" -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded -DCMAKE_CXX_FLAGS_RELEASE="/MT /O2 /Ob2 /DNDEBUG /bigobj" -DCMAKE_C_FLAGS_RELEASE="/MT /O2 /Ob2 /DNDEBUG"
pushd "%COLMAP_DIR%"
"%PYTHON_EXE%" -m pip install -ve . >> "%LOG_DIR%\pycolmap_build.log" 2>&1
if %ERRORLEVEL% NEQ 0 (
    popd
    echo [ERROR] pycolmap build failed. See %LOG_DIR%\pycolmap_build.log
    goto :error
)
popd
echo [OK] pycolmap built and installed.
echo.

:verify
REM ============================================================
REM STAGE 8: verify
REM ============================================================
echo [8/8] Verifying installation...
echo.
echo ---- colmap.exe ----
"%COLMAP_INSTALL_DIR%\bin\colmap.exe" --help | findstr /I "patch_match_stereo stereo_fusion" >nul
if errorlevel 1 (
    echo [WARN] colmap.exe did not report patch_match_stereo/stereo_fusion commands.
) else (
    echo [OK] colmap.exe has dense-reconstruction commands available.
)
"%COLMAP_INSTALL_DIR%\bin\colmap.exe" -h | findstr /I "CUDA" >nul
if errorlevel 1 (
    echo [WARN] colmap.exe help output did not mention CUDA.
) else (
    echo [OK] colmap.exe reports CUDA support.
)
echo.
echo ---- pycolmap ----
"%PYTHON_EXE%" -c "import pycolmap; print('pycolmap version:', pycolmap.__version__); opts = pycolmap.PatchMatchOptions(); print('gpu_index option present -> CUDA build:', hasattr(opts, 'gpu_index'))"
if errorlevel 1 (
    echo [ERROR] pycolmap import/verification failed.
    goto :error
)
echo.
echo ============================================================
echo  DONE. COLMAP + pycolmap with CUDA are installed at:
echo    %COLMAP_INSTALL_DIR%
echo  pycolmap is installed into the "%PYTHON_EXE%" environment above.
echo  Logs are under: %LOG_DIR%
echo ============================================================
endlocal
exit /b 0

:error
echo.
echo ============================================================
echo  INSTALL FAILED. Check the messages above and the logs under
echo  %LOG_DIR% for details. This script is safe to re-run after
echo  fixing the issue -- completed stages will be skipped.
echo ============================================================
endlocal
exit /b 1
