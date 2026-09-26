"""Structure-type extension seam (댐/아파트/공장 등) — 2026-09-24, explicit user
request: a combo box next to the "단지 종합보고서" button so a 단지 can be tagged
DAM/아파트/공장/etc., "왜냐하면 내부 처리 알고리즘이 다른것 같아요".

Clarified with the user before implementing (this session, AskUserQuestion):
structure type SHOULD eventually change real processing (both plane/geometry
assumptions AND crack-judgment criteria — a dam's curved/sloped surface and
structural-safety crack thresholds are genuinely not the same problem as an
apartment tower's flat vertical wall), but explicitly NOT today: "지금은
아파트로직 완료 후 차후 DAM 할경우 로직 건드리고 싶지 않음, 추후 DAM 이미지 확보하여
시험하면서 알고리즘 보정". I.e. this module exists so that ONE PLACE holds the
per-type profile, and today every non-APARTMENT type resolves to the exact
same values APARTMENT already uses (the existing, already-validated
pipeline default) — so selecting DAM/FACTORY today changes NOTHING about
actual output, only tags provenance (`{facade_id}_structure_type.json`,
mirroring the `{facade_id}_scale_colmap.json` sidecar convention). When real
DAM/FACTORY imagery is available, a future session calibrates real values
here without touching APARTMENT's.

`calibrated=False` on the returned profile is the caller's signal to log/
display "미보정 -- 아파트 기본값 사용 중" wherever it matters, so this is never
silently mistaken for "already handled" the way CLAUDE.local.md #9 already
insists calibration status never be silently assumed for mm measurements.
"""

from __future__ import annotations

STRUCTURE_TYPES = ("APARTMENT", "DAM", "FACTORY")
DEFAULT_STRUCTURE_TYPE = "APARTMENT"


def get_structure_profile(structure_type: str | None) -> dict:
    """Returns `{"structure_type": <normalized>, "calibrated": bool}`.

    `calibrated` is True only for APARTMENT today. DAM/FACTORY are
    intentionally NOT branched to different geometry/crack-criteria values
    yet (see module docstring) — this function's job right now is only to
    normalize the label and make that "not yet calibrated" fact explicit and
    machine-readable, not to fabricate per-type numbers nobody has validated.
    An unrecognized value falls back to APARTMENT rather than raising, since
    this tag must never be able to abort an otherwise-successful run.
    """
    normalized = (structure_type or DEFAULT_STRUCTURE_TYPE).strip().upper()
    if normalized not in STRUCTURE_TYPES:
        normalized = DEFAULT_STRUCTURE_TYPE
    return {
        "structure_type": normalized,
        "calibrated": normalized == "APARTMENT",
    }
