$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot

Write-Host "===== CheckCrack 개발 환경 설치 ====="
Write-Host ""

Write-Host "[1/4] Python 확인..."
$python = Get-Command python -ErrorAction SilentlyContinue
$conda = Get-Command conda -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "[경고] python이 PATH에 없습니다. Python 3.11+ 설치 후 다시 실행하세요."
}
else {
    & python --version

    Write-Host ""
    Write-Host "[2/4] Python 패키지 설치 (requirements.txt)..."
    & python -m pip install --upgrade pip
    & python -m pip install -r (Join-Path $root "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[오류] pip install 실패. 위 로그를 확인하세요."
    }

    Write-Host ""
    Write-Host "[중요] PyTorch는 여기 포함되지 않습니다. CUDA 버전에 맞춰 별도 설치해야 합니다."
    Write-Host "  https://pytorch.org/get-started/locally/ 에서 본인 GPU/CUDA에 맞는 명령을 확인하세요."
    Write-Host "  예) pip install torch --index-url https://download.pytorch.org/whl/cu121"

    Write-Host ""
    Write-Host "[3/4] WeasyPrint (PDF 보고서 엔진) 설치..."
    Write-Host "  Windows에서는 'pip install weasyprint'만으로는 동작하지 않습니다 (Pango/cairo/gobject"
    Write-Host "  네이티브 라이브러리가 없어서 'cannot load library libgobject-2.0-0' 오류로 죽음, 직접 확인함)."
    if ($conda) {
        & conda install -y -c conda-forge weasyprint
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[오류] conda install weasyprint 실패. 위 로그를 확인하세요."
        }
        else {
            Write-Host "  conda-forge 빌드는 Pango/cairo/gobject를 <conda 환경>\Library\bin 에 같이 설치합니다."
            Write-Host "  tools/generate_report.py가 실행될 때 이 경로를 PATH에 자동으로 추가하므로"
            Write-Host "  (src/report/pdf_report.py의 _ensure_native_libs) 별도 조치는 필요 없습니다."
        }
    }
    else {
        Write-Host "[경고] conda가 PATH에 없습니다. Windows에서 WeasyPrint를 쓰려면 conda(Miniconda/Anaconda)"
        Write-Host "  환경에서 'conda install -c conda-forge weasyprint'로 설치해야 합니다."
        Write-Host "  conda 없이 GTK3 런타임을 직접 설치하는 방법은 다음을 참고하세요:"
        Write-Host "  https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows"
        Write-Host "  (PDF 보고서 생성 기능만 영향받습니다 -- 스티칭/CM/크랙탐지 등 나머지는 정상 동작합니다.)"
    }
}

Write-Host ""
Write-Host "[4/4] .NET SDK 확인 + NuGet 패키지 복원..."
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) {
    Write-Host "[경고] dotnet이 PATH에 없습니다. .NET 9 SDK 설치 후 다시 실행하세요: https://dotnet.microsoft.com/download"
}
else {
    & dotnet --version

    $viewerDir = Join-Path $root "viewer\CheckCrackViewer"
    Push-Location $viewerDir
    try {
        # CheckCrackViewer.csproj가 참조하는 NuGet 패키지 전부(BCrypt.Net-Next, CommunityToolkit.Mvvm,
        # Microsoft.Data.Sqlite, PDFtoImage 포함)를 dotnet restore 하나로 자동 복원한다.
        # 패키지별로 따로 설치할 필요 없음 -- NuGet 캐시에 없으면 nuget.org에서 자동 다운로드한다.
        & dotnet restore CheckCrackViewer.csproj
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[오류] dotnet restore 실패. 위 로그를 확인하세요 (인터넷 연결/NuGet 접근 여부 점검)."
        }
        else {
            Write-Host ""
            Write-Host "복원된 주요 NuGet 패키지:"
            & dotnet list CheckCrackViewer.csproj package
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "===== 완료 ====="
Write-Host "참고:"
Write-Host " - COLMAP 폴백(pycolmap)은 위 pip 설치에 포함됩니다. 별도 COLMAP 실행파일 설치는 필요 없습니다."
Write-Host " - PDF 보고서는 Python 쪽(src/report/pdf_report.py, WeasyPrint)에서 생성합니다. 뷰어(C#)는"
Write-Host "   tools/generate_report.py를 서브프로세스로 호출만 합니다 -- 위 [3/4] 단계가 핵심입니다."
Write-Host "   한글 폰트는 Windows 기본 맑은 고딕(C:\Windows\Fonts\malgun.ttf)을 그대로 씁니다. 별도 설치 불필요."
Write-Host " - 로그인 계정(SQLite, Microsoft.Data.Sqlite)은 뷰어 최초 실행 시"
Write-Host "   %APPDATA%\SmartCrackViewer\users.db 에 자동 생성되고 admin / admin123 계정이 자동으로"
Write-Host "   만들어집니다. 설정 - 계정/연결 화면에서 계정명/비밀번호 변경 가능합니다."
Write-Host " - MySQL(설정 - 계정/연결 화면의 DB 연결)은 이 스크립트가 설치하지 않습니다. Ubuntu MySQL"
Write-Host "   서버 쪽은 scripts\setup_mysql_ubuntu.sh 를 그 서버에서 별도로 실행하세요."

Read-Host "Press Enter to exit"
