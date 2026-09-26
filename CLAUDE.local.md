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

### 4.2.1 실무 확보 방법 (2026-08-29 기록 — 실제 계약/현장 요청 시 참고용)

이 프로그램은 3D 모델을 만들지 않고, 한 면(예: 정면)만 촬영·저장·분석하는 것이 목적 (균열 검사가
핵심). footprint는 드론으로 "촬영"하는 게 아니라 별도로 확보하는 외곽선 좌표 데이터이며,
`src/building/footprint.py`가 읽는 형식은 오직 UTM 텍스트 파일뿐 — 드론 사진에서 자동으로
외곽선을 추출하는 기능(우선순위 4번)은 아직 구현되어 있지 않음.

확보 방법 (우선순위 1~3, 업체/관리사무소와 계약·협의 시 요청할 항목):

1. **CAD/BIM 도면**: 관리사무소/시공사에서 준공도면(평면도)을 받아 외곽선 좌표를 UTM으로 변환.
2. **공공 GIS 데이터**: 국가공간정보포털/브이월드(V-World) 등에서 건물통합정보 폴리곤을 조회해서
   UTM 좌표로 변환.
3. **수동 디지타이징**: 항공/위성 정사영상(구글어스 등)을 QGIS 같은 GIS 툴에 띄워 건물 모서리를
   직접 클릭해서 좌표를 뽑거나, 실측(RTK-GPS/토탈스테이션)으로 직접 측량.

필요한 파일 형식 (`load_footprint_utm_txt`가 읽는 그대로 — 예시, 실제 좌표 아님):
```
1 UTM
4
322150.5 4045320.2
322180.3 4045320.2
322180.3 4045295.8
322150.5 4045295.8
```
(첫 줄: 링 개수 + "UTM" 고정, 둘째 줄: 정점 개수, 이후 각 줄이 UTM easting/northing 미터 좌표.
여러 동/부속 건물이면 링을 여러 개 반복.) 실행 시 `--utm-epsg`도 그 건물 위치에 맞는 UTM 존을
지정해야 함.

**현재 상태**: 실제 CheckCrackViewer가 호출하는 `tools/stitch_folder.py`엔 `--footprint`/
`--utm-epsg` 옵션 자체가 없음 — footprint 파일이 있어도 지금은 뷰어의 "▶ 실행"으로 쓸 방법이
없음(저수준 `pipeline/runner.py`의 `run-building` CLI에만 존재). 실제 footprint 파일을 확보하기
전까지는 배관 작업을 보류하기로 함(2026-08-29 결정).

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

## 2026-08-29 세션 기록: 모자이크 미리보기 UI 버그 2건 + 방향 문제 원인 조사 + 동시 분석 개수 조정

**모자이크 미리보기 배치 버그** (`MainWindow.xaml`): `UniformGrid`는 `Columns` 값과 무관하게
Collapsed된 자식도 그리드 셀을 차지 — 보정본(visual_CM)이 없는 경우 빈 칸이 남거나(Columns=2),
있는 경우 나란히 안 놓이고 세로로 쌓이는(Columns=1) 문제가 반복됨. 가로 `StackPanel`로 교체 —
Collapsed 자식에 공간을 안 주므로 1개면 그 하나가 폭만큼만, 2개면 나란히 놓임.

**전체보기 창에서 큰 모자이크가 검게만 나오는 버그** (`Views/ImageViewerWindow.xaml.cs`): 실제
파일 조사로 확정 — `BACK_visual.tif`가 42165x36012픽셀(약 15억 픽셀, 211MB)인데 `DecodePixelWidth`
제한 없이 원본 그대로 로드하도록 되어 있어서, 이 앱의 소프트웨어 렌더링 모드가 이 크기를 그리지
못하고 검은 화면만 나옴(디코드 자체는 성공 — PixelWidth/Height는 정상 읽힘). 헤더만 먼저 가볍게
읽어(DelayCreation) 원본 폭이 8000px를 넘으면 그때만 캡을 걸도록 수정 — 캡 적용 디코드 결과를
PNG로 뽑아 실제 이미지가 정상 렌더링되는 것을 직접 확인함. 작은 이미지(예: visual_colmap.tif,
5635x4112)는 그대로 원본대로 로드됨.

**COLMAP 보정 모자이크의 대각선/기울어짐 — 원인 조사 결과 (추론 아닌 실측)**: `BACK` 촬영 건에서
실제 `crackvision_archive_manager`/COLMAP 산출물(`BACK_quality_report*.json`, `BACK_colmap_report.json`,
실제 COLMAP sparse reconstruction을 pycolmap으로 직접 로드)로 확인:
- COLMAP은 실제로 목적(카메라 간 기하학적 관계 재추정으로 정합 정확도 향상)을 달성함 — median
  reprojection error 1.977px(체인 방식) → 0.713px(COLMAP), coverage_ratio 0.281 → 0.923,
  disconnected_components 3 → 1. "화면상 방향이 기울어 보임"은 이 정합 정확도와는 별개의, 캔버스
  렌더링 단계(`facade_plane_from_reconstruction`의 e_u/e_v 축 결정)의 문제.
- 33장 중 COLMAP에 실제 등록된 건 16장(`DJI_0167`~`DJI_0183`)뿐이고, 이 16장의 3D 점군을 SVD로
  피팅하면 평면 법선이 거의 완벽히 수직(world_up과의 내적 0.984)으로 나옴 — 즉 이 점군은 벽이
  아니라 거의 평평한 지붕(옥상) 면을 나타냄. 코드는 이를 정확히 감지해 "옥상/평면뷰" 분기(e_u =
  드론 비행 방향)를 타는데, 비행 방향은 건물 방위와 무관해서 결과가 대각선으로 보임.
- DJI EXIF 짐벌 피치/요로 "옥상 사진만 자동 제외"하는 방식을 검토했으나, 실측 결과 등록된
  16장과 등록 안 된 17장의 짐벌 피치가 전부 -20°~-54°로 비슷해서 **짐벌 각도만으로는 구분이
  안 됨**(등록 안 된 DJI_0071: -40.4°, 등록된 DJI_0177: -37.4° — 거의 동일). 즉 이번 데이터는
  "섞인 사진 중 일부를 걸러내면 되는" 문제가 아니라, 촬영 자체(고도/거리/각도)가 벽보다 옥상
  위주로 이뤄진 것으로 보임 — 이 판단도 실측 기반이며, 정면 위주로 제대로 찍힌 검증용 데이터가
  아직 없어 추가 검증은 보류 상태.
- footprint(외곽선) 기반 보정이 가장 근본적인 해결책이지만(사진 내용과 무관하게 항상 올바른
  방향 산출), 이 프로그램은 3D 모델을 만들지 않고 한 면만 촬영·저장·분석하는 것이 목적이라
  footprint 인프라 구축은 보류 — 확보 방법은 4.2.1절에 기록.

**동시 분석 개수 (`GpuDetectionService.cs`)**: 기존 "GPU 탐지됨 → 5, CPU만 → 1"을 **GPU 여부와
무관하게 항상 1**로 변경 — 운용자가 RTX 4080에서 직접 확인한 결과 facade 1건만 돌려도 GPU
사용률이 이미 95% 이상이라 동시 2건 자체가 불가능함을 확인. **이 값은 RTX 4080 기준 실측치이지
영구 상한이 아님** — 향후 GPU 교체 예정이며, 교체 후 같은 방식(1건 실행 중 사용률 확인)으로
재측정 필요(관련 규칙 문서는 `AnalysisLoadBalancer` 저장소의 README "max_concurrent 결정 규칙").

## 2026-09-10~12 세션 기록: COLMAP 랙티파이 안정화 + H체인 드리프트 완화 + mm 스케일 배선 + 다중 건물 facade 식별 버그 수정

### COLMAP 랙티파이 캔버스 OOM/대각선 문제 (`src/geometry/rectification.py`)
- **캔버스 폭발 크래시**: 150장 FRONT reconstruction에서 평면이 882m x 860m(물리적으로 불가능)로
  계산돼 캔버스 할당이 ~63GB를 시도, OOM. `_robust_range`(IQR, k=3.0) 추가 — 원인 재확인 결과
  일부는 배경(하늘/원경) 포인트가 실제 벽면에서 -150m~-1500m 떨어진 곳에 삼각측량돼 섞여 들어간
  것(`_filter_points_near_plane`로 평면 거리 기준 별도 필터 추가, 210x107m → 56x37m로 정상화).
- **u축(건물 폭 방향) 비로버스트 SVD**: 카메라 center PCA(`_principal_direction`)가 단일 SVD라
  이상치 카메라 위치에 흔들려 u-extent가 실제(~60m)의 3.5배(~210m)로 부풀던 문제 — 반복 트림드
  PCA(IQR 기반 inlier만 남기고 재피팅, `_robust_range`와 동일 철학)로 교체.
- **벽/옥상 오판정**: 점군 SVD normal이 지붕 포인트에 압도돼 실제로는 벽을 찍은 비행을 옥상으로
  오판정 → `_camera_forward`(카메라 실제 촬영 방향) 기반 override 추가.
- **실행별 COLMAP 작업폴더 분리**(`268fb24`): 재실행 시 이전 reconstruction이 섞여 들어가는 문제
  방지 — trial마다 독립 폴더.
- 반복 시험(매번 `output/` 완전 삭제 후 진짜 CLI 절차로 재실행, 캐시/이전 산출물 재사용 금지)
  결과 FRONT(150장) coverage_ratio가 4회 연속 **0.96 안팎**(목표 0.95 이상 충족) — 상세 결과는
  세션 스크래치패드 `coverage_sweep_results.jsonl`.
- COLMAP 배경 크롭(`_crop_to_dense_coverage`) + 이음선 COST_COLOR_GRAD(`9da9165`)도 같은 흐름에서
  추가 — sky/mountain 배경이 최종 캔버스 여백에 남아 이음선 품질을 해치던 문제 완화.

### H체인(비-COLMAP) 드리프트 완화 (`src/stitching/graph.py`, `a32ea4c`)
같은 종류의 "이미지 수가 늘수록 결과가 나빠짐" 문제의 근본 원인 — H체인 경로(기준 이미지까지
pairwise 호모그래피를 그래프 최단경로로 곱해서 합성)는 경로(hop)가 길어질수록 개별 매칭 오차가
곱셈으로 누적됨. 두 가지 **서로 다른 실패 모드**를 각각 다른 방식으로 완화:
1. **반복 패턴 오탐 매칭**(`detect_inconsistent_edges`): 특정 엣지 하나가 그럴듯하지만 완전히
   틀린 경우(같은 모양 창문/층을 다른 층과 매칭) — inlier_ratio 자체는 정상이라 가중치 기반
   컷오프로는 못 잡음. 공통 이웃과의 삼각형 호모그래피 합성 결과가 직접 측정값과 크게 어긋나면
   그 엣지를 그래프에서 제거. 임계치 실측 튜닝: 40px는 너무 공격적(82% 제거, 그래프 36개
   컴포넌트로 분절, 55장 unreachable) → 500px/2-witness로 재조정(669개 제거, unreachable 0,
   mean drift 998→163px, max 1,000,219→4,761px).
2. **점진적 다중 hop 누적**(`refine_homographies_globally`): 위 필터를 거친 뒤에도 개별로는
   문제없어 보이는 엣지 2~3개가 누적되면 여전히 크게 벗어남 — motion averaging(scipy
   least_squares, TRF + x_scale="jac"/tr_solver="lsmr", 기준 노드는 identity로 gauge 고정)으로
   살아남은 모든 엣지를 동시에 만족시키는 방향으로 재보정. FRONT 실측: mean 163→129px, max
   4761→3041px — **부분 개선이며 완전 해결 아님**(정직하게 문서화, 과장 금지).
- `mosaic.py`에 실제 배선 완료(`stitch_facade`가 `build_stitch_graph` 직후 불일치 엣지 제거 →
  `pick_reference` → `compute_global_homographies` → `refine_homographies_globally` 순서로 호출),
  프로덕션 코드 경로에서 실제로 동작 확인.
- BACK(121장, 고층 아파트라 반복 패턴이 FRONT보다 훨씬 많음)에서도 동일 증상(31→59→95장 진행될수록
  하늘/지면 영역이 고스팅처럼 뿌예짐) 재현 확인 — 위 완화 장치가 이미 적용된 상태에서도 여전히
  드리프트가 크면 `should_run_colmap` 게이트가 COLMAP 폴백으로 전환하므로, 최종 산출물은
  `_visual_colmap.tif`/`_analysis_colmap.tif` 기준으로 판단해야 함(H체인 미리보기만으로 "실패"
  단정 금지).

### mm 스케일 배선 + 보고서 표시 (`d955b0f`)
- 운영자 결정(2026-09-11): **정밀 RTK 없이 GPS 기반 COLMAP align_reconstruction_to_utm 스케일을
  일단 채택**(9번/26번 원칙의 "calibrated 없이 mm 없다"는 그대로 유지하되, calibrated=True +
  `reference_object_type="gps_colmap_alignment"`로 명시적으로 낮은 정밀도임을 표시) — 향후 RTK-GCP
  등 실제 기준으로 교체 가능하도록 `{facade_id}_scale_colmap.json`에 출처를 남김.
  `tools/detect_cracks_folder.py`가 이 파일이 있으면 읽어서 `ScaleInfo`에 반영, 없으면 기존대로
  `calibrated=False`(px만).
- `report.html`/`pdf_report.py`: 길이/최대폭/면적을 calibrated면 `mm (px)`, 아니면 px만 표시하도록
  Jinja2 템플릿 + `_crack_metrics` 양쪽 다 수정.
- `CrackReviewItem`/`OriginalCrackViewerWindow`: 원본 보기 창 툴바에 `길이 {mm} · 최대폭 {mm} · 면적 {mm}`
  표시 추가(`7a9d7a6`).

### 결과 비교 화면: 스티칭 클릭 → 원본 사진 이동 (`e5c3db8`, `25a94e3`)
우측(Panel2) 스티칭 패널 클릭 시 좌측(Panel1)을 "원본" 모드로 전환하고 클릭 지점을 뷰포트
중앙으로 스크롤(seam owner map 기반 역산, `ResultsCompareViewModel.JumpToOriginalImageAt`). H체인
전용 미리보기 카드(COLMAP 폴백 시 의미 없어지는)는 그 상황에서 숨김 처리.

### 레이아웃: Crack Segmentation/검사 보고서를 모자이크 미리보기 오른쪽 열로 (`7840328`)
`MainWindow.xaml`을 단일 열 스택에서 `Grid`(왼쪽 `Auto`=모자이크, 오른쪽 `*`=Crack Segmentation+
검사 보고서)로 재구성 — 모자이크 Border가 `MaxHeight=260`+`HorizontalAlignment=Left`라 균등 `*`
분할로는 실제 렌더 폭이 줄어드는 레이아웃 버그를 겪은 뒤 `Auto`로 확정.

### 다른 건물의 동일 이름 facade 식별 충돌 버그 수정 (`296d00e`, 사용자 실사용 중 발견)
`D:\ClaudePr\UE_TemImg\TestApt\TestBuilding\BACK`을 한 번도 실행한 적 없는데 결과가 이미 채워져
있는 걸 사용자가 발견 → 근본 원인(추측 아니라 코드 확인): `FacadeId`("BACK"/"FRONT" 같은 방위
이름)는 단지/동이 다르면 얼마든지 재사용되는 이름인데, `MainViewModel.GetOrCreateFacade`와
`FacadeOutputScanner.ScanAll`의 `seenFacadeIds`, `ResultsCompareViewModel.Rescan`의 매칭/dedup
키로 전부 이 bare 문자열을 쓰고 있었음 — 서로 다른 건물의 "BACK"이 하나의 화면 객체를 공유하거나
(값 덮어쓰기) 두 번째 건물의 facade가 스캔에서 통째로 누락됨. 전부 `FacadeHierarchyStore.KeyFor
(sourceFolderPath, facadeId)` 합성 키(`FacadeItemViewModel.Key`/`FacadeSnapshot.Key`, 신규 추가)
기준으로 통일해서 수정 — 6개 `GetOrCreateFacade` 호출부 전부 sourceFolderPath 배선, 로그 tailer만
예외(라인에 폴더 경로 정보가 없어 `ResolveRunningFacade`로 IsRunning 기준 상관관계를 대신 사용).
"수백/수천 단지 규모까지 완벽하게 구별돼야 함"이 명시적 요구사항 — 단순 표시 버그가 아니라
데이터 무결성 버그였음.

## 2026-09-12 세션 기록: 균열 개별 geometry를 MngData PostgreSQL에 적재 (crackvision_cracks)

### 배경 (MngData 쪽에서 파악한 현황)
`crackvision_archives.facade_analysis_results`(JSONB)는 facade별 zip/report 경로 + 상태만
기록했지, `{facade_id}_cracks.json`이 이미 담고 있는 균열 개별 bbox/폭/좌표는 PostgreSQL 어디에도
저장되지 않고 있었음(MngData 쪽 `crackvision_archive_manager`/`facade_archives.sql` 확인으로
직접 확정). 이번 세션에서 그 저장 경로를 새로 만듦.

### 구현 — zip을 다시 풀 필요 없음
당초 "MngData가 받은 결과 zip을 파싱"하는 방식을 검토했으나, 실제 코드 확인 결과 더 간단한 지점이
있었음: `MainViewModel.WriteBackAnalysisResultsAsync`가 `outputDir`(로컬 스티칭 결과 폴더)를 zip으로
묶어 SFTP 업로드하면서 `CrackVisionArchiveQueryService.UpdateAnalysisResultAsync`로 경로만 직접
Postgres에 write-back하는데, **그 zip으로 묶기 전 시점에 `{facade_id}_cracks.json`이 이미
`outputDir`에 그대로 있음** — 압축 해제 없이 바로 읽으면 됨.

- **`Services/CrackVisionArchiveQueryService.cs`**: `UpsertFacadeCracksAsync()` 신규.
  `{facade_id}_cracks.json`(+ `{facade_id}_scale_colmap.json`, 있으면) 읽어서 `crackvision_facades`
  upsert(`ON CONFLICT (archive_id, facade_id)`) → 그 facade_row_id의 `crackvision_cracks`/
  `crackvision_crack_sources`를 delete+insert(한 트랜잭션, run-to-run 비교는 별도 기능이라 여기서
  diff 안 함). `UpdateAnalysisResultAsync`와 동일한 direct-Npgsql 패턴 그대로 재사용.
  `position.u_m/v_m`은 Python 쪽(`tools/detect_cracks_folder.py`)이 항상 null로 쓰는 값이라
  여기서 `px_per_m`으로 직접 계산해서 채움. mosaic_width_px/height_px는 WPF `BitmapDecoder`
  헤더만 읽어서(`DelayCreation`, 전체 디코드 안 함) 채움 — 실패 시 조용히 null.
  `facade_id`의 선행 토큰("FRONT_0" → direction="FRONT", sub_index=0)으로 direction/sub_index 도출.
- **`ViewModels/MainViewModel.cs`**: `WriteBackAnalysisResultsAsync`의 `UpdateAnalysisResultAsync`
  호출 바로 다음에 `UpsertFacadeCracksAsync` 호출 추가.
- **MngData 쪽 신규 스키마**: `backend_core/schemas/crackvision_cracks.sql` (이 저장소가 아니라
  MngData 저장소, commit `3e2c6e7`) — `crackvision_facades`/`crackvision_cracks`/
  `crackvision_crack_sources` 3개 테이블. `crack_links`(비교분석용)는 이번엔 제외, 다음 iteration.

### 사용법
1. **최초 1회, DB에 스키마 적용** (MngData 쪽, 자동 적용 안 됨 — `facade_images.sql`/
   `facade_archives.sql`과 동일한 컨벤션):
   ```
   psql <conninfo> -f Z:\DDS_Platform\MngData\backend_core\schemas\crackvision_cracks.sql
   ```
2. **그 뒤로는 아무것도 더 할 필요 없음** — 원격(CrackVisionDB) 경로로 등록된 facade에서
   "보고서 생성"(`GenerateReportCommand`)이 성공할 때마다 자동으로 적재됨. 조건:
   - `facade.ArchiveId`가 있어야 함(순수 로컬 Browse로 추가한 폴더는 archive_id가 없어서 write-back
     자체가 스킵됨 — 기존 `UpdateAnalysisResultAsync`와 동일한 게이트).
   - `{facade_id}_cracks.json`이 존재해야 함(크랙검사를 아예 안 돌린 facade는 자동으로 no-op,
     에러 아님).
3. **확인 방법**: `SELECT * FROM crackvision_facades WHERE archive_id = <해당 archive_id>;` 로
   facade_row_id 확인 후 `SELECT * FROM crackvision_cracks WHERE facade_row_id = <위 값>;`.
   같은 facade를 재분석하면 이전 crack 행이 전부 지워지고 최신 결과로 통째로 교체됨(같은
   crack_id는 `src/crack/merge_tiles.py`가 재부여하므로 값 자체는 안정적으로 유지).
4. **스케일 미보정 facade**: `scale_calibrated=false`인 facade는 `length_mm`/`width_mm`/`area_mm2`/
   `position_u_m`/`position_v_m`이 전부 NULL로 들어감 — px 값(`length_px` 등)은 항상 채워짐.

### 빌드 확인
`dotnet build` 컴파일 에러 0개 (같은 날 실행 중이던 `CheckCrackViewer.exe`가 파일을 잠그고 있어서
최종 exe 복사만 실패 — 앱 재시작 후 재빌드하면 해결, 소스 자체는 확정).

### 커밋/푸시 완료
- **CheckCrackV2** `a63c98e` → origin/main (`CrackVisionArchiveQueryService.cs`, `MainViewModel.cs`
  2개 파일만 — 같은 시점에 미커밋 상태였던 `OriginalCrackViewerWindow.xaml*` 변경분은 이 작업과
  무관해서 건드리지 않음)
- **MngData** `3e2c6e7` → origin/main (`backend_core/schemas/crackvision_cracks.sql` 신규)

## 2026-09-12 세션 기록: 원본 보기 창 -- 크랙 선택 지점 타겟 마커 추가 (`3241494`)

균열 검토 모드에서 스티칭 캔버스의 크랙(번호 배지/폴리곤 클릭) 또는 균열 목록 항목을 선택하면
"원본 보기" 창(`OriginalCrackViewerWindow`)이 항상 그 크랙의 bbox를 화면 중앙에 프레이밍하고
사각형(`CrackBboxOverlay`)으로 표시하는데, 사용자 피드백: "선택한 곳이 즉시 눈에 들어 오지 않음"
— bbox 사각형만으로는 크랙 영역 전체를 짚어줄 뿐 정확히 어디를 봐야 하는지 한눈에 안 들어옴.
bbox 중심(=`CenterOnCrack`이 뷰포트 중앙에 놓는 바로 그 지점)에 **줌/팬과 무관하게 항상 같은
화면 크기**를 유지하는 타겟 마커(흰 테두리 바깥 원 + 밝은 노랑 안쪽 원, 건물 외벽의 흔한
회색/베이지/빨강 계열과 안 부딪히는 배색)를 추가로 겹쳐 그림 — `ApplyTransform`(줌/전환 시)과
`Canvas_MouseMove`(드래그 팬 중) 양쪽 모두에서 위치 갱신.

**커밋/푸시**: CheckCrackV2 `3241494` → origin/main.

## 2026-09-12 세션 기록: BACK 모서리/옥상 결함 실측 진단 → COLMAP 2단계 자동 재실행 기능 → 크랙 검출 아키텍처 전면 재설계(설계 확정, 구현 착수)

### 배경 -- BACK 모서리 지그재그 결함의 실제 원인
사용자가 BACK_analysis_colmap 재실행 결과에서 한쪽 모서리(사용자 기준 왼쪽/프로그램 기준
오른쪽)만 세로선이 지그재그로 어긋난다고 지적. 실측(coverage_count + 컬럼당 distinct
image count)으로 확인: 정상 모서리는 평균 coverage 13.83/컬럼당 평균 36.65장(최소
22장)인데 문제 모서리는 평균 7.71/평균 21.20장, **최소 1장**(다른 사진과 대조할 방법이
없는 구간)이었음 -- 코드 버그가 아니라 그쪽 모서리의 **실제 촬영 커버리지 부족**(추가
촬영이 필요한 데이터 문제)으로 확정.

### COLMAP 2단계 자동 재실행 기능 (`src/pipeline/runner.py`)
같은 세션에서 "흔들린/블러 이미지 제외" 요청 → 실제 데이터로 검증한 결과 Laplacian
variance(블러 지표)는 "하늘 위주 단순 구도"와 "진짜 흔들림"을 구분 못 함(가장 낮은 점수
사진들을 직접 열어보니 전부 흔들림이 아니라 하늘/산 위주 구도였음)을 확인하고 폐기.
대신 **이미 실행되는 COLMAP 재구성 자체의 3D 포인트**를 신호로 씀 -- 어떤 사진이 벽
평면(facade plane) 근처에 3D 포인트를 거의 못 만드는지(`_detect_off_wall_images`,
plane_distance_m=3.0 이내 포인트 개수) 확인하면, coverage-count나
connected-components나 이미지별 호모그래피 국소 스케일보다 훨씬 정확하게 "벽면이 거의
안 찍힌 사진"(지붕 장비/하늘 위주)을 잡아낼 수 있음을 실측으로 확인(BACK 121장 중 53장,
on_wall count가 0~248 vs 나머지 775~10,030 -- 3배 이상 자연스러운 간격).
`min_gap_ratio`(자연스러운 배수 간격 기준, 처음 4.0으로 잡았다가 실제 3.12배 간격을
놓쳐서 2.5로 재보정), `min_island_area_px`류의 안전장치와 같은 "실측 기반 임계치, 추측
아님" 원칙 적용. `_run_colmap_and_rectify_once`로 COLMAP mapping+rectify 한 세트를
함수화하고, 1단계(전체 이미지) 결과에서 자동 감지된 이미지를 뺀 뒤 2단계로 재실행,
성공하면 2단계 결과를 채택. BACK 실측: 121→53장 자동 제외→68장, coverage_ratio
0.931→0.956, 옥상 오탐 아티팩트 육안 확인 감소. 커밋 `e28a23a`.

**부수 발견/수정**: `facade_plane_from_reconstruction`의 e_u축(SVD 기반)은 부호가
정해지지 않아 **같은 이미지셋의 서로 다른 COLMAP 실행(재현성 없음)마다 좌우가 뒤집힐 수
있음**을 실제로 확인(V002 vs V003 렌더링이 거울상). DJI 파일명(촬영 순서, 실행마다
안정적)과 카메라 위치의 공분산 부호로 e_u를 결정론적으로 고정 -- V002/V003 둘 다 이제
같은 방향(e_u≈[-0.93,-0.34,0.13])으로 수렴함을 실측 확인. 같은 커밋에 포함.

### 파이프라인 순서 재구조화 논의 -- CLAUDE.local.md #10 명시적 오버라이드 (설계만 확정, 미구현)
사용자가 "COLMAP을 1단계로 먼저 돌려서 자동 감지용으로 쓰고, 그 다음에 이미지를
추출/필터링한 뒤 LoFTR+스티칭을 하자"고 제안 -- 검토 결과 이건 원칙 #10("COLMAP은 항상
필수가 아니다, H체인 품질 부족시에만 폴백")을 **명시적으로 뒤집는** 변경(COLMAP이 이제
모든 facade에서 무조건 최소 한 번 먼저 실행됨)임을 짚어드렸고, 사용자가 "시간은 상관없다,
정확성이 중요하다"며 확정. 확정된 3가지 세부사항:
1. 1단계는 COLMAP mapping만(rectify_and_blend 생략 -- 자동 감지엔 재구성+평면만 있으면
   충분하고 이 결과는 최종 산출물로 안 쓰이므로 무거운 렌더링이 낭비).
2. 2단계 COLMAP은 H체인의 `needs_colmap_fallback` 판정과 무관하게 **항상** 실행하고,
   H체인 결과(`_analysis.tif`)와 COLMAP 결과(`_analysis_colmap.tif`) **둘 다 저장**.
3. 2단계는 1단계 재구성을 재활용하지 않고 **완전히 새로 COLMAP을 처음부터 재실행**
   (1단계는 나쁜 이미지가 섞인 채 번들조정된 재구성이라 남은 이미지 포즈에도 미세한
   영향이 남아있을 수 있어, 정확성 우선 원칙상 새로 도는 쪽이 맞음).

**안전장치(사용자 승인)**: 1단계(필터 전 전체) coverage/observed_mask와 2단계(필터 후)
coverage/observed_mask를 비교해서, 필터링 후 coverage가 줄어드는 영역이 있으면 경고(또는
그 영역만 재확인) -- "이번엔 우연히 안전했다"(BACK은 coverage가 오히려 늘어나서 실측으로
안전 확인됨)를 "항상 안전하다"로 착각하지 않기 위한 장치. 아직 미구현.

### 크랙 검출 아키텍처 전면 재설계 (설계 확정, 구현 착수 -- 가장 큰 변경)
사용자가 크랙 검출이 "원본 개별 사진에서 진행"되는 것으로 원래(v2 시점) 의도했다고 확인 --
실제 코드(`tools/detect_cracks_folder.py`)를 재확인한 결과 지금 구현은 **완성된 스티칭
모자이크**(`_analysis.tif`/`_analysis_colmap.tif`)를 타일링해서 검사하는 방식이고, 이건
CLAUDE.local.md 자체의 기존 스펙(#21 "Large Mosaic Tiling", #23 "Tile -> Global")과
정확히 일치함 -- 즉 버그가 아니라 v1 스펙 그대로 구현된 것. 사용자가 "v1과 v2 사이에
방향 차이가 있었고 문서에 반영이 안 됐다"고 확인, 아래 새 아키텍처로 확정.

**동기 1**: 자동 이미지 필터링(위 121→68) 시, 검출이 최종 모자이크 기준이면 제외된
이미지에만 있던 크랙 정보가 손실될 수 있음 -- 실측으로는 이번 BACK 케이스가 안전했지만
(coverage가 줄지 않음) "항상 안전"은 보장 안 됨.

**동기 2**: 모자이크는 각 원본을 평면에 투영+블렌딩한 결과라 seam/blend 아티팩트(이번
세션에서 겪은 disagreement-island, 재발코니 왜곡 등)가 크랙 검출을 오염시킬 수 있음 --
원본(비왜곡) 사진에서 직접 검출하면 이 문제 자체가 없음.

**확정된 새 순서**:
1. 1단계 COLMAP(전체 이미지, mapping만) -- 위 재구조화와 공유. 여기서 얻는
   재구성/포즈가 필터링으로 제외된 이미지에도 유효한 원본→평면 호모그래피를 제공하므로,
   제외된 이미지의 크랙도 (검출만 한다면) 캔버스 좌표에 위치시킬 수 있음.
2. 자동 감지 → 필터링(121→68 예시).
3. **필터링된 68장 각각에서 독립적으로 크랙 검출**(다운사이즈 절대 금지, 기존 모자이크
   타일링 설정(`tiling.tile_width/height/overlap_px`)을 원본 사진 각각에 그대로 적용) --
   스티칭과 무관하게 미리/독립적으로 실행 가능. (필터링 전 121장이 아니라 68장만 검사하기로
   확정 -- 근거: 자동 감지가 애초에 on-wall 포인트가 거의 0인 사진만 골라서 빼므로,
   heavy overlap 상 남은 68장이 그 벽면을 이미 커버하고 있을 개연성이 매우 높고, 실측으로도
   이번 케이스에서 coverage가 줄지 않아 안전이 확인됨 -- 위 안전장치가 이 잔여 리스크의
   최종 방어선.)
   - **1차 병합(원본 사진 내부 타일 겹침)**: 기존 `merge_tiles.py` 그대로 재사용.
4. LoFTR+H체인 스티칭 → `_analysis.tif`/`_visual.tif` 저장(기존과 동일, 크랙 검출과 무관).
5. 2단계 COLMAP(필터된 이미지로 완전히 새로 실행) 완료 대기.
6. 2단계의 최종 호모그래피로 각 원본의 크랙 폴리곤을 **캔버스 좌표로 변환** -- 이건
   위치(어디 있는지)와 서로 다른 원본 간 병합(매칭)에만 사용.
7. **길이/폭 실측값은 캔버스 좌표가 아니라 원본 사진 자체의 픽셀 해상도 + 그 크랙 위치의
   로컬 스케일**(호모그래피 기반 mm-per-raw-pixel, 이 세션에서 다른 목적으로 썼던 국소
   스케일 계산과 동일한 개념)로 측정 -- 캔버스의 전역 px_per_m(Phase 1 기준 100px/m=
   1cm/px)은 드론 원본 사진의 실제 해상도(보통 mm 단위 GSD)보다 거칠어서, 캔버스에서
   측정하면 원본의 정밀도를 잃는다는 게 핵심 이유(폭처럼 mm 단위로 미세한 값일수록
   치명적). 위치 변환과 실측 측정이 서로 다른 좌표계를 쓰는 구조.
8. **2차 병합(서로 다른 원본 사진 간, 같은 물리적 크랙)**: 캔버스 좌표로 변환된 폴리곤들을
   `merge_tiles.py`와 **동일한 IoU 임계치**로 매칭(사용자 확정 -- 새 임계치 도입 안 함).
   매칭된 중복 크랙들의 측정값(길이/폭)은 **평균**, 신뢰도(confidence)와 크랙 ID 안정성
   매칭(`match_crack_ids`)은 **기존 로직 그대로 유지**.
9. `source_observations` 필드(어느 원본 사진에서 이 크랙이 보이는지)는 스키마는 그대로
   유지되지만, **채워지는 방식이 역방향(캔버스에서 찾은 크랙을 원본으로 역추적)에서
   정방향(원본별 검출 결과를 캔버스에서 병합한 결과 그 자체)으로 전환**됨.

**상태 (2026-09-12 세션 종료 시점)**: 설계 전체 확정, 사용자가 "1(문서화)->2(구현)"
순서로 진행 지시. 이 문서화 다음 실제 구현 착수 예정 -- 아직 코드 변경 없음(위
COLMAP 2단계 자동 재실행 기능 제외, 그건 이미 커밋됨). 구현 범위가 크므로(1단계
mapping-only 분리, 원본별 크랙 검출 신규 파이프라인, 2차 병합 신규 로직, 로컬 스케일
측정 로직 등) 여러 단계로 나눠 진행 필요.

## 2026-09-24 세션 기록: hloc(SuperPoint) 매칭 선택 옵션 + dense-stereo hybrid 프로덕션 배선 + 구조물 유형(댐/아파트/공장) 태그 seam

### hloc(SuperPoint+LightGlue)을 선택 가능한 매칭 백엔드로 추가
사용자 명시 요청("LoFTR 적용, hloc(SuperPoint) 적용 설정 창에 선택 콤보 박스 추가
하삼 : 라이선스 문제 고려하지 않음") -- LoFTR을 표준으로 채택한 근거(Magic Leap
비상업 라이선스 회피 + 이 건물 데이터 실측상 동등/우수한 품질)는 그대로 CLAUDE.local.md에
남지만, 사용자가 비교 목적으로 hloc도 실제로 돌려볼 수 있게 명시적으로 라이선스
고려를 배제하고 진행.

- **`tools/hloc_worker.py`**(신규): hloc/SuperPoint/LightGlue는 이 프로젝트 메인 env가
  아니라 별도 env(`D:/ClaudePr/CheckCrack3D/nerfstudio/hloc_env/python.exe`, gsplat3d
  탐색 작업 때 만들어둔 것)에만 설치돼 있음 -- 이 워커는 그 env에서 subprocess로
  실행되어 `hloc.extract_features`/`match_features`만 수행하고 결과를 h5py→plain
  `.npy`로 export. pycolmap은 이 워커 안에서 아예 import하지 않음(별도 env의 pycolmap
  버전과 메인 env COLMAP 재구성 버전이 섞일 위험 자체를 구조적으로 제거 -- 이번
  세션 dense-stereo 작업에서 실제로 겪었던 pycolmap 버전 불일치 문제의 재발 방지).
- **`src/matching/hloc_bridge.py`**(신규, `loftr_colmap_bridge.py`의 hloc 대응):
  `match_database_with_hloc()` -- pair_selector로 후보 페어 선정 → hloc_worker.py를
  subprocess로 호출 → export된 keypoints/matches를 COLMAP database에 직접 write
  (`db.write_keypoints`/`write_matches`) → `pycolmap.verify_matches`. LoFTR 브릿지와
  달리 SuperPoint는 이미지당 고정 keypoint 집합이라 LoFTR 브릿지가 필요로 했던
  radius-merge consolidation이 아예 불필요(더 단순한 구조).
- **`src/sfm/colmap_runner.py`**: `run_colmap()`에 `matcher_backend: str | None` 파라미터
  추가. 해석 순서: 명시 인자 > `cfg.colmap.matcher_backend` > 레거시
  `cfg.colmap.use_loftr_matching` 불리언 > `"sift"`. `"hloc"`/`"loftr"` 선택 시 필요한
  인자(catalog/cfg/matcher)가 없으면 SIFT로 안전하게 대체하고 경고 로그(크래시 대신).
- **`src/common/config.py`**: `Config.set_override(section, key, value)` 추가 -- 매
  `load_config()` 호출이 독립된 dict를 반환하므로 이 한 인스턴스만 안전하게 덮어씀
  (다른 실행 중인 facade의 cfg에 영향 없음). CLI 인자 기반 1회성 오버라이드 전용,
  범용 mutation API 아님.
- **`src/pipeline/runner.py`**: `run_facade_poc()`에 `matcher_backend`/`structure_type`
  파라미터 추가. `matcher_backend`는 `cfg.set_override("colmap", "matcher_backend", ...)`로
  주입 -- `_run_facade_pipeline` 내부 호출 체인(COLMAP stage1/stage2 등 여러 지점)을
  전혀 건드리지 않고 cfg 하나만 통해 전파(이미 모든 내부 함수가 cfg를 받고 있었기
  때문에 이 방식이 가장 침습이 적음, 특히 현재 실행 중이던 FRONT facade 파이프라인을
  건드리지 않기 위해 의도적으로 선택).
- **`tools/stitch_folder.py`**: `--matcher-backend`/`--structure-type` CLI 인자 추가,
  둘 다 생략 시 기존 동작과 100% 동일(config의 use_loftr_matching 그대로, structure_type
  기본 APARTMENT).
- **Viewer**: 설정 화면(계정/연결 탭, "환경 설정" 카드) 안에 매칭 알고리즘 콤보박스
  추가(LoFTR/hloc) -- `PipelineSettingsStore`(RootPath/pipeline_settings.json, 신규)에
  즉시 영속화, `MainViewModel.RunFacade`가 매 실행마다 이 값을 읽어 subprocess 인자로
  전달.

### 구조물 유형(댐/아파트/공장) 태그 -- 오늘은 provenance만, 실제 분기는 보류
사용자 요청("단지 종합보고서 옆에 콤보박스로 DAM/아파트/공장 선택") 배경을
AskUserQuestion으로 확인: 구조 유형에 따라 평면/기하 가정과 크랙 판정 기준이
실제로 달라져야 한다는 데는 동의하지만, "지금은 아파트로직 완료 후 차후 DAM 할
경우 로직 건드리고 싶지 않음, 추후 DAM 이미지 확보하여 시험하면서 알고리즘 보정"
-- 즉 오늘은 seam(확장 지점)만 만들고 APARTMENT 외에는 아무 실제 값도 바꾸지
않기로 확정.

- **`src/common/structure_profiles.py`**(신규): `get_structure_profile(structure_type)`
  -- `{"structure_type": "APARTMENT"|"DAM"|"FACTORY", "calibrated": bool}`. `calibrated`는
  APARTMENT만 True. DAM/FACTORY는 오늘 APARTMENT와 완전히 동일한 가정으로 처리되고
  `calibrated=False`로 명시적으로 표시됨(9번/26번 원칙의 "보정 여부를 절대 암묵적으로
  가정하지 않는다"는 정신을 여기도 그대로 적용).
  `run_facade_poc`가 매 실행마다 `{facade_id}_structure_type.json` 사이드카로 기록
  (`{facade_id}_scale_colmap.json`과 동일한 컨벤션), `tools/detect_cracks_folder.py`가
  이를 읽어 각 크랙 JSON 항목에 `structure_type`/`structure_type_calibrated` 필드로
  기록(CrackResultModel이 안 읽는 추가 provenance 필드 -- severity/building_id와
  동일한 전례).
- **Viewer**: `단지 종합보고서` 버튼 옆에 콤보박스 추가(아파트/댐/공장). `ComplexNode`는
  트리 재구성 때마다 새로 만들어지는 트랜지언트 객체라(FacadeTreeNodes.cs의
  BuildingNode 문서 참고), 실제 영속화는 신규 `ComplexSettingsStore`
  (RootPath/complex_settings.json, ComplexId 키)가 담당 -- 콤보박스 선택 즉시
  `ComplexNode.OnStructureTypeCommitted` 콜백으로 저장되고, `MainViewModel.RunFacade`가
  실행 시점에 이 값을 읽어 `--structure-type`으로 전달.

### 검증
- Python: 신규/수정 파일 전부 `py_compile` 통과 + 실제 import(`hloc_bridge`,
  `structure_profiles`, `colmap_runner.run_colmap`, `runner.run_facade_poc`의 새
  시그니처) 확인. `Config.set_override` 동작 직접 테스트(재현: override 후
  `cfg.colmap.matcher_backend`가 정확히 반영되고 기존 키는 그대로 유지됨).
- C#: `dotnet build` -- 코드 자체는 컴파일 에러 0개(처음 시도 때 MainWindow.xaml/
  SettingsView.xaml 새 주석에 `--`가 들어가 MC3000 에러 2건 -- 이전 세션과 동일한
  종류의 실수, 바로 수정). 최종 실행 파일 복사 단계만 실패(다른 프로세스로 실행 중인
  CheckCrackViewer.exe가 파일을 잠금 -- 2026-09-12 세션에 이미 겪은 것과 동일한
  성격의 무해한 실패, 재빌드 전 앱을 닫으면 해결).
- **실제 CheckCrackViewer 재시작 후 두 콤보박스를 눌러보는 end-to-end 확인은 아직
  안 함** -- 사용자가 이미 이 세션 시작 시점에 FRONT facade를 "CM 매칭 중"으로
  실행하고 있었어서 앱을 재시작하지 않고 코드만 작성함. 다음 확인 필요: 설정
  화면에서 hloc 선택 후 실제로 facade 하나 돌려서 hloc_worker.py subprocess가
  살아서 끝까지 도는지(다른 env 경로/hloc 버전 문제 있을 수 있음, 지금까지는 import
  가능 여부만 확인했고 실제 extract_features/match_features 풀 실행은 안 해봄).

### 실사용 중 발견된 별개 버그: `run_dense_stereo`의 `stereo_fusion` 크래시 (수정 완료)
사용자가 방금 완료된 FRONT V004 실행 결과를 확인하다가 `_analysis_colmap_dense.tif`가
없는 것을 발견 -- `logs/pipeline.log` 확인 결과 `DENSE_STEREO_FAILED`:
```
[reconstruction.cc:1047] Check failed: colmap::ExistsDir(path_val)
Directory ".../colmap_dense/dense/fused.ply" does not exist.
```
원인(`pycolmap.stereo_fusion` 실제 시그니처를 `help()`로 직접 확인해 특정): `output_type`
파라미터를 안 넘기면 기본값이 `"bin"`(COLMAP 바이너리 재구성 "폴더" 포맷)이라, `output_path`로
준 `.ply` 파일 경로를 디렉터리로 취급하려다 `ExistsDir` 체크에서 항상 실패 -- 정작
`patch_match_stereo`(depth map 계산, FRONT 276장 기준 3.8시간 소요, `dense/stereo/depth_maps/`에
552개 파일로 이미 성공 완료 확인)는 멀쩡했는데 마지막 한 줄 때문에 dense stereo 결과 전체가
날아갔던 것. **`src/geometry/dense_stereo.py`에 `output_type="PLY"` 추가로 수정.**
검증: 이미 계산된 depth map을 재사용해 수정된 옵션으로 `stereo_fusion`만 다시 실행 -- 크래시
없이 191/276장까지 정상 진행(초당 ~2.2s/이미지, point count 191549로 안정)하는 것까지 확인 후
사용자 요청으로 중단(전체 재실행은 사용자가 UI에서 직접 진행하기로 함). fused.ply 자체를
끝까지 만들어 확인하지는 못했지만, 원래 버그는 시작하자마자(첫 이미지 전에) 크래시했던 것과
달리 이번엔 실제 fusion 루프가 정상적으로 여러 이미지를 처리했으므로 수정이 유효함은 사실상
확인됨. **다음 실제 실행에서 `_analysis_colmap_dense.tif`가 생성되는지가 최종 확인.**

### Dense Stereo 무진행(black-box) 문제 -- CheckCrackViewer에 진행 로그 추가
사용자가 위 버그 수정 확인을 위해 V005를 재실행했는데, "3시간 넘게 도는데 되는지 모르겠다"고
지적 -- 실제 원인 확인: `patch_match_stereo`는 `incremental_mapping`과 달리 `next_image_callback`
같은 콜백이 없는 단일 블로킹 호출이라, 이 단계(FRONT 276장 기준 3.8시간) 내내 로그가 전혀
안 찍히고 있었음(코드 확인 결과 `run_dense_stereo`가 애초에 `logger` 파라미터 자체를 안 받고
있었음 -- 처음부터 이 단계는 로깅 대상이 아니었던 것).

- **`src/geometry/dense_stereo.py`**: `run_dense_stereo`에 `logger`/`facade_id`/
  `progress_interval_s`(기본 60초) 파라미터 추가. `patch_match_stereo`가 depth map을
  `dense/stereo/depth_maps/`에 (image, geometric/photometric) 단위로 파일 하나씩
  즉시 기록한다는 사실(실측 확인 -- 진행 중에 직접 `ls`로 303/552 관찰)을 이용해,
  백그라운드 daemon thread가 60초마다 그 폴더의 파일 개수를 세서
  `DENSE_STEREO_PROGRESS`(`"진행": "303/552"` 형식, `COLMAP_MAPPING_PROGRESS`와 동일
  관례)로 로그. pycolmap C++ 호출이 GIL을 놓아준다는 전제(이 프로젝트가 이미
  `incremental_mapping`의 콜백으로 검증한 것과 같은 종류의 바인딩 관례) 위에서 동작 --
  안 놓아주더라도 poller가 그냥 스케줄 안 되고 조용히 아무것도 안 찍을 뿐, 기존 동작
  대비 회귀는 없음. `DENSE_STEREO_UNDISTORT`/`DENSE_STEREO_PATCHMATCH`/
  `DENSE_STEREO_FUSION` 단계 경계 로그도 추가.
- **`src/pipeline/runner.py`**: `run_dense_stereo(..., logger=logger, facade_id=facade_id)`로
  호출부 갱신.
- **Viewer (`MainViewModel.cs`)**: `StageLabels` 표에 위 dense stereo stage들과, 그동안
  빠져 있던 2026-09-12 COLMAP 1/2단계 재구조화 관련 stage(`COLMAP_STAGE1`,
  `OFF_WALL_DETECTED`, `COLMAP_STAGE1_FAILED`, `COLMAP_STAGE2`, `COLMAP_STAGE2_FAILED`,
  `OFF_WALL_COVERAGE_REGRESSION`, `COLMAP_ALIGNMENT_POOR`, `POSE_PRIOR_UNAVAILABLE`,
  `POSE_PRIOR_RECTIFIED`, `STRUCTURE_TYPE`, `COLMAP_LOFTR_MATCH`, `COLMAP_HLOC_MATCH`)
  전부 한글 라벨 추가 -- 표에 없어도 원본 영문 stage명이 그대로 표시되는 폴백 자체는
  이미 있었지만(코드 확인), 사용자에게 의미가 안 보였음. `DENSE_STEREO_PROGRESS`를
  진행률 접미사(`(X/Y)`) 표시 대상에도 추가(`COLMAP_MAPPING_PROGRESS`/`MATCH_GEOMETRY`와
  동일 취급).
- **중요한 한계**: 이 수정은 **다음 실행부터** 적용됨 -- 이미 실행 중이던 V005는 옛
  `dense_stereo.py`가 메모리에 로드된 채 돌고 있어서 이번 수정으로 로그가 추가되지
  않음(재시작해야 반영). V005 자체의 진행 상황은 파일시스템 폴링(10분 간격,
  ScheduleWakeup)으로 계속 보고.
- 검증: Python `py_compile` 통과, `dotnet build` 코드 컴파일 에러 0개(실행 중인
  CheckCrackViewer.exe가 파일을 잠가 exe 복사만 실패 -- 동일한 무해한 실패 패턴).

### Dense Stereo 하이브리드가 실제로는 100% flat로만 채워지던 심각한 버그 발견+수정 (align 순서)
V005 완료 후 사용자가 결과 스크린샷으로 "베란다/창문 왜곡 심하다, gsplat3d/render_dense_stereo_loftr
처럼 만들라"고 지적 → 실측 확인 결과 `FRONT_visual_colmap_dense.tif`가 `FRONT_visual_colmap.tif`
(순수 flat)와 **픽셀 단위로 0 차이** — dense stereo가 최종 결과에 전혀 기여하지 못하고 있었음.
원인: fused.ply의 228,224개 점 전부가 facade 평면에서 53~380m 떨어져 있어서(`WALL_BAND_M=3.0`
필터 통과 0개) `build_hybrid_mosaic`가 100% flat로 대체됨. 같은 재구성의 sparse points3D는
정상적으로 86%가 평면 3m 이내 — 카메라 포즈/정렬 자체는 문제 없고 dense fusion 결과만 이상.
실제로 잘 되는 `gsplat3d/finish_loftr_pipeline.py`와 비교해 유일한 구조적 차이를 특정: 그 스크립트는
**UTM align 이전(네이티브 스케일)** 재구성으로 dense stereo를 돌리고 GPS-align sim3d를 **나중에**
fused.ply 좌표에 직접 적용하는데, 프로덕션은 **align 이후** reconstruction을 그대로 dense stereo에
넘기고 있었음(내가 원래 "정렬 걱정 없다"고 자신했던 설계 가정이 틀렸음이 실측으로 확정). 수정:
`align_reconstruction_to_utm`이 `bool` 대신 `Sim3d|None` 반환하도록 변경(하위호환 유지) →
`_run_colmap_mapping_only`/`_run_colmap_and_rectify_once`가 sim3d를 같이 반환 → dense stereo
호출부가 `colmap_result.sparse_dir`에서 새로 읽은(align 안 된) reconstruction으로 `run_dense_stereo`를
돌리고, `build_hybrid_mosaic`가 `sim3d`를 받아 fused.ply raw 좌표에 직접 적용하도록 재배선.
**검증 완료(V006 재실행)**: fused 점 근접 비율 0%→97.3%, flat 대비 픽셀 차이 0%→63.7%, 육안 비교도
gsplat3d 기준본과 거의 동일 확인 -- align 순서 수정 유효함 확정.

### V006에서 발견된 두 번째 결함: 건물 실제 코너가 단일 평면에 잘못 투영됨 (수정 완료)
align 순서 수정 후 사용자가 "왼쪽 모서리 왜곡 심하다"고 재지적 → 실측 결과 색상/정렬 문제가 아니라
**건물의 실제 모따기(chamfer) 코너 패널**이 이 facade 평면에 잘못 투영된 것으로 확정(로컬 SVD 법선이
메인 평면과 44도 어긋남, 원본 DJI 사진으로 실제 코너 존재 확인). 로컬 법선 편차 기반 전역 필터는
발코니(원래도 평면과 다른 각도라 코너와 구분 안 됨)를 통째로 날려서 폐기. 최종 채택: 근접점군 자체의
u-extent 양 끝 2m만 dense stereo에서 제외(`EDGE_MARGIN_M`)하고 flat 모자이크 hole-fill로 대체 --
실제 코너는 촬영 범위 맨 끝에서만 발생 가능하다는 사실을 이용한 안전한 크롭. 실제 프로덕션
`_render_dense_canvas` 함수를 V006의 fused.ply로 직접 재검증, 쐐기 아티팩트 완전 제거 + 발코니 디테일
손실 없음 확인. `src/geometry/dense_stereo.py` 수정. 다음 실제 재실행(V007)에서 최종 확인 필요.
상세: 메모리 `dense_stereo_flat_hybrid_facade.md`의 2026-09-25 절 참고.

### render_dense_stereo_loftr 절차 대조 검토 + Track 1/Track 2 구조 정리 (사용자 요청)
사용자 그림: Track 1 = COLMAP 1단계 → 필터 → H체인 → COLMAP 2단계 → 완료 / Track 2 = COLMAP 1단계 →
필터 → render_dense_stereo_loftr 절차. 참조 스크립트(`run_loftr_reconstruction.py` →
`run_dense_stereo_loftr.sh` → `finish_loftr_pipeline.py`)와 프로덕션 코드를 단계별로 대조:
- 참조의 "LoFTR sparse reconstruction"은 프로덕션의 COLMAP 2단계 mapping과 동일(같은 LoFTR 브릿지) →
  Track 2가 별도 재구성을 또 돌릴 필요 없이 2단계 재구성을 그대로 씀(같은 결과를 20분 더 들여 재계산할 이유 없음).
- **실측으로 확정된 차이 1건 수정 -- z-buffer 방향 뒤집힘**: 참조는 법선을 카메라 쪽으로 뒤집는데
  (`if dot(cam_mean-origin, normal) < 0: normal = -normal`) 프로덕션 `_render_dense_canvas`는 안 뒤집었음.
  FRONT에서 cross(e_u,e_v) 법선은 벽 안쪽을 향함(카메라 평균 −9.3m) → "가장 큰 d가 이긴다(=카메라에 가장
  가깝다)"는 z-buffer가 실제로는 **가장 깊은 점이 이기는** 반대 동작이었음. `camera_centers_utm` 인자로
  카메라 쪽 방향 정렬 추가. 같은 데이터(V006 fused.ply)로 옛/새 방식 직접 비교 → 데이터 픽셀의 **38.6%**
  (5,778,441개) 색이 바뀜, 변화는 창틀/발코니 슬래브 모서리/난간/리세스 경계에 집중(오버레이 육안 확인).
- 코너 쐐기가 z-order 뒤집힘의 부산물이라는 가설은 **틀렸음**(z-order 고치고 마진 끈 채 재렌더 → 쐐기 그대로)
  → EDGE_MARGIN_M=2.0 크롭은 여전히 필요. 참조 결과 왼쪽 모서리엔 쐐기 없음(참조는 422장 무필터, 프로덕션은
  276장 필터 후 -- 코너를 본 사선 이미지가 필터에서 빠진 영향일 가능성, 미확인 추정).
- **Track 1 산출물 저장 시점 수정**: 기존엔 H체인 산출물(`_analysis.tif/_visual.tif/quality_report/
  failed_pairs/source_images/homographies`)이 함수 맨 끝(Dense Stereo 이후)에 저장돼서, V006에서 COLMAP
  2단계는 09:01에 끝났는데 이 파일들은 12:51에야 써짐(3h50m 공백, Dense 도중 죽으면 유실). 멱등 클로저
  `_persist_track1_outputs()`로 빼서 COLMAP 2단계 직후·Dense 시작 전에 저장 + `TRACK1_DONE` stage 로그
  (뷰어 StageLabels에 "Track 1 완료 · Dense Stereo 준비" 추가). 2단계 실패/생략 경로용으로 함수 끝에서도
  호출(이미 저장됐으면 no-op).
- 미변경(의도적): Track 1 동작(H체인 → 2단계 순서, 2단계 이미지 세트가 H체인 통과 이미지 `images.keys()`에서
  나오는 결합)은 사용자가 "진행"이라 한 그대로 둠. H체인 없이 Track 2만 단독 실행하는 스위치는 요청이 없어
  만들지 않음. 관찰: V006엔 `_analysis.tif/_visual.tif`(H체인 모자이크)가 없음 -- `_safe_blend`가 블렌딩
  실패를 조용히 None 처리하는 기존 동작(COLMAP/Dense 산출물엔 영향 없음, 원인 미확인).
- **미검증**: 위 z-order 수정과 Track 1 저장 순서는 코드 컴파일/렌더 단독 검증만 됨 -- 실제 전체 재실행(V007)
  에서 최종 확인 필요.

### Track 2 단독 실행 모드 추가: `pipeline.track: dense_only` (사용자: "COLMAP 2단계 진행 하지 마세요. Dense Stereo 이 단계를 실행" / "소스 수정 하라고")
- `config/pipeline.yaml`에 `pipeline.track` 추가, **현재 `dense_only`로 설정됨**(키가 없으면 `full` = 기존 흐름). 되돌리려면 `full`.
- `dense_only` 흐름: COLMAP 1단계(LoFTR) → 벽면 미노출 이미지 필터 → flat 보정(hole-filler) → Dense Stereo 하이브리드.
  H체인과 COLMAP 2단계는 실행하지 않음(참조 `run_loftr_reconstruction.py`→`run_dense_stereo_loftr.sh`→`finish_loftr_pipeline.py`도
  필터/2단계 재구성 없이 1회 재구성 위에서 dense를 돌렸음). `colmap.use_dense_stereo` 값과 무관하게 dense를 실행.
- 구현(`src/pipeline/runner.py`): `_restrict_reconstruction_to_images`(필터 통과 이미지만 남긴 재구성; deregister만 하면 `rec.images`에 pose 없는
  이미지가 남아 `projection_center()`가 예외라서 **저장 후 재로드**로 제거 -- 실측 확인), `_run_dense_only_track`(제한된 네이티브 재구성 +
  1단계 sim3d 재사용 → GPS 잔차 검사 → 평면 재적합 → `rectify_and_blend` → 크랙검사/뷰어용 사이드카 저장: `_colmap_report`,
  `_scale_colmap`, `_wall_region_mask_colmap`, `_source_images`, flat `_colmap` 모자이크 → dense), `_run_dense_hybrid_stage`(기존 2단계 경로의
  Dense 블록을 공용 함수로 추출, 두 트랙이 공유). 준비 단계 실패 시 H체인/2단계로 되돌아가지 않고 `DENSE_ONLY_FAILED` 로그 + 중단
  (몇 시간 쓸 작업으로 조용히 폴백하지 않기 위함). Dense 자체 실패는 기존과 같이 flat 결과 유지 + `DENSE_STEREO_FAILED` 경고.
- dense_only에서는 H체인 산출물(`_analysis.tif`/`_visual.tif`/`_quality_report.json`/`_failed_pairs.json`)이 **생성되지 않음**(정상).
- 검증(V006 실데이터, GPU/dense 제외 스텁): 필터 통과 278장으로 제한 재구성(off-wall 잔존 0), 평면 재적합+flat 보정 130초, coverage
  0.9573(2단계 0.9576과 동등), 사이드카 전부 생성, `_source_images.json` 278건. **미검증**: dense 단계를 공용 함수로 통해 실제 GPU로 돌린 것,
  전체 UI 실행(사용자가 직접 실행 예정).

### V007 실행 결과 (`pipeline.track: dense_only`, 사용자가 UI에서 직접 실행, 2026-09-25 16:43~20:59, 총 4h16m)
- 1단계 COLMAP 1,405초(422장 전부 등록) → 필터 152장 제외, 270장 사용 → flat 보정 130초(coverage 0.9574) → Dense 13,744초(3h49m, fused 24,176,198점) → 하이브리드 coverage 0.9773. DONE 정상, ERROR/FAILED 없음.
- 산출물: `*_colmap_dense.*` 전체 + `*_colmap.*` 사이드카(scale/wall_region_mask/colmap_report/source_images). H체인 산출물(`_analysis.tif`/`_visual.tif`/`_quality_report.json`)은 dense_only라 없음(정상).
- 검증: flat(`_visual_colmap.tif`) 대비 54.4% 픽셀이 다름(dense 실제 반영). 전체 육안 -- 기둥 곧음, 발코니 층별 정렬(참조 `render_dense_stereo_loftr` 수준). 왼쪽 코너 흰 쐐기 **제거됨**, 코너 기둥이 전 높이 직선.
- 관찰(미측정, 육안): EDGE_MARGIN_M(2m)이 잘라낸 경계에서 flat(사진 질감)→dense(그림 같은 질감) 세로 이음선이 보이고 발코니 슬래브 선이 그 경계에서 대략 수십 px 어긋나 보임(flat의 시차 오차 때문으로 추정). 필요시 경계 feather/마진 축소 검토.
- 진행 로그(`DENSE_STEREO_PROGRESS`)와 `TRACK1_DONE` 등 새 stage 로그 정상 동작 확인. 메모리: 파이프라인 프로세스 patch_match 중 최대 약 22.6GB(캐시 상한 부근에서 증가 둔화 후 종료 시 감소), 시스템 여유 RAM 최저 약 17GB.

### V007 결과 불합격 → `pipeline.track: reference` (render_dense_stereo_loftr 절차 그대로) 적용 (사용자: "Top 도 안맞고, 왼쪽 면도 맞지가 않고요. 검정색 구멍도 있고요. 쓸수가 없어요 ... 이것 버리고 render_dense_stereo_loftr 이것의 실행 로직을 적용")
- **V007 불합격 사유(사용자 육안)**: 위쪽(파라펫) 안 맞음, 왼쪽 면 안 맞음, 검은 구멍. 위쪽을 참조 `final_mosaic.png`와 나란히 비교 -- V007은 분홍 파라펫 패널이 비스듬히 찌그러지고
  지붕선에 검은 줄/조각이 섞임, 참조는 깨끗한 직사각형 + 수평 지붕선.
- **가설 검증(측정)**: "flat 모자이크가 `_crop_to_dense_coverage`로 크롭돼 dense와 좌표계가 어긋났다" → V007에서 측정한 결과 **크롭이 일어나지 않았음**(bbox=None, flat 캔버스 6009x3715 = 평면 캔버스). 이 가설은 **틀림**.
  (그래도 크롭된 flat과 dense를 섞으면 조용히 어긋나는 잠재 버그라서 방어 코드는 추가: `build_hybrid_mosaic`가 캔버스 불일치 시 예외, `_run_dense_hybrid_stage`가 dense 시작 전에 미리 검사해 몇 시간 낭비 방지.)
- **채택한 원인 추정(미증명, 참조와의 구조 차이 기준)**: 참조는 **이미지 필터 없이 422장 전부**를 쓰는데 우리는 152장을 제외해 270장만 썼음 -- 제외된 옥상/사선 사진이 위쪽 파라펫과 왼쪽 측면을 보던 사진일 가능성.
  참조 절차 그대로 재현하는 트랙으로 교체하는 게 사용자 지시.
- **구현**: `config/pipeline.yaml`의 `pipeline.track: reference`(현재 값). `runner.py::_run_reference_track`: (1) COLMAP 1단계(LoFTR, 전체 422장) -- **off-wall 필터/coverage 추정 생략**; (2) 전체 등록 이미지로 `rectify_images` →
  `blend_rectified_images(crop_to_coverage=False)`(신규 플래그, 평면 캔버스 좌표 그대로) → flat `_colmap` 산출물+사이드카 먼저 저장(`_write_flat_colmap_outputs`, dense_only와 공용으로 추출);
  (3) 전체 등록 이미지로 Dense Stereo(undistort 2640 → patch_match geom_consistency, **cache_size 16**(참조 CLI와 동일) → stereo_fusion) → 1단계 sim3d를 fused 점에 적용 → 근접 밴드 3m → z-buffer(법선 카메라 쪽) → **참조의 병합 기준(순검정=구멍, `np.all(dense==0) & flat_has_data`)**;
  `edge_margin_m=0.0`(가장자리 마진 없음). 의도적 등가 치환 1건: 참조는 풀해상도 `image_undistorter` 후 `rectify_images`, 여기선 `rectify_images`가 원본을 cv2.undistort(SIMPLE_RADIAL k)로 직접 처리(같은 기하, source_transforms는 원본 픽셀 공간 유지).
- 뷰어 StageLabels에 `REFERENCE_START/REFERENCE_FAILED/DENSE_CANVAS_MISMATCH` 추가. `dense_only`(V007 방식)/`full`은 코드에 남겨둠(비권장).
- 예상 시간: 이미지 422장 → patch_match 약 5시간(참조 실행 5h05m, 장당 43초) + 융합/렌더 → 총 약 6시간.
- **reference 트랙 CPU 미리보기 검증(V007 stage-1 데이터, dense는 V007의 270장짜리 fused.ply 재사용 -- 최종 결과 아님)**: 422장 전부로 flat 모자이크 158초, 전체 355초, 정상 종료, 산출물/사이드카 전부 생성(source_images 422),
  flat 캔버스 = 평면 캔버스(가드 통과), `edge_margin_m=0.0` 전달 확인. **평면 캔버스 6172x4034 (V007 필터본은 6009x3715)** -- 필터가 캔버스 자체를 위 약 3.2m/옆 약 1.6m 줄여 위쪽/왼쪽이 잘려 있었음(위쪽·왼쪽 불일치의 실측 근거).
  위쪽을 참조 final_mosaic과 나란히 비교 -- 분홍 파라펫 직사각형/수평 지붕선/굴뚝 위치까지 거의 일치, 왼쪽 코너 기둥·발코니 칸 직선(쐐기/이음선 없음), 벽 영역 순검정 픽셀 0.03%(V007 0.08%, 참조 0.00%). 실제 GPU 실행(422장 dense)은 미검증.

## 2026-09-26 세션 기록: V008 reference 트랙 결과 + 결과 보기 "클릭 지점 정합 오차" 표시

### V008 (pipeline.track: reference, FRONT 422장 무필터, 사용자가 UI에서 직접 실행)
- 04:29 정상 종료(오류 없음). 1단계 COLMAP 1,569초(422장 전부 등록), flat 모자이크 387초(coverage 0.961, Dense 시작 전 저장), Dense 5h50m(융합 점 24,103,061개).
- 육안: 위쪽 파라펫 직사각형/수평 지붕선/굴뚝 위치가 참조(`gsplat3d/render_dense_stereo_loftr/final_mosaic.png`)와 거의 일치, 왼쪽 모서리 기둥 직선(쐐기 없음). 벽 영역(높이 20~90%, 폭 15~85%) 순검정 0.008% (V007 0.08%, 참조 0.005%).
- 미해결: 벽면 흰 얼룩(원인 미확인, 참조에도 비슷하게 보임 -- 사용자가 다음에 확인), 지붕 위 큰 검은 다각형/벽 밖 배경 조각(참조에도 있음). 점군 색 기반이라 원본 사진 질감이 아님 -- 균열 검출 입력으로 쓰기 전 확대 확인 필요.
- 확인용 이미지: `preview_reference_track/V008_review/`.

### 결과 보기: 스티칭 클릭 지점 정합 오차 (사용자 요청, 오차 정의는 사용자가 "클릭 지점 정합 오차"로 확정)
- 우측(Panel2) 스티칭 클릭 → 좌측 원본 이동 + 원 마커는 기존 기능. 추가: 보고서(PDF) 버튼 옆에 오차 표시.
- 정의: 클릭 지점을 함께 찍은 다른 원본 사진(최대 4장)을 각 호모그래피로 모자이크 좌표에 펼쳐 owner 사진과 NCC 정합(템플릿 65x65 모자이크 px, 탐색 +-16px)했을 때의 어긋남 중앙값. 질감 부족/NCC<0.5/탐색 범위 끝에 붙은 경우는 버림(근거 없는 값 생성 금지). mm는 scale_colmap.json이 calibrated일 때만(GPS 기반 스케일, RTK 아님을 툴팁에 명시).
- 구현: `Services/ClickRegistrationErrorCalculator.cs`(신규), `ResultsCompareViewModel.StartClickErrorCalculation/FormatClickError`, `ComparePanelState.ClickErrorText/Detail`, `ResultsCompareView.xaml` Panel2 툴바 TextBlock. 계산은 백그라운드, 재클릭/화면 전환 시 이전 결과 폐기.
- 한계: 호모그래피(평면) 모델 기준 -- 발코니처럼 평면에서 벗어난 곳은 시차 때문에 크게 나옴. Dense 깊이 보정의 오차가 아님.
- 검증: `dotnet build` 컴파일 오류 0(실행 중인 뷰어가 exe를 잠가 복사만 실패). C# 자체는 앱에서 실행 확인 못 함 -- 같은 알고리즘의 Python 프록시를 V008 데이터 8개 지점에 돌려 6개 지점에서 값(1.0~17.5px, NCC 0.87~0.99)이 나오고 2개 지점은 비교 불가(NCC 미달) 확인.

## 2026-09-26 세션 기록 (2): 크랙 위치 오차 원인 확정 + 깊이 기반 크랙 배치 (사용자: "사업 불가 수준, 반드시 수정")

### 질문 1 -- 크랙 검출은 원본에서 했나, 스티칭 전체 이미지에서 했나 (로그로 확정)
- V008 `CRACK_DETECT_STARTED`의 `"pipeline": "raw_photo"` -- **원본 사진 각각에서 검출**하고 캔버스 좌표로 옮긴 것이지, 스티칭 모자이크를 타일링한 것이 아님(모자이크 타일링은 COLMAP 스케일/호모그래피가 없을 때의 폴백 경로).
- 화면이 스티칭 전체에서 검출한 것처럼 보인 이유: 크랙 다각형이 스티칭 캔버스 좌표로 그려지기 때문. 얼룩까지 크랙으로 잡힌 것은 **검출 모델 오탐**(원본 사진에서 벗겨진 페인트/얼룩 질감을 크랙으로 판정) -- 위치 문제와 별개.

### 질문 2 -- 위치 오차의 실측 원인
- 원본 사진 -> 캔버스 배치를 **평면 호모그래피**(모든 픽셀이 피팅된 벽 평면 위에 있다고 가정)로 했는데, 표시되는 Dense 모자이크는 **실제 3D 점**으로 그려진 것. 벽은 완전한 평면이 아니어서(창 들어감, 패널 굴곡, 발코니, 환기구/배관) 평면에서 d만큼 벗어난 점은 촬영 각도에 따라 d*tan(각도)만큼, **사진마다 다르게** 어긋남.
- 실측(V008, FRONT_cracks_v2 표본 80개, 표시된 Dense 모자이크와 NCC 정합): 호모그래피 배치 중앙값 **14~16 px(=14~16 cm, 캔버스 1 cm/px)**, 90번째 백분위수 24 px, 최대 30 px. 같은 지점을 본 서로 다른 원본 사진끼리도 중앙값 ~10 px 불일치(중복 크랙 병합이 깨지는 원인).
- 렌즈 왜곡은 원인이 아님(SIMPLE_RADIAL k=0.0007). 재구성 평면/Sim3d/내부 파라미터는 실행 때의 호모그래피를 광선-평면 교점으로 0.000 px 재현(검증됨).

### 수정: 깊이 기반 크랙 배치 (`src/geometry/depth_mapping.py`, 신규)
- COLMAP patch_match가 이미 만든 사진별 깊이 맵(`colmap_dense/dense/stereo/depth_maps/*.geometric.bin`)으로 원본 픽셀 -> 3D -> Sim3d -> 벽 평면 (u,v) -> 캔버스 픽셀. Dense 렌더(`_render_dense_canvas`)와 같은 투영이라 크랙이 모자이크에 그려진 그 면 위에 놓임.
- 깊이 맵의 약 35~40%는 다중 뷰 일관성 검사에서 0(무늬 없는/얼룩진 벽에 집중) -> 가까운 유효 깊이로 메우되 40 depth-px(~20 cm) 이내만(`fill_max_px`), 그 밖은 무효 -> 평면 호모그래피로 폴백하고 `method: "flat"`로 명시(근거 없는 좌표 생성 금지).
- pycolmap 배열은 `np.asarray`가 C++ 메모리를 가리키는 뷰일 수 있어(Reconstruction 해제 후 NaN/쓰레기) 반드시 `np.array(copy=True)`로 복사 -- 이 버그로 검출 프로세스에서만 깊이 배치가 조용히 전부 폴백되었음.
- 배선: `pipeline/runner.py::_run_dense_hybrid_stage`가 Dense 직후 `{facade_id}_depth_mapping.json`(Sim3d+평면) 저장 / `tools/detect_cracks_folder.py`가 사이드카가 있고 표시 모자이크가 `_analysis_colmap_dense.tif`일 때만 `depth_mapper`를 `raw_pipeline.detect_cracks_from_raw_images`에 전달. V008처럼 사이드카가 없는 이전 실행은 `tools/make_depth_mapping_sidecar.py <output_dir> <images_dir> FRONT`로 생성(스테이지1 재구성+GPS로 재계산 후 호모그래피와 대조 검증, 불일치 시 저장 안 함).

### 위치 오차 표시 (`src/crack/position_check.py`, 뷰어)
- 정의: 크랙의 주 원본 사진(source_observations[0]) 픽셀을 크랙을 놓은 것과 **같은 방식으로** 캔버스에 놓고, 실제 표시되는 모자이크와 몇 캔버스 px 어긋나는지 NCC(서브픽셀)로 실측. 텍스처 부족/상관 낮음/탐색 경계 -> None("측정 불가", 0이 아님). mm는 스케일 보정된 경우만.
- `{facade_id}_cracks*.json`의 크랙마다 `position_check {offset_px, dx, dy, ncc, method(depth|flat), image_id, offset_mm}`. 뷰어: 크랙 목록 항목, 원본 보기 창 툴바에 "위치 오차 X px (약 Y mm) · 깊이 배치|평면 호모그래피 배치".
- 실험(V008 표본): 깊이 배치 오차 중앙값 1.7~1.8 px(90% 2.7~3.2 px) vs 호모그래피 14~16 px. (호모그래피 쪽은 탐색 범위(24 px)를 넘는 경우가 측정 불가로 빠져 실제로는 더 나쁨.)

### 남은 한계 (사실대로)
- 결과 보기에서 **스티칭 클릭 -> 원본 이동/원 마커**(`JumpToOriginalImageAt`)는 여전히 평면 호모그래피 역변환 -- 크랙 배치와 같은 종류의 오차가 남아 있음(뷰어가 깊이 맵을 읽지 않음). 정밀 개선하려면 사진별 (원본 -> 캔버스) 깊이 격자를 내보내 C#에서 역보간해야 함.
- 길이/폭 mm 측정은 여전히 호모그래피 야코비안 로컬 스케일(평면 가정) -- 벽에서 튀어나온 면의 mm는 깊이 기반 스케일로 바꿔야 정확.
- 검출 모델 오탐(얼룩/벗겨진 페인트)은 별도 문제 -- 이번 수정 범위 아님.
- 백업: `V008/_backup_before_depth_mapping/`(수정 전 cracks*.json, crack_mask*.tif).

## 2026-09-26 세션 기록 (3): 스티칭 클릭 -> 원본 마커 위치를 깊이로 계산 + "선택 위치 오차" 표시 (사용자 정의 확정)

- 사용자가 말한 오차 = **스티칭에서 클릭한 지점 vs 원본 사진에 표시된 마커 위치**. (직전에 만든 "여러 사진 간 정합 오차"는 다른 값이라 삭제: `ClickRegistrationErrorCalculator.cs` 제거.)
- 기존 마커 = 평면 호모그래피 역변환 H^-1(click). V009 벽 위 60곳 실측(원본 픽셀을 캔버스에 올려 표시 모자이크와 NCC 정합): 오차 중앙값 **7.2 px(=7 cm, 원본 사진 기준 약 27 px)**, 90% 14.2, 최대 24 px, 70%가 5 px 초과.
- 수정: `tools/click_locator.py`(상주 프로세스, JSON 라인 프로토콜) -- 클릭 캔버스 좌표에서 깊이 매핑을 뉴턴법으로 역산해 원본 픽셀을 구하고(실패 시 국소 격자 탐색 후 재정련), 그 마커의 오차를 구성과 독립적으로(이미지 내용 NCC, `position_check.check_position`) 잰다. 평면 방식과 깊이 방식 둘 다 측정해 개선이 보이게 함. 시작 1.2 s, 클릭당 0.34 s(V009 40곳 테스트).
- 테스트(V009, 벽 위 40곳 무작위 클릭): 깊이 마커 발견 37/40, 역산 잔차 중앙값 0.03 px(최대 0.23), **독립 측정 오차 평면 6.8 px(p90 13.5) -> 깊이 2.0 px(p90 3.1, 최대 8.4, 5 px 초과 3%)**, 원본 사진 위에서 마커가 옮겨진 거리 중앙값 31 raw px.
- 뷰어: `Services/ClickLocatorClient.cs`(상주 프로세스 관리, facade 바뀌면 교체, 실패 시 null), `ResultsCompareViewModel.StartClickLocate/MoveOriginalMarker/FormatClickError`, `ClickMarkerRefined` 이벤트로 뷰가 다시 스크롤. 동작: 클릭 즉시 임시(평면) 마커 -> 약 0.3~1.5 s 후 깊이 마커로 이동 + 보고서 버튼 옆 "선택 위치 오차 X px (약 Y mm) · 깊이 기반 (평면 방식은 Z px)". 깊이 없는 지점/사이드카 없는 facade는 평면 마커 유지 + 그렇다고 명시, 측정 불가는 "측정 불가"(0 아님).
- 한계: 실제 WPF 화면에서 클릭해 본 확인은 안 함(빌드 오류 0 + 프로토콜을 Python 드라이버로 검증). 클릭 지점이 벽 밖(배경)이면 값이 의미 없음(벽 마스크로 거르지 않음). 최소 요구: `{facade}_depth_mapping.json`이 있는 Dense 결과(V008은 `tools/make_depth_mapping_sidecar.py`로 생성됨, V009는 자동 생성).

## 2026-09-26 세션 기록 (4): 다른 컴퓨터에서 실행할 수 있게 정리 (배치 파일 + 문서)

- 저장소 루트 배치 파일: `check_env.bat`(환경 점검, `tools/check_environment.py`), `setup_new_machine.bat`, `run_stitch.bat`, `run_detect.bat`, `make_depth_sidecar.bat`, `run_viewer.bat` + 공용 `scripts/_python.bat`. 실행 방법은 README 섹션 0에 순서대로 정리.
- Python 탐색: 환경변수 `CHECKCRACK_PYTHON` -> `%USERPROFILE%`/`%LOCALAPPDATA%`/`ProgramData`의 miniconda3/anaconda3 -> PATH. 뷰어(`PythonEnvironment.cs`)도 같은 순서(예전엔 `C:\Users\KAC-USER\miniconda3` 하드코딩).
- 다른 컴퓨터에서 깨지던 것: `*_source_images.json`의 사진 **절대 경로**(스티칭한 컴퓨터 기준). `src/common/paths.py::resolve_source_image`(Python)와 `SourceImagePathResolver.cs`(뷰어)가 같은 파일명을 결과 폴더 위쪽/`images`/`CHECKCRACK_IMAGES_DIR`에서 찾음(파일명만 일치, 다른 사진으로 대체 안 함).
- 새 컴퓨터에 자동으로 안 따라오는 것(문서화): CUDA PyTorch, **CUDA pycolmap**(pip 휠엔 Dense Stereo 없음 -> `scripts/install_colmap_cuda_dense.bat`), 크랙 모델 `*.pt`(gitignore), LoFTR 가중치(첫 실행 때 다운로드), hloc 별도 env(선택).
- `requirements.txt`에 scipy 추가(깊이 매핑/Dense/그래프 정제에서 사용하는데 빠져 있었음). `.gitignore`에 gsplat3d/, preview_reference_track/, training_data_structural_fp_v2_gt/, .vscode/ 추가.
- 미검증: 실제로 두 번째 컴퓨터에서 처음부터 설치해 본 적은 없음 -- 이 컴퓨터에서 `check_env.bat`이 READY로 나오는 것과 경로 해석 함수만 확인.

## 2026-09-26 세션 기록 (5): LEFT(수직 한 줄 촬영)도 FRONT와 같은 산출물(Dense + 깊이 사이드카)로 -- 사용자 확정 "B, C 둘 다"

### 원인 (LEFT 이전 로그 + 실측)
- LEFT는 드론이 벽을 따라 수직으로 오르내림: GPS 촬영 위치 주성분 표준편차 15.3 / 1.6 / 0 m (FRONT 15.8 / 14.8 / 0). 평평한 벽 + 좁은 이동이라 COLMAP이 **사진마다 카메라를 따로** 최적화하면서 초점거리가 흩어짐(한 카메라가 581~18642 px, FRONT는 3730~3835) -> 재구성이 휨(카메라 경로 12x10 m로 퍼짐, 실제 ~1.6 m) -> GPS 정렬 잔차 평균 7.6~9.5 m / 최대 25~45 m -> `COLMAP_ALIGNMENT_POOR` -> 예전엔 GPS/짐벌 자세 기반 대체 보정(`POSE_PRIOR_RECTIFIED`, scale 출처 `gps_pose_prior`, Dense 없음).
- 별개의 발견: 이동 중 촬영분의 EXIF 고도는 수직으로 최대 ~22 m 어긋남(제자리에 떠 있던 사진은 0.3~2 m) -> "전체 사진 평균/최대 잔차" 검사가 좋은 재구성도 탈락시킴. GPS만으로는 휜 재구성과 정상 재구성을 구분할 수 없음(실측: 휜 것 중앙값 0.79 m / 정상 1.89 m) -> 초점거리 일관성이 직접 지표.
- 시도했으나 부족했던 것: COLMAP pose prior(`use_prior_position`; DB의 EXIF prior는 WGS84 + 공분산 NaN이라 오히려 악화, 직접 쓴 CARTESIAN prior도 개선 없음), 카메라 하나만 공유(48장 중 23장만 등록).

### 수정
- **C (재구성)**: `colmap.single_camera: auto`(기본) -- GPS가 한 줄인 촬영일 때만(`gps_configuration_is_degenerate`: 2번째/1번째 주성분 < 0.25, 1번째 >= 3 m) 카메라 하나 공유 + `colmap.known_intrinsics`(카메라 기종별 focal/k1, 현재 `L2D-20c` = FRONT V009 중앙값 3789.27 px)로 **고정**하고 초점거리/왜곡 최적화 끔. 해당 기종 값이 없으면 기존 동작. FRONT/BACK은 auto에서 영향 없음(코드 경로/결과 동일 확인).
- **C (평면)**: `_filter_points_near_plane`에 두 번째 기회 -- 걸러낸 뒤에도 p10-p90 깊이 폭이 5 m 넘으면(벽이 지배적 군집이 아님) 가장 촘촘한 3 m 슬래브에서 다시 시작해 +-1.5 m 밴드만 남김. LEFT: 캔버스 328x103 m(렌더링 안 끝남) -> 14.3x28.0 m. FRONT는 이 분기를 안 타서 6171x4013 그대로(검증).
- **GPS 검사** `check_gps_alignment`: 일반 촬영은 기존 규칙(평균 2 m/최대 3 m) 그대로, GPS가 한 줄인 촬영은 "3 m 이내 비율 >= 50% + 초점거리 상대 편차 <= 10%". 세 트랙(full/dense_only/reference)이 공용.
- **B (폴백)**: reference 트랙도 GPS 검사 실패 또는 캔버스 > 150 MP(평면 맞춤 실패)면 실패로 끝내지 않고 GPS/짐벌 자세 기반 flat 모자이크로 같은 산출물 세트를 저장(Dense/깊이 사이드카 없음, 화면에 평면 방식으로 표시). `_write_flat_colmap_outputs`가 `aligned=None` 허용.

### LEFT 검증 실행 (별도 폴더, 완료 후 삭제)
- 1단계 COLMAP 114 s, 48/48장 등록(재투영 오차 1.19 px), GPS 검사 통과 -> flat 99 s(coverage 0.972) -> Dense 39분(융합 점 1,885,554) -> DONE. 산출물 세트가 FRONT와 동일(`*_colmap_dense.*`, `LEFT_depth_mapping.json`, wall_region_mask 등). 벽 안쪽 순검정 0.000%.
- 크랙 검출도 동작: v1 24건 / v2 5건(무늬 없는 벽이라 적음). 위치 오차 측정 가능한 것 중 깊이 배치 0.9~5.5 px, 평면 배치 0.2~19 px. **깊이 배치 비율이 낮음**(대부분 `flat`으로 표시) -- 깊이 맵 유효 비율 25~77% + 벽 밴드(+-3 m) 밖 픽셀(하늘/배경) 제외 때문. 벽이 평평해서 평면 배치도 오차 ~5 px 수준이지만 FRONT처럼 1.8 px는 아님.
- 한계/미검증: 재구성 재투영 오차가 FRONT(~1.0)보다 약간 큼(1.19), 촬영 정지 구간별로 벽 모자이크에 가로 이음선이 보임(정지 위치가 다른 두 묶음). `known_intrinsics`는 FRONT에서 잰 값을 같은 카메라 기종에 재사용한 것 -- 다른 카메라 기종은 값이 없어 이 경로를 못 씀(대체 보정으로 떨어짐).
