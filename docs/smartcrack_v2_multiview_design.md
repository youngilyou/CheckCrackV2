# SmartCrack V2 — 원본 우선 검출 + Facade 통합 설계

원안(SmartCrack V2 텍스트 문서)을 검토하고 2026-08-26 세션에서 확정/보강한 내용을
정리한 문서. CLAUDE.local.md의 절대 원칙(#9 calibration 없이 mm 금지, #43.6 큰
이미지 리사이즈 금지 등)과 충돌하지 않는 것을 코드로 직접 검증하며 진행했다.

## 1. 핵심 아키텍처 (확정)

```
DJI ORIGINAL
     |
  +--+----------------------+
  |                         |
#1 CRACK PIPELINE      #2 GEOMETRY PIPELINE
  |                         |
조명/그림자 보정          원본 그대로
  |                         |
Crack Segmentation        COLMAP/Homography (기존 구현, 변경 없음)
  |                         |
image_id/bbox_px/         Stitching -> H(원본->facade), seam_owner_map
polygon_px/confidence     (기존 구현, 변경 없음)
  |                         |
  +----------+--------------+
             |
      Coordinate Mapping (H 정방향 투영, crack/multiview.py)
             |
      Facade 공통 좌표계
             |
   [1] Duplicate Association  -- 같은 크랙이 여러 장에 "전체가" 겹쳐 보임
             |
   [2] Crack Continuation Association -- 크랙이 사진 경계로 "반씩" 갈라져 보임
             |
      FINAL Crack Geometry (Width/Length/Area 여기서 1회 계산)
             |
      Facade Mosaic Overlay / Viewer / DB / Report
```

**#1과 #2는 완전히 독립**이고, "Coordinate Mapping" 단 한 지점에서만 만난다 — #2가
이미 계산해둔 `H`를 빌려 #1의 결과 좌표만 옮기는 것이지, 서로의 내부 처리에
관여하지 않는다.

### 1.1 좌표 방향 (실측 검증 완료)

`stitching/warp.py`가 저장하는 `H`는 **원본 → facade(mosaic)** 방향이다
(`warp_images()`가 각 원본을 캔버스에 그릴 때 쓰는 그 방향). 기존
`crack/pipeline.py`의 `_compute_source_observations`는 이 반대 방향(`H^-1`)을
써서 mosaic에서 검출한 크랙을 원본으로 되돌린다. `crack/multiview.py`는 반대로
원본에서 검출한 크랙을 `H` **정방향**으로 곱해 facade 좌표로 투영한다 — 같은
`H`, 반대 적용 방향.

검증: 실제 `testImg_homographies.json`의 4개 이미지 각각에서 임의의 점을
`H`로 정방향 변환 후 `H^-1`로 역변환했을 때 원점과의 오차가 0.000000px임을
확인(라운드트립 항등성 테스트).

## 2. Duplicate Association vs Crack Continuation Association (핵심 구분)

원안의 "Duplicate Removal" 섹션은 한 가지 문제만 다루고 있었다 — 실제로는 두
가지 다른 문제다.

**A. Duplicate Association** — 같은 크랙 전체가 여러 원본에 겹쳐 보임(overlap
70~80% 촬영 정책상 흔함):
```
DJI_0122 : Crack A 전체
DJI_0123 : Crack A 전체
```
→ facade 좌표에서 두 polygon이 실질적으로 겹침(IoU) → 병합.

**B. Crack Continuation Association** — 크랙이 사진 경계에서 갈라져 각 사진엔
절반씩만 보임(겹침이 거의 없음):
```
DJI_0122 : Crack A의 왼쪽 절반
DJI_0123 : Crack A의 오른쪽 절반
```
→ facade 좌표에서 두 polygon은 겹치지 않음 → Duplicate Association으로는 못
잡음 → **skeleton 끝점(endpoint)** 기반으로 별도 판정: 끝점 사이 거리(gap)가
작고, 두 끝점의 접선(tangent) 방향이 서로를 향해 정렬돼 있으면 하나로 연결.

이 둘은 목적 자체가 다르므로 threshold도 분리한다(`config/pipeline.yaml`의
`multiview:` 섹션 — `duplicate_iou_threshold`/`duplicate_max_center_distance_px`
vs `continuation_max_gap_px`/`continuation_max_tangent_angle_deg`).

### 2.1 구현 및 검증 (`src/crack/multiview.py`)

- `associate_duplicates`: STRtree + union-find로 facade 좌표계 polygon의 IoU/중심거리
  기반 병합. 같은 원본 이미지 내부 fragment는 대상에서 제외(그건 이미
  `merge_tiles.py`가 타일 단위로 처리함).
- `_find_endpoints`: skeleton 픽셀의 8-connectivity 그래프에서 degree-1(진짜
  끝점) 픽셀을 찾고, 분기/폐곡선처럼 애매한 경우 최원거리 두 점으로 대체.
- `_tangent_at_endpoint`: 끝점 근방 skeleton 점들의 중심에서 끝점 방향 벡터 —
  바깥쪽을 향하는 접선.
- `associate_continuations`: 끝점 gap과 두 접선-연결선 각도를 모두 확인해서만
  병합(각도 조건 없이 거리만 보면 십자로 만나는 무관한 두 크랙도 잘못 이어붙일
  위험이 있어 반드시 둘 다 검사).
- 유닛 테스트 4개(겹치는 다른 이미지 fragment 병합/같은 이미지 fragment
  비병합/근접·동일선상 fragment 이어붙임/수직으로 만나는 fragment 거부) 전부
  통과.
- 실제 검증: `facades/testImg`(DJI_0191~0194, 5280×3956) + 실제 학습된
  YOLOv8-seg 모델로 end-to-end 실행 — 38개 최종 크랙 생성, 그 중 여러 개가 2~4개
  원본에 걸쳐 정확히 병합됨(예: 4개 원본 전부에서 관측된 크랙 1건).

### 2.2 Width/Length/Area는 어디서 계산하는가 (확정)

**최종 병합 geometry에서 딱 한 번만 계산**한다 — `#1 CRACK PIPELINE` 단계(원본별)
에는 절대 두지 않는다.

- Length/Area를 원본 단위로 먼저 계산해서 합치면: Duplicate 케이스는 overlap
  구간이 중복 계산되고, Continuation 케이스는 애초에 절반짜리 값만 나온다(이어
  붙이기 전에는 정의 자체가 안 됨).
- Width는 국소적(local) 값이라 원본 단위 계산 자체는 가능하지만, 최종 대표값
  (mean/max)은 반드시 최종 병합 mask/centerline 기준으로 다시 계산한다 — 원본별
  값은 provenance로만 보존(`SourceObservation`).

## 3. 조명/그림자 보정 (`src/illumination/`)

### 3.1 검토한 방법과 최종 결정

| 방법 | 상태 | 사유 |
|---|---|---|
| Zero-DCE/Zero-DCE++/Retinexformer/dark_infer_collections | 배제 | 야간/저조도 도메인, 낮 외벽 그림자와 불일치. 라이선스 미검증 |
| Classical LAB/Retinex-lite (직접 구현) | **채택, MODE 0/1/2** | 학습 데이터 불필요, 결정론적, 좌표 100% 보존, 라이선스 리스크 없음 |
| SID (ICCV 2019, MIT) | **배제** | 코드 직접 확인 결과 입력을 256×256 강제 리사이즈하고 최종 출력도 256×256 — 좌표 보존 규칙 위반. 우회하려면 예측 파라미터(6개 전역 스칼라 + 256해상도 alpha matte)만 뽑아 원본 해상도에 직접 재적용해야 하는데, 그래도 alpha matte 자체가 256 단계에서 이미 뭉개진 뒤라 공간 정밀도가 classical 방식보다 떨어짐 |
| PhaSR (CVPR 2026, MIT) | **효과는 확인, 품질 문제로 보류 중, MODE 3** | 아래 3.4절 참고 |

**상업적 라이선스 문제(별도 트래킹, 지금은 보류)**: PhaSR은 DepthAnything-V2의
Large 체크포인트(CC-BY-NC-4.0, 비상업적 전용)를 필수로 요구한다 — PhaSR 자체는
MIT이지만 이 의존성 때문에 **지금 상태로는 상업 서비스에 못 씀**. 사용자 지시로
지금 단계는 라이선스를 배제하고 기술 검증만 진행 중이나, **실제 서비스 출시
전에 반드시 재검토** — Apache-2.0 Small 모델로 재학습하거나 별도 상업 라이선스
협의 필요.

### 3.4 PhaSR 전체 해상도 OOM → 타일링 → 품질 문제 (2026-08-26, 진행 중)

- **OOM 실측**: 5280×3956 원본을 그대로 넣으면 `torch.OutOfMemoryError: Tried
  to allocate 80.41 GiB`(GPU는 16GB) — `model.py`의 GSRA 융합 단계가 DINO
  특징을 전체 이미지 크기로 다시 업샘플링하는 부분에서 발생. DINO 자체의
  14/8 정렬 비율(고정값, 손대면 안 됨)과는 다른 별개의 업샘플.
- **타일링으로 해결**: 768px 타일, 96px overlap, raised-cosine feather 블렌딩
  (`src/illumination/phasr_wrapper.py`). 실측 속도: 워밍업(모델 로딩 12.6초)
  후 타일당 0.7초, 사진 1장(48타일) 총 37.8초 — 실용적인 속도.
- **그런데 타일링 후 실제 사진 2장(DJI_0191, DJI_0173)에서 심각한 품질 문제
  재현**: 콘크리트 표면 변색(누런색), 나무 영역 파란 색조, 창문 근처 빨간 점
  아티팩트, 타일 경계 얼룩/체커보드. 타일 하나만 잘라 테스트해도 크랙 선 자체가
  검은색→갈색으로 변색됨(그림자로 오인하는 것으로 추정).
- **사용자 판정(2026-08-26)**: classical LIGHT/RETINEX는 DJI_0173의 강한
  발코니 그림자 줄무늬를 거의 못 지운 반면(보정 강도 미조정 문제로 추정, Phase
  B에서 튜닝 대상), PhaSR은 아티팩트에도 불구하고 그림자를 확실히 지웠다 —
  **PhaSR을 포기하지 말고 보완할 것**으로 결정.
- **원인 가설(아직 미적용, 사용자가 직접 조사 후 재개 예정)**:
  1. `_depth_and_normal()`이 타일마다 독립적으로 depth를 정규화(`(depth-min)/
     (max-min)`) — 같은 실제 깊이가 타일마다 다른 값으로 매핑돼 인접 타일 간
     기하 정보가 불일치. 전체 이미지에서 depth를 한 번만 계산(OOM 여부 미확인,
     PhaSR+DINO만 OOM났지 DepthAnythingV2 단독은 안 켜봄) 후 타일별로 잘라
     쓰는 방식이 유력한 해법.
  2. `_depth_to_point()`가 타일마다 자기 자신을 중심으로 한 가짜 카메라로
     취급(cx/cy를 타일 크기 기준으로 계산) — 실제로는 하나의 카메라로 찍은
     사진의 크롭이므로, 전체 이미지 기준 focal length + 타일 위치만큼
     오프셋된 cx/cy를 써야 함.
  3. `torch.amp.autocast(..., dtype=torch.bfloat16)` — GSRA가 두 attention map을
     **빼는** 연산(`A_rect = A_sem - λ·A_geo`)을 하는데, 낮은 정밀도에서 뺄셈은
     노이즈를 증폭시키는 대표적 케이스. fp32로 테스트 필요(아직 안 함).

### 3.2 MODE 스위치 방식

코드를 고쳐가며 바꾸는 게 아니라 `config/pipeline.yaml`의 `illumination.mode`
값 하나로 스위치한다(`ORIGINAL`/`LIGHT`/`RETINEX`/`PHASR`). 0.3mm 검증 스크립트는
이 값을 자동으로 순회하며 같은 테스트 이미지에 전부 돌려 precision/recall/폭
오차를 표로 비교한다.

### 3.3 Classical LAB/Retinex-lite 검증

- MODE_ORIGINAL은 완전 항등(identity) — 확인됨.
- LIGHT/RETINEX 둘 다 입력과 출력 shape/dtype 동일(좌표 불변) — 확인됨.
- 합성 half-shadow 이미지(왼쪽 어두움/오른쪽 밝음)로 테스트: 어두운 쪽만 밝아지고
  이미 밝은 쪽은 거의 안 변함(soft mask가 의도대로 작동) — 확인됨.
- 실제 DJI 사진(옥상/파라펫 사선 촬영)으로도 실행 — 인테리어 창문처럼 완전히
  검은 영역은 무리하게 밝히지 않음(가짜 텍스처 생성 방지 원칙과 일치, 진짜
  어두운 영역은 정보 자체가 없으므로 복원하지 않는 게 맞는 동작).

## 4. Phase A/B/C 재구성 (사용자 확정, 2026-08-26)

150mm duplicate threshold, MODE 0/1/2, 0.3mm 목표는 전부 **확정 파라미터가
아니라 검증 항목**으로 취급한다. 기술 개발과 현장 검증을 분리한다.

- **Phase A (실사 전, 지금)**: V2 코드 구조 완성 — 원본 Crack Detection, 좌표
  저장, COLMAP Mapping, Duplicate/Continuation 로직, 측정 알고리즘, Viewer/DB.
  지금 있는 데이터(`facades/testImg`)로 값이 실제로 나오는 것까지 확인한다.
- **Phase B (드론 확보 후)**: 실제 아파트 촬영, GT 데이터 구축, 150mm 계열
  threshold 튜닝, MODE 0/1/2(/3) A/B 테스트, 0.3mm 검출/측정 검증, 거리별 성능
  검증.
- **Phase C (현장 반복 검증)**: 여러 재질/정면·사선/그림자/줄눈·창틀/장단거리
  조건별 반복, 최종 threshold 고정.

**기체 확보보다 GT(폭을 아는 기준 균열 시편/실측 균열) 확보가 우선** — 드론이
있어도 GT가 없으면 0.3mm 성능을 입증할 수 없다(운용자 가이드에 반영 완료,
`tools/build_operator_camera_settings_ppt.py`의 "0.3mm 정량측정 검증 전용
준비물" 슬라이드).

문서/명세에 150mm·0.3mm·MODE 수치를 표기할 때는 다음 문구를 병기한다:

> ※ 본 값은 초기 검증 기준이며, 실제 DJI/Matrice 외벽 촬영 데이터와 Ground
> Truth 기반 성능평가 후 최종 확정한다.

## 5. DJI 카메라 메타데이터 (참고, `src/capture/dji_metadata.py`)

- JPEG에 실제로 들어있는 것: EXIF(시각/해상도/focal length), XMP
  GPS/자세(Flight·Gimbal Yaw/Pitch/Roll), DJI 자체 보정값
  (`CalibratedFocalLength`/`CalibratedOpticalCenterX/Y`, px 단위 — 이미 파싱은
  되는데 아직 아무 데서도 안 씀).
- JPEG에 절대 없는 것: 센서 물리 크기(mm) — `config/camera.yaml` 자체가 아직
  없음(실제 공백), 렌즈 왜곡계수(COLMAP 자체 추정에 의존, 이건 의도된 설계),
  원시 IMU/비행로그(별도 파일), RTK 상태/정밀도(파서 미구현).
- 운용자 준비 가이드는 이미 상당 부분 존재(`tools/build_operator_camera_settings_ppt.py`,
  2026-08-17) — 이번에 0.3mm 전용 슬라이드만 추가.
