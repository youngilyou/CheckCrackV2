# CLAUDE.local.md

# DJI Building Facade Mapping & Crack Inspection Platform

> 목적: DJI 드론으로 건물 외벽을 측면 촬영하고, 건물 형상에 따라 Facade Segment를 자동 분리한 뒤 Segment별로 독립 Stitching하고, 저층 가림 영역은 Ground Camera 실측 이미지로 보완하며, YOLO Crack Segmentation으로 균열 위치/길이/폭/검사 가능 여부를 산출한다.
>
> 이 문서는 Claude/Codex가 실제 구현 시 따라야 하는 개발 지침이다. 임의로 단순화하지 말 것.

---

## 0. 절대 원칙

1. 건물 전체 360도를 하나의 Stitching 결과로 만들지 않는다.
2. `North/East/South/West` 4면 고정 모델에 종속하지 않는다.
3. 실제 처리 단위는 `Facade_001 ... Facade_N` 가변 Segment이다.
4. N/E/S/W는 UI/관리용 방향 그룹일 뿐 실제 Stitching ID가 아니다.
5. 실시간 RTMP 영상과 정밀 균열 분석용 고해상도 사진을 분리한다.
6. 나무/수풀/차량 등에 가린 벽을 Generative AI로 복원하지 않는다.
7. `NO_CRACK`과 `OCCLUDED`를 절대 동일하게 처리하지 않는다.
8. 대형 Facade 이미지를 640×640 하나로 축소해 Crack 분석하지 않는다.
9. mm 단위 Crack 폭/길이는 Camera Calibration 또는 신뢰 가능한 Scale이 있을 때만 출력한다.
10. COLMAP은 항상 필수가 아니다. Kornia 정합 품질이 부족할 때 정밀 보정 경로로 사용한다.
11. 모든 Crack 결과는 원본 사진까지 provenance를 역추적할 수 있어야 한다.
12. 1 Facade = 1 Flight를 기본 운용 정책으로 한다.

---

# 1. 최종 시스템 아키텍처

```text
DJI Mavic 3E / Matrice 4E
        │
        ├──────────── 실시간 영상 ─────────────┐
        │                                      │
        │                                  RC + Pilot 2
        │                                      │
        │                                     RTMP
        │                                      │
        │                                  ZLMediaKit
        │                                 ├─ RTSP/WebRTC -> 자체 Video Viewer
        │                                 └─ 자체 멀티채널 Video Viewer (개발/검증용 Optional)
        │
        └──────────── 고해상도 사진 ─────────────────────────┐
                                                            │
                                                    DJI Metadata Parse
                                                            │
                                                    Building Footprint
                                                            │
                                                    Facade Segmentation
                                                      F001 ... Fnnn
                                                            │
                                            Waypoint + GPS + Gimbal
                                                            │
                                                    Image Assignment
                                                            │
                                              Facade별 독립 처리
                                                            │
                                                     Kornia LoFTR
                                                            │
                                               RANSAC / Homography
                                                            │
                                                      Quality Gate
                                                  ┌─────────┴─────────┐
                                                GOOD                 BAD
                                                  │                   │
                                                  │                COLMAP
                                                  │              Camera Pose
                                                  │                   │
                                                  └─────────┬─────────┘
                                                            │
                                                   Facade Rectification
                                                            │
                                                   Stitch / Blend
                                                            │
                                                   Facade_xxx.tif
                                                            │
                                           Low-floor Ground Supplement
                                                            │
                                                 Observed/Occluded Mask
                                                            │
                                                    Overlap Tiling
                                                            │
                                                YOLO Crack Segmentation
                                                            │
                                                   Tile Merge / Restore
                                                            │
                                              Crack Length / Width / Map
                                                            │
                                                   Human Verification
                                                            │
                                                    Report / History
```

---

# 2. 사용 모듈

## 2.1 Kornia
Repository: https://github.com/kornia/kornia

주요 역할:
- LoFTR Feature Matching
- RANSAC
- Homography
- Perspective transform
- `warp_perspective`
- PyTorch/CUDA 기반 GPU 처리

주의:
- Kornia에 OpenCV가 포함된 것이 아니다.
- 본 프로젝트에서는 기하 정합 Core를 Kornia 중심으로 구현한다.

기본 흐름:

```text
Image A + Image B
    ↓
LoFTR
    ↓
Matched Keypoints
    ↓
RANSAC
    ↓
Homography
    ↓
warp_perspective
```

## 2.2 OpenCV
Repository: https://github.com/opencv/opencv

역할:
- Image I/O
- SIFT/ORB fallback
- Mask processing
- Camera calibration
- Seam finding
- Exposure compensation
- Multi-band blending
- Final large Mosaic utility

역할 분리:

```text
Kornia = Feature/Geometry Core
OpenCV = Image Utility + Blend + Fallback
```

## 2.3 COLMAP
Repository: https://github.com/colmap/colmap

역할:
- SfM
- Camera Pose
- Bundle Adjustment
- 누적 drift 보정
- Parallax/복합 geometry 대응

COLMAP 호출 조건:
- Homography global drift 과다
- 반복 창문 패턴으로 오정합 증가
- 카메라 거리 변화 큼
- Roll/Pitch/Yaw 변화 큼
- 발코니/돌출 구조가 많음
- 동일 Facade Segment 내부 parallax 큼

## 2.4 ZLMediaKit
Repository: https://github.com/ZLMediaKit/ZLMediaKit

상태:
- 현재 별도 환경에서 설치 및 스트리밍 검증 완료.

역할:

```text
Pilot 2
  ↓ RTMP
ZLMediaKit
  ├ RTSP -> 자체 멀티채널 Video Viewer
  └ WebRTC -> Browser
```

정밀 Crack 분석에는 RTMP 프레임이 아니라 Original DJI Still Image를 우선한다.

## 2.5 DJI Pilot 2 / RC

대상:
- Mavic 3E + RC Pro Enterprise
- Matrice 4E + RC Plus 2

개념:

```text
Drone Camera
   ↓ AirLink
RC
   ↓
Pilot 2
   ↓
RTMP
   ↓ Internet
ZLMediaKit
```

제약:
- RC 일반 메뉴에 임의 RTMP URL 입력 기능이 항상 있다고 가정하지 않는다.
- 실제 RC/Pilot 2 Firmware 조합에서 Live Streaming Module/JSBridge/Cloud API 제어 경로를 PoC로 검증한다.
- FlightHub 2는 현재 PoC 필수 아님.
- GB28181도 필수 아님.
- DJI Cloud Backend 전체도 영상 PoC 단계 필수 아님.
- 다만 Pilot 2에서 RTMP push를 시작하기 위한 최소 Live Module/JSBridge/Cloud API 제어가 필요할 수 있다.

## 2.6 비디오 Viewer

본 시스템에는 **별도의 자체 비디오 Viewer가 존재한다.**

역할 분리:

```text
ZLMediaKit
   │
   └─ RTSP/WebRTC/지원 스트림
            ↓
       자체 멀티채널 Video Viewer
       - 실운용 영상 표시
       - 다중 채널/다중 패널 표시
       - 향후 AI Overlay
       - GCS/관제 UI 연계
       - DDS 기반 상태/제어/메타데이터/AI 결과 연동

DDS
   ├─ Drone / Flight 상태
   ├─ Mission / Facade / Image Metadata
   ├─ Viewer 제어 및 상태
   ├─ Stitching / Pipeline 상태
   └─ Crack AI 분석 결과
```


## 2.7 Crack Segmentation
PoC 후보:
https://huggingface.co/OpenSistemas/YOLOv8-crack-seg

권장 시작:
- YOLOv8m-seg
- YOLOv8l-seg

주의:
- 공개 모델이 아파트 외벽에 최적화되었다고 가정하지 않는다.
- 실제 DJI 외벽 데이터로 Finetuning을 계획한다.
- 상용 적용 전에 라이선스/배포 조건 재검증.

---

# 3. 촬영 전략

## 3.1 1 Facade = 1 Flight

기본:

```text
Flight #1 -> Facade group A -> Battery #1
착륙 / 교체
Flight #2 -> Facade group B -> Battery #2
착륙 / 교체
...
```

직사각형 건물 예:

```text
Battery #1 -> North
Battery #2 -> East
Battery #3 -> South
Battery #4 -> West
Battery #5 -> Low-floor/Occlusion supplement
Battery #6 -> Spare/Re-shoot
```

장점:
- 한 Facade 내부 sequence 연속성
- Stitching session 단순화
- 데이터 관리 용이
- Flight ID와 Facade ID 연계 용이
- 배터리 교체가 Facade 중간에 끼지 않도록 계획 가능

## 3.2 20층 아파트 시간 가정

초기 목표:
- 현장 촬영 약 1~1.5시간
- 여유 포함 최대 약 2시간

단 SLA로 고정 금지.

실제 시간 영향 요소:
- 건물 높이
- 외벽 폭
- 목표 GSD
- overlap
- 풍속
- 수목/장애물
- 비행 규제
- 재촬영 비율

## 3.3 고해상도 Still 우선

```text
Original JPEG/DNG
    ↓
Metadata
    ↓
Stitch
    ↓
Tile
    ↓
Crack Segmentation
```

영상 프레임:
- Live Monitoring
- Coverage Preview
- Mission Validation
- AI Preview

정밀 측정용 원본 대체 금지.

## 3.4 Overlap

초기값:

```yaml
capture:
  horizontal_overlap_target: 0.80
  vertical_overlap_target: 0.80
  minimum_overlap: 0.70
```

실제 카메라/FOV/GSD에 따라 튜닝.

---

# 4. 건물 형상 처리

## 4.1 4면 고정 금지

잘못된 내부 모델:

```text
North
East
South
West
```

올바른 내부 모델:

```text
Building
  ├ Facade_001
  ├ Facade_002
  ├ Facade_003
  ├ ...
  └ Facade_N
```

방향명은 별도 metadata:

```yaml
facade:
  id: F003
  direction_group: NORTH
```

## 4.2 Building Footprint

입력 우선순위:
1. CAD/BIM
2. GIS footprint
3. 수동 polygon
4. 향후 3D point cloud 추출

각 edge:
- start
- end
- length
- tangent
- outward normal
- local origin

## 4.3 Facade Segment Merge/Split

예:

```yaml
facade_segmentation:
  merge_if_normal_angle_delta_deg: 8
  split_if_normal_angle_delta_deg: 15
  min_segment_length_m: 2.0
```

PoC 후 조정.

## 4.4 부채꼴 / 곡면

하나의 Homography로 전체 곡면을 펴지 않는다.

```text
Curved Wall:
F01 F02 F03 F04 F05 F06 ...
```

각 Segment마다 Local Plane 정의:

```text
Facade_001 -> Local Plane -> Stitch
Facade_002 -> Local Plane -> Stitch
...
```

필요 시 UI에서 하나의 Building으로 연결 표시.

---

# 5. 이미지 메타데이터

최소 schema:

```yaml
image:
  image_id:
  file_path:
  timestamp_utc:
  drone_model:
  camera_model:
  width:
  height:

  gps:
    latitude:
    longitude:
    altitude_m:

  drone_pose:
    yaw_deg:
    pitch_deg:
    roll_deg:

  gimbal_pose:
    yaw_deg:
    pitch_deg:
    roll_deg:

  camera:
    focal_length_mm:
    equivalent_focal_length_mm:
    sensor_width_mm:
    sensor_height_mm:
    calibrated: false

  mission:
    flight_id:
    waypoint_id:
    facade_hint:
```

없는 값은 `null`.
임의 생성 금지.

---

# 6. Facade 자동 분류

분류 우선순위:

```text
Mission/Waypoint Hint
        +
Drone GPS
        +
Building Footprint
        +
Gimbal/Camera Direction
        ↓
Final Facade Segment
```

## 6.1 후보 Facade

Drone 위치와 Facade plane/edge 관계로 후보 생성.

단순 nearest만으로 확정하지 않는다.

## 6.2 View Validation

비교:
- camera optical axis
- drone -> facade center vector
- facade normal

예:

```yaml
facade_assignment:
  max_camera_to_target_angle_deg: 25
  corner_duplicate_angle_deg: 35
  minimum_score: 0.60
```

## 6.3 Score

```text
score =
  w_position * position_score
+ w_view * view_score
+ w_waypoint * waypoint_score
+ w_distance * distance_score
```

낮은 score:
`UNASSIGNED`.

## 6.4 Corner Image

모서리 사진은 한 Facade에 강제하지 않는다.

예:

```json
{
  "image_id": "IMG_0101",
  "facades": ["F001", "F002"],
  "role": "CORNER_OVERLAP"
}
```

파일 복사보다 DB relation 권장.

---

# 7. Pair Selection

N장 전체 N² matching 금지.

후보:
- 같은 Facade
- 같은 Flight 우선
- timestamp neighbor
- GPS 거리
- view angle
- predicted overlap

초기값:

```yaml
matching:
  temporal_neighbor_count: 4
  max_gps_distance_m: 20
  max_view_angle_delta_deg: 20
```

---

# 8. LoFTR Matching

Pseudo:

```python
def match_pair(img_a, img_b):
    a, scale_a = preprocess_for_loftr(img_a)
    b, scale_b = preprocess_for_loftr(img_b)

    result = loftr({"image0": a, "image1": b})

    pts0 = result["keypoints0"]
    pts1 = result["keypoints1"]
    conf = result["confidence"]

    keep = conf >= cfg.loftr.min_confidence

    pts0 = scale_back(pts0[keep], scale_a)
    pts1 = scale_back(pts1[keep], scale_b)

    return pts0, pts1, conf[keep]
```

원본 이미지를 LoFTR 입력 크기로 영구 downscale하지 않는다.
Matching 좌표는 반드시 원본 resolution 기준으로 복원.

---

# 9. RANSAC / Homography

출력:
- `H_ij`
- inlier mask
- inlier ratio
- reprojection error

초기 Quality Gate:

```yaml
geometry:
  min_matches: 50
  min_inliers: 30
  min_inlier_ratio: 0.35
  max_median_reprojection_error_px: 3.0
```

실패 코드:
- LOW_MATCH
- LOW_INLIER
- HIGH_REPROJECTION_ERROR
- DEGENERATE_HOMOGRAPHY

Fallback:
1. 다른 neighboring frame
2. SIFT/ORB
3. LoFTR parameter 변경
4. COLMAP
5. manual review

---

# 10. Global Stitch Graph

단순 chain 누적:

```text
H01 * H12 * H23 ...
```

만 사용하지 않는다.

Graph 구성:

```text
IMG1 -- IMG2 -- IMG3
  \      |      /
        IMG4
```

확인:
- cycle consistency
- disconnected component
- drift
- overlap graph coverage

---

# 11. Stitch Quality

Segment별 저장:

```yaml
quality:
  image_count:
  matched_pair_count:
  failed_pair_count:
  mean_inlier_ratio:
  median_reprojection_error_px:
  global_drift_score:
  coverage_ratio:
  observed_ratio:
  occlusion_ratio:
```

---

# 12. COLMAP Fallback

Pseudo:

```python
if (
    global_drift_score > cfg.colmap.max_drift
    or coverage_gap_ratio > cfg.colmap.max_gap
    or repeated_pattern_failure
    or large_parallax
):
    run_colmap(facade)
```

활용:
- intrinsics
- extrinsics
- sparse points
- bundle-adjusted poses

---

# 13. Facade Rectification

각 Facade Local Coordinate:

```text
origin
u-axis = horizontal
v-axis = vertical
normal = outward normal
```

사선 촬영을 Local Facade Plane으로 변환.

초기 목표:
- 최대 native detail 보존
- scale 임의 생성 금지

---

# 14. Final Stitch / Blend

단계:

```text
Warped Images
   ↓
Exposure Normalize
   ↓
Seam
   ↓
Multi-band Blend
```

권장 2개 출력:

```text
F001_analysis.tif
F001_visual.tif
```

`analysis`:
- Crack detail 보존
- 최소 blending

`visual`:
- 사람이 보기 좋은 exposure/seam 보정

Blending 때문에 Crack이 흐려지는지 반드시 테스트.

---

# 15. 저층 1~3층 가림 문제

대표 Occluder:
- 나무
- 수풀
- 화단
- 차량
- 표지판
- 가로등
- 사람
- 시설물

절대 규칙:

```text
보이지 않은 벽면을 생성하지 않는다.
```

---

# 16. Drone + Ground Camera Fusion

개념:

```text
Drone Image
   ↓
Occlusion Mask
   ↓
Invalid Area

Ground Camera
   ↓
LoFTR
   ↓
Perspective Rectification
   ↓
Same Facade Coordinate
   ↓
Observed pixels only
   ↓
Merge
```

두 소스 모두 가림이면:
`OCCLUDED`.

Generative Fill 금지.

---

# 17. Ground Camera 촬영

가려진 저층만 보완.

권장:
- 좌측 사선
- 정면
- 우측 사선

Ground image 처리:

```text
Camera Calibration
   ↓
Feature Matching
   ↓
Homography/Pose
   ↓
Facade Rectification
   ↓
Facade Coordinate
```

필요 시 Manual Control Point 허용.

---

# 18. Occlusion Mask

추상화:

```python
class OcclusionSegmenter:
    def infer(self, image) -> "OcclusionMask":
        ...
```

초기 class:
- VEGETATION
- VEHICLE
- PERSON
- POLE
- UNKNOWN_OCCLUDER

특정 segmentation model에 Core pipeline을 강결합하지 않는다.

---

# 19. Merge Rule

핵심:

```python
if drone_observed:
    use(drone_pixel)
elif ground_observed:
    use(ground_pixel)
else:
    mark(OCCLUDED)
```

금지:

```python
if missing:
    generative_fill()
```

---

# 20. Observation State

Coverage 상태:

```text
OBSERVED
OCCLUDED
OUT_OF_COVERAGE
INVALID
```

Crack 상태:

```text
CRACK
NO_CRACK
UNKNOWN
```

규칙:

```text
NO_CRACK => OBSERVED == true
```

---

# 21. Large Mosaic Tiling

대형 Facade 예:

```text
30000 × 50000
```

전체 resize 금지.

초기 Tile:

```yaml
tiling:
  tile_width: 1024
  tile_height: 1024
  overlap_px: 128
  skip_if_observed_ratio_below: 0.5
```

---

# 22. Crack Segmentation

Per Tile output:

```yaml
tile_result:
  tile_id:
  facade_id:
  x0:
  y0:
  width:
  height:
  crack_masks:
  confidence:
  observed_ratio:
```

Tile overlap 중복 Crack merge 필요.

---

# 23. Tile -> Global

```text
X = tile_origin_x + x_tile
Y = tile_origin_y + y_tile
```

Scale가 유효하면:

```text
(X,Y) -> (u_m, v_m)
```

---

# 24. Crack Post-processing

- morphology
- connected component
- skeletonization
- spur pruning
- component merge
- duplicate merge
- confidence aggregation

Crack entity:

```yaml
crack:
  crack_id:
  building_id:
  facade_id:
  bbox_px:
  polygon_px:
  skeleton_px:
  length_px:
  max_width_px:
  confidence:
  observation_state:
  source_image_ids:
```

---

# 25. Crack 길이/폭

길이:
- skeleton arc length 기반

폭:
- bbox width 사용 금지
- skeleton + distance transform 기반 권장

개념:

```text
width_px(p) = 2 * distance_to_boundary(p)
```

Scale 유효 시:

```text
width_mm = width_px * local_scale_mm_per_px
```

Calibration 없으면:

```text
width_mm = null
```

---

# 26. Camera Calibration

mm 단위 측정에 필요한 정보:
- sensor size
- focal length
- lens distortion
- image resolution
- camera-to-wall distance
- facade plane
- rectification scale

Scale 기준:
- Surveyed control point
- Known marker
- BIM/CAD 치수
- Known window dimension
- RTK/GCP

근거 없는 pixel-to-mm 변환 금지.

---

# 27. Crack 결과 예

```json
{
  "building_id": "B001",
  "facade_id": "F003",
  "crack_id": "C000123",
  "position": {
    "pixel_x": 12345,
    "pixel_y": 8231,
    "u_m": 14.23,
    "v_m": 35.82
  },
  "measurement": {
    "length_px": 921.3,
    "length_mm": null,
    "max_width_px": 5.2,
    "max_width_mm": null
  },
  "confidence": 0.94,
  "observation": "OBSERVED",
  "source_image_ids": ["IMG_00123", "IMG_00124"]
}
```

---

# 28. 권장 폴더 구조

```text
project/
├── config/
│   ├── building.yaml
│   ├── camera.yaml
│   └── pipeline.yaml
├── raw/
│   ├── flight_001/
│   ├── flight_002/
│   └── ground/
├── metadata/
│   ├── images.parquet
│   ├── flights.json
│   └── building_footprint.geojson
├── facades/
│   ├── F001/
│   │   ├── images/
│   │   ├── matches/
│   │   ├── warped/
│   │   ├── masks/
│   │   ├── tiles/
│   │   └── output/
│   │       ├── F001_analysis.tif
│   │       ├── F001_visual.tif
│   │       ├── F001_observed_mask.tif
│   │       ├── F001_occlusion_mask.tif
│   │       └── F001_crack_mask.tif
│   └── F002/
├── colmap/
├── crack/
├── reports/
└── logs/
```

---

# 29. Python Module 구조

```text
src/
├── capture/
│   ├── dji_metadata.py
│   └── image_catalog.py
├── building/
│   ├── footprint.py
│   ├── facade_segmenter.py
│   └── facade_classifier.py
├── matching/
│   ├── loftr_matcher.py
│   ├── sift_fallback.py
│   └── pair_selector.py
├── geometry/
│   ├── homography.py
│   ├── quality.py
│   ├── pose.py
│   └── rectification.py
├── sfm/
│   └── colmap_runner.py
├── stitching/
│   ├── warp.py
│   ├── seam.py
│   ├── blend.py
│   └── mosaic.py
├── occlusion/
│   ├── segmenter.py
│   ├── ground_registration.py
│   └── merge.py
├── crack/
│   ├── tiler.py
│   ├── detector.py
│   ├── merge_tiles.py
│   ├── skeleton.py
│   └── measurement.py
├── pipeline/
│   ├── jobs.py
│   ├── state.py
│   └── runner.py
├── report/
│   ├── json_report.py
│   └── pdf_report.py
└── common/
    ├── config.py
    ├── logging.py
    └── types.py
```

---

# 30. Core Interface

```python
class FacadeSegmenter:
    def build_segments(self, footprint):
        ...

class FacadeClassifier:
    def assign(self, image_metadata, segments):
        ...

class ImageMatcher:
    def match(self, image_a, image_b):
        ...

class GeometrySolver:
    def estimate(self, matches):
        ...

class FacadeStitcher:
    def stitch(self, facade_id, images, transforms):
        ...

class OcclusionFusion:
    def merge(self, drone_mosaic, drone_observed_mask, ground_images):
        ...

class CrackDetector:
    def infer_tile(self, tile):
        ...
```

---

# 31. DB Entity

필수:
- Building
- FacadeSegment
- Flight
- Image
- ImageFacadeRelation
- ImagePair
- MatchResult
- CameraPose
- Mosaic
- Tile
- OcclusionRegion
- Crack
- Inspection
- Report

---

# 32. Pipeline State Machine

```text
NEW
 ↓
METADATA_PARSED
 ↓
FACADE_ASSIGNED
 ↓
PAIR_GRAPH_BUILT
 ↓
MATCHED
 ↓
GEOMETRY_SOLVED
 ↓
RECTIFIED
 ↓
STITCHED
 ↓
GROUND_SUPPLEMENTED
 ↓
TILED
 ↓
CRACK_INFERRED
 ↓
MEASURED
 ↓
REPORTED
```

실패:
- FAILED_METADATA
- FAILED_ASSIGNMENT
- FAILED_MATCH
- FAILED_GEOMETRY
- FAILED_STITCH
- FAILED_AI
- NEEDS_MANUAL_REVIEW

Stage는 idempotent해야 한다.

예:

```bash
pipeline run --building B001 --facade F003 --from matched
```

---

# 33. Pipeline Config 예

```yaml
project:
  building_id: B001

capture:
  min_overlap: 0.70
  target_overlap: 0.80

facade_assignment:
  max_camera_to_target_angle_deg: 25
  minimum_score: 0.60

loftr:
  pretrained: outdoor
  min_confidence: 0.50

geometry:
  min_matches: 50
  min_inliers: 30
  min_inlier_ratio: 0.35
  max_median_reprojection_error_px: 3.0

colmap:
  enabled: true
  mode: fallback

stitch:
  generate_analysis_mosaic: true
  generate_visual_mosaic: true

tiling:
  width: 1024
  height: 1024
  overlap_px: 128

crack:
  model: OpenSistemas/YOLOv8-crack-seg
  confidence: 0.25

measurement:
  require_calibration_for_mm: true

occlusion:
  allow_generative_fill: false
```

값은 초기 PoC용이며 실제 데이터로 튜닝.

---

# 34. Parallel Processing

Facade는 독립 Job.

```text
F001 -> Worker 1
F002 -> Worker 2
F003 -> Worker 3
...
```

Job:

```yaml
job:
  job_id:
  building_id:
  facade_id:
  stage:
  status:
  gpu_required:
  priority:
```

같은 GPU에서 LoFTR + YOLO 동시 실행 시 VRAM scheduling 필요.

---

# 35. Logging

Structured log:

```json
{
  "building_id": "B001",
  "facade_id": "F003",
  "stage": "MATCH",
  "image_a": "IMG_1023",
  "image_b": "IMG_1024",
  "matches": 451,
  "inliers": 321,
  "inlier_ratio": 0.711,
  "median_reproj_px": 1.43,
  "status": "OK"
}
```

---

# 36. QA Gate

Final Facade 승인 전:

```text
[ ] Coverage ratio 만족
[ ] Unassigned image 검토
[ ] Stitch gap 검토
[ ] Drift score 정상
[ ] Occlusion mask 존재
[ ] Low-floor 보완 여부 기록
[ ] Crack inference 완료
[ ] Source provenance 존재
[ ] Calibration 여부 명확
[ ] mm 결과는 calibration 있을 때만 존재
```

---

# 37. Human Verification

최종:

```text
Drone/Ground Camera
    ↓
AI Crack Candidate
    ↓
Human Verification
    ↓
Final Record
```

사람 역할:
- false positive 확인
- 심각 Crack 현장 확인
- 완전 가림 영역 확인
- 최종 보고서 승인

---

# 38. Temporal Inspection

동일 건물 재검사:

```text
2026 Facade Map
   ↓
Crack C001
   ↓
2027 Facade Map
   ↓
Registration
   ↓
C001 Change
```

향후 저장:
- length delta
- width delta
- change %
- confidence
- matched previous crack ID

---

# 39. Evidence / Provenance

모든 Crack:

```text
Crack ID
  ↓
Facade coordinate
  ↓
Tile
  ↓
Mosaic
  ↓
Original DJI/Ground images
```

권장 provenance:

```yaml
provenance:
  original_sha256:
  model_name:
  model_version:
  git_commit:
  config_hash:
  processed_at:
```

---

# 40. 개발 단계

## Phase 1 — Single Facade Offline PoC
- 50~200장
- LoFTR
- Homography
- Warp
- `F001_analysis.tif`

## Phase 2 — Rectangular Building
- GPS/Gimbal
- Facade classification
- 4면 독립 처리

## Phase 3 — Arbitrary Shape
- Footprint
- variable Facade Segment
- 부채꼴/L자/곡면

## Phase 4 — Low Floor Fusion
- Vegetation/Occlusion
- Ground Camera
- Registration
- Observed/Occluded

## Phase 5 — Crack AI
- Tile
- YOLOv8 Crack-Seg
- Tile merge
- Global coordinate

## Phase 6 — Measurement
- Calibration
- Scale
- Length/Width

## Phase 7 — Live Video
- Pilot 2
- RTMP
- ZLMediaKit
- 자체 멀티채널 Video Viewer

## Phase 8 — Report/History
- Crack Map
- PDF/JSON
- 시계열 비교

---

# 41. Minimum PoC Success Criteria

Stitch:
- 1 Facade 자동 Stitch
- 90% 이상 촬영 영역 반영 목표
- 실패 pair/image 자동 report
- Crack detail이 과도하게 blur되지 않음

Facade:
- 직사각형 건물 방향 grouping
- 복합형 건물 F001...Fn assignment
- corner multi-assignment

Crack:
- Crack mask
- tile duplicate merge
- global coordinate
- source image tracking

Occlusion:
- vegetation을 No Crack으로 판정하지 않음
- Ground 실제 관측 pixel만 보완
- 미관측 = OCCLUDED

---

# 42. Hardware Guidance

권장:
- NVIDIA GPU
- CUDA PyTorch
- RAM 32 GB 이상
- 대형 TIFF/병렬 작업은 64 GB 이상 유리
- NVMe SSD
- 원본 보존용 대용량 Storage

RTX 4080/5090급:
- LoFTR
- YOLO Seg
- Tile 병렬 추론
테스트 가능.

---

# 43. Never Do These

1. 360° 전체 건물을 단일 Panorama로 만들지 않는다.
2. 건물을 무조건 4면으로 강제하지 않는다.
3. 곡면을 single Homography로 강제하지 않는다.
4. 모든 이미지를 N² matching하지 않는다.
5. Low-confidence Homography를 그대로 사용하지 않는다.
6. 대형 Mosaic 전체를 작은 YOLO 입력으로 축소하지 않는다.
7. 가려진 벽을 생성형 AI로 복원하지 않는다.
8. OCCLUDED를 NO_CRACK으로 처리하지 않는다.
9. Calibration 없이 mm 수치를 생성하지 않는다.
10. 원본 사진 provenance를 삭제하지 않는다.
11. Stitching용 visual mosaic만 남기고 analysis mosaic을 버리지 않는다.
12. DJI Pilot 2 RTMP 동작을 실제 장비 검증 없이 고정 가정하지 않는다.

---

# 44. 최종 목표 출력

직사각형 예:

```text
Building_B001/
├── North/
│   └── F001_analysis.tif
├── East/
│   └── F002_analysis.tif
├── South/
│   └── F003_analysis.tif
└── West/
    └── F004_analysis.tif
```

복합형 예:

```text
Building_B002/
├── North/
│   ├── F001_analysis.tif
│   └── F002_analysis.tif
├── East/
│   ├── F003_analysis.tif
│   └── F004_analysis.tif
├── South/
│   └── F005_analysis.tif
└── Other/
    └── F006_analysis.tif
```

각 Facade별:
- analysis TIFF
- visual TIFF
- observed mask
- occlusion mask
- crack mask
- crack JSON/Parquet
- source image list
- quality report

---

# 45. 최종 개발 원칙

이 프로젝트의 핵심은 단순 YOLO inference가 아니다.

핵심 기술:
1. 촬영 Mission 표준화
2. 1 Facade = 1 Flight
3. 가변 Facade Segment 생성
4. Waypoint + GPS + Gimbal + Footprint 기반 자동 이미지 분류
5. 반복 패턴이 많은 외벽에서 안정적 LoFTR matching
6. Homography drift 품질 관리
7. 필요한 경우만 COLMAP Pose refinement
8. Crack detail을 보존하는 고해상도 Stitching
9. Drone + Ground 실제 관측 이미지 Fusion
10. Observed/Occluded 상태 모델
11. Tile 기반 Crack Segmentation
12. Tile -> Facade global coordinate 복원
13. Calibration 기반 길이/폭 정량화
14. 원본 증거 provenance
15. 시계열 변화 추적
16. 복합/부채꼴/곡면 건물 지원
17. 시스템 간 상태/메타데이터/AI 결과는 DDS 기반 통신
18. 실시간 영상은 ZLMediaKit + 자체 Video Viewer 사용


Claude/Codex는 위 설계를 깨뜨리는 단순화를 임의로 하지 않는다.
특히 **4면 고정, 전체 건물 단일 Stitch, Generative Fill, Calibration 없는 mm 측정**은 구현하지 않는다.
