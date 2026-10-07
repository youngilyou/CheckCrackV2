"""내규 검사 (2026-10-08): 이미지 파일 읽기/쓰기는 src/common/imageio.py(imread_unicode/imwrite_unicode)로만 한다.

Windows의 OpenCV는 cv2.imread/cv2.imwrite에 한글(비ASCII) 경로를 주면 오류 없이 실패한다(None / False).
원격 분석은 사진을 ...\\extracted\\수목토_1100_1\\처럼 한글 폴더에 풀어 왔고, depth_fill 한 곳이 cv2.imread를 써서
벽 질감 보정이 통째로 건너뛰어졌다(2026-10-07 원격 분석 두 건, texture_px=0). 같은 실수가 다시 들어오지 않도록
src/ 와 tools/ 의 .py에서 아래 호출을 찾으면 실패한다(외부에서 가져온 vendor 코드는 이 파이프라인이 경로를 넘기지
않으므로 제외).

  python tools/check_unicode_io.py        -> 위반이 있으면 목록을 출력하고 exit 1
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(r"\bcv2\.(imread|imwrite|imreadmulti|VideoCapture|VideoWriter)\s*\(")
ALLOWED_FILES = {ROOT / "src" / "common" / "imageio.py", Path(__file__).resolve()}
EXCLUDED_DIR_PARTS = {"phasr_vendor", "sid_vendor", "__pycache__"}


def find_violations() -> list[str]:
    hits = []
    for base in (ROOT / "src", ROOT / "tools"):
        for path in base.rglob("*.py"):
            if path.resolve() in ALLOWED_FILES or EXCLUDED_DIR_PARTS & set(path.parts):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                code = line.split("#", 1)[0]
                if FORBIDDEN.search(code):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    return hits


def main() -> int:
    hits = find_violations()
    if hits:
        print("cv2 직접 파일 입출력 금지 -- src/common/imageio.py의 imread_unicode/imwrite_unicode를 쓰세요:")
        for h in hits:
            print("  " + h)
        return 1
    print("OK: cv2 직접 파일 입출력 없음 (한글 경로 안전)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
