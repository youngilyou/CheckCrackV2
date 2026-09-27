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

## 0. 다른(새) 컴퓨터에서 실행하기 — 처음부터 순서대로

요약: **설치 → `check_env.bat`로 확인 → `run_stitch.bat`/`run_detect.bat` 또는 `run_viewer.bat`.**
아래 `.bat` 파일은 모두 저장소 루트에 있고 더블클릭 또는 cmd에서 실행합니다(Python은 자동으로 찾습니다 — 아래 "Python 지정" 참고).

### 필요한 것
- Windows 10/11 + **NVIDIA GPU**(RTX 4080 SUPER에서 검증) + 최신 드라이버. GPU 없이는 사실상 실행 불가(Dense Stereo는 CUDA 필수).
- RAM **64 GB 권장**(Dense Stereo 융합 단계에서 프로세스가 일시적으로 40 GB 넘게 씀), 디스크 여유 **수백 GB 권장**(한 건물 촬영분 결과 폴더의 `colmap_dense/`가 수십 GB — 270장에서 45 GB 실측, 422장은 더 큼).
- Git, Python 3.11+(Miniconda 권장), .NET 9 SDK(뷰어용), Visual Studio 2022 C++ 워크로드 + CUDA Toolkit 12.x + CMake + Ninja(아래 3번, CUDA pycolmap을 직접 빌드할 때만).

### 설치 순서
1. **받기**: `git clone https://github.com/youngilyou/CheckCrackV2.git`
2. **`setup_new_machine.bat`** — Python 패키지(`requirements.txt`) + WeasyPrint(conda-forge) + .NET NuGet 복원.
3. **CUDA PyTorch**(위 스크립트가 안 깔아줌): `pip install torch --index-url https://download.pytorch.org/whl/cu126`
4. **CUDA pycolmap 빌드** — `requirements.txt`의 pip `pycolmap`에는 **CUDA Dense Stereo가 없습니다.** `check_env.bat`이 `pycolmap dense stereo`를 FAIL로 표시하면 `scripts\install_colmap_cuda_dense.bat`을 실행하세요(COLMAP+pycolmap 4.3.x CUDA 빌드, 수 시간 걸릴 수 있음, 재실행해도 안전).
5. **크랙 모델 가중치 복사** — `*.pt`는 git에 없습니다(`.gitignore`). 원래 컴퓨터의 `training\crack_seg\models\`(`v1_all_cracks\best.pt`, `v2_exclude_structural_fp\best.pt`)를 같은 경로로 복사하세요.
6. **`check_env.bat`** — 마지막에 `RESULT: READY`가 나오면 준비 완료. FAIL 줄마다 해결 방법이 같이 나옵니다. (LoFTR 가중치는 첫 실행 때 자동 다운로드되므로 인터넷이 한 번은 필요합니다.)

### Python 지정
`.bat`과 뷰어는 같은 순서로 Python을 찾습니다: 환경변수 **`CHECKCRACK_PYTHON`**(전체 경로, 최우선) → `%USERPROFILE%\miniconda3` / `anaconda3` / `%LOCALAPPDATA%\miniconda3` / `C:\ProgramData\miniconda3` → PATH의 `python`. torch/pycolmap이 다른 conda env에 있으면 `setx CHECKCRACK_PYTHON "C:\...\envs\내env\python.exe"` 로 지정하세요(새 cmd에서 적용).

### 실행 (배치 파일)
| 파일 | 하는 일 |
|---|---|
| `check_env.bat` | 이 컴퓨터가 실행 가능한지 점검 |
| `run_stitch.bat <사진폴더> [facade이름] [--in-place] [--matcher-backend loftr\|hloc] [--structure-type APARTMENT\|DAM\|FACTORY]` | 사진 한 폴더 → 스티칭 + COLMAP + Dense Stereo. `--in-place`면 `<사진폴더>\output\Vnnn\`에 결과. **약 400장에 6시간 안팎**(RTX 4080 SUPER) |
| `run_detect.bat <결과 output 폴더> [facade이름] [--skip-v2]` | 스티칭된 결과에서 크랙 검출(원본 사진에서 검출 → 깊이로 스티칭 좌표 배치 → 위치 오차 측정) |
| `make_depth_sidecar.bat <결과 output 폴더> <사진폴더> [facade이름]` | 2026-09-26 이전에 끝난 실행(V008까지)에 깊이 사이드카 `*_depth_mapping.json` 생성 |
| `rebuild_dense_hybrid.bat <결과 output 폴더> [facade이름]` | 끝난 실행의 Dense 모자이크(`*_colmap_dense.*`)를 기존 Dense 작업 폴더(`fused.ply`)로 **몇 분 만에** 다시 합성. 합성 규칙을 바꾼 뒤 이전 결과에 적용할 때 사용(이전 결과는 `_backup_before_rebuild/`에 보관) |
| `run_viewer.bat` | 뷰어 빌드 + 실행(로그인 `admin`/`admin123`) |

뷰어의 "▶ 실행"도 같은 파이썬 스크립트를 호출합니다. 결과 화면의 클릭 위치 계산(`tools/click_locator.py`)도 뷰어가 알아서 띄웁니다.

### 결과를 다른 컴퓨터로 옮겨서 볼 때
- 결과 폴더(`<사진폴더>\output\Vnnn\`)를 **통째로**(`colmap_stage1\`, `colmap_dense\` 포함) 옮기세요. 크랙을 깊이로 배치하고 스티칭 클릭 위치를 계산하려면 이 두 폴더가 필요합니다(`FRONT_depth_mapping.json`이 상대 경로로 가리킴). 용량 때문에 뺐다면 스티칭 결과 열람은 되지만 크랙/클릭은 평면 호모그래피 방식으로 떨어지고 화면에 그렇게 표시됩니다.
- 원본 사진: 결과 폴더의 `*_source_images.json`에는 스티칭한 컴퓨터의 **절대 경로**가 들어 있습니다. 옮긴 컴퓨터에서도 자동으로 같은 파일명을 결과 폴더 위쪽(`<사진폴더>\` 또는 `<사진폴더>\images\`)에서 찾습니다. 사진이 다른 곳에 있으면 환경변수 **`CHECKCRACK_IMAGES_DIR`**에 그 폴더를 지정하세요. 못 찾은 사진은 로그에 경고로 나옵니다.

### 촬영 위치가 한 줄인 촬영 (예: LEFT — 드론이 벽을 따라 수직으로 오르내림)
- 일반 촬영(FRONT/BACK)과 같은 절차·같은 산출물(`*_colmap_dense.*`, `*_depth_mapping.json`)이 나오도록 자동 처리합니다. 사진 GPS 위치가 한 줄이면 로그에 `COLMAP_SINGLE_CAMERA`가 뜨고, 카메라 하나를 공유하며 내부 파라미터를 `config/pipeline.yaml`의 `colmap.known_intrinsics`(카메라 기종별 초점거리/왜곡)로 **고정**해서 재구성합니다. 이 값이 없는 카메라 기종은 이 경로를 못 써서 GPS/짐벌 자세 기반 대체 보정(Dense 없음)으로 끝납니다 — 새 기종은 그 기종을 잘 찍은 촬영에서 재구성된 값을 `known_intrinsics`에 추가하세요.
- 어떤 촬영이든 COLMAP과 GPS가 안 맞으면(`COLMAP_ALIGNMENT_POOR`) 실패로 끝내지 않고 GPS/짐벌 기반 모자이크로 저장합니다(이때는 Dense/깊이 사이드카가 없고, 크랙과 클릭 위치는 평면 방식으로 표시됨).
- LEFT(48장) 실측: 약 1시간 이내(1단계 2분, flat 2분, Dense 39분).

### 파이프라인 트랙 (`config/pipeline.yaml`의 `pipeline.track`)
- `reference`(현재 기본): 필터 없이 전체 이미지로 COLMAP 1단계 → flat 모자이크 → Dense Stereo 하이브리드. 결과 `*_colmap_dense.*`.
- `full`: COLMAP 1단계 → 벽 미노출 이미지 필터 → H체인 → COLMAP 2단계 → Dense.
- `dense_only`: 필터 후 Dense만(V007 방식, 위쪽/왼쪽 모서리가 어긋나 비권장).

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

## 3-1. 크랙 위치 정확도 (깊이 기반 배치, 2026-09-26)

- 크랙은 **원본 사진 각각**에서 검출하고, 각 픽셀의 COLMAP 깊이(`colmap_dense/dense/stereo/depth_maps`)로 스티칭 좌표에 올립니다(`src/geometry/depth_mapping.py`). 예전의 평면 호모그래피 배치는 벽 굴곡 때문에 표시 모자이크와 중앙값 약 14 cm 어긋났고, 깊이 배치는 약 1.8 px(1.8 cm)입니다(V008/V009 실측).
- 각 크랙의 `position_check`(JSON)와 뷰어의 "위치 오차 … px · 깊이 배치"는 원본 픽셀을 스티칭에 올려 표시 모자이크와 비교한 실측값입니다. 측정 못 한 것은 "미측정"(0 아님).
- 스티칭 화면 클릭 → 원본 사진 마커도 깊이로 계산하고(`tools/click_locator.py`), 보고서 버튼 옆에 "선택 위치 오차"를 표시합니다.
- 한계: 크랙 길이/폭 mm 측정은 아직 평면 가정 스케일이고, 검출 모델의 얼룩/벗겨진 페인트 오탐은 별도 문제입니다. 자세한 근거와 수치는 `CLAUDE.local.md`의 2026-09-26 기록 참고.

## 4. 빌드 결과물 제외

`viewer/**/bin/`, `viewer/**/obj/`는 `.gitignore`에 포함되어 있어 커밋되지 않습니다 — 위 `dotnet build`로 로컬에서 생성하세요.
