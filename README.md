# CheckCrackV2

DJI 드론 촬영 이미지를 Facade 단위로 자동 분리·스티칭하고, 균열(Crack)을 탐지/측정하는 파이프라인입니다.
전체 설계 원칙과 아키텍처는 [CLAUDE.local.md](CLAUDE.local.md) 참고 (건물 전체를 하나로 스티칭하지 않음, N/E/S/W 4면 고정 아님, Kornia+COLMAP 기반 정합, Observed/Occluded 상태 모델 등).

구성:
- `src/` — Python 파이프라인 (메타데이터 파싱 → Facade 분류 → LoFTR/RANSAC 매칭 → 스티칭 → COLMAP 보정 → Crack 세그멘테이션)
- `tools/` — 독립 실행 CLI 스크립트
- `viewer/` — C# WPF 진행상황 모니터링 뷰어
- `config/` — 파이프라인 설정 (YAML)
- `datasets/`, `facades/` — 원본 이미지/결과물 폴더 (내용물은 git에 포함 안 됨, 각 폴더의 README 참고)

현장 촬영용 도구(FacadePreviewer, DDS 실시간 영상 캡처 + 오프라인 스티칭)는 별도
저장소([youngilyou/FacadePreviewer](https://github.com/youngilyou/FacadePreviewer))로
완전히 분리되어 있습니다 — 이 저장소는 이 도구와 무관하게 독립적으로 동작합니다.

---

## 1. 다운로드

```bash
git clone https://github.com/youngilyou/CheckCrackV2.git
cd CheckCrackV2
```

`datasets/`, `facades/` 폴더의 실제 이미지/결과물(TIF 등 대용량 바이너리)은 git에 포함되어 있지 않습니다 — 각 폴더의 `README.md` 참고.

## 2. Python 파이프라인 빌드/설치

Windows + NVIDIA GPU(CUDA) 환경 기준입니다.

```bash
# 1) CUDA 빌드 torch 먼저 설치 (일반 pip install torch는 CPU 전용이라 이 프로젝트 속도에 안 맞음)
pip install torch --index-url https://download.pytorch.org/whl/cu126

# 2) 나머지 의존성
pip install -r requirements.txt
```

GPU 없는 환경에서는 1번을 생략하면 CPU로 동작하지만 스티칭/매칭이 훨씬 느립니다.

### 실행

```bash
# 폴더 하나 = facade 하나 (가장 간단한 방법)
python tools/stitch_folder.py <이미지_폴더> [facade_이름]

# 하위 폴더별로 여러 facade를 한 번에 (좌/우/앞/뒤/top 등)
python tools/stitch_all_folders.py <상위_폴더>

# 건물 footprint 기반 (Phase 2, 실제 다면체 건물 자동 분할)
python -m src.pipeline.runner run-building --building <id> --footprint <footprint.txt> --utm-epsg <epsg코드> --images-dir <폴더>

# 스티칭된 facade에서 균열 탐지 (위 명령이 만든 output/ 폴더 대상)
python tools/detect_cracks_folder.py <facade_output_dir> [facade_이름]

# PDF 검사 보고서 생성 (WeasyPrint 필요 — Windows는 pip 대신 conda-forge로 설치,
# scripts/setup_dev_machine.ps1 참고)
python tools/generate_report.py facade <facade_output_dir> <facade_이름>
```

결과물은 `facades/<facade_id>/output/`에 생성됩니다 (`--in-place` 옵션을 주면 선택한 이미지 폴더 바로 아래 `output/`에 생성).

## 3. C# Viewer 빌드/실행

솔루션: [`viewer/CheckCrackViewer.sln`](viewer/CheckCrackViewer.sln)  (.NET 9, WPF, Windows 전용)

```bash
cd viewer
dotnet build
# 빌드된 실행 파일 실행
./CheckCrackViewer/bin/Debug/net9.0-windows/CheckCrackViewer.exe
```

또는 Visual Studio에서 `CheckCrackViewer.sln` 열어서 F5.

최초 실행 시 로그인 화면이 뜹니다 — `%APPDATA%\SmartCrackViewer\users.db`(SQLite)에 계정이 자동 생성되고, 기본 계정은 `admin`/`admin123`입니다(설정 화면에서 변경 가능).

로그인하면 프로젝트 루트(`CLAUDE.local.md`가 있는 폴더)를 자동으로 찾아서 `facades/`, `logs/pipeline.log`를 모니터링합니다. "+ 폴더" 버튼으로 이미지 폴더를 선택해 직접 파이프라인을 실행하고 실시간 스티칭 진행 상황을 볼 수 있습니다.

## 4. 빌드 결과물 제외

`viewer/**/bin/`, `viewer/**/obj/`는 `.gitignore`에 포함되어 있어 커밋되지 않습니다 — 위 `dotnet build`로 로컬에서 생성하세요.
