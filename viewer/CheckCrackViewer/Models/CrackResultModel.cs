using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>One original (pre-stitch) photo a crack is actually visible in, and
/// where within that photo -- mirrors src/common/types.py: SourceObservation.
/// Unlike CrackResultModel.SourceImageIds (facade-level provenance, every
/// photo used for the whole facade), this pins the crack to the specific
/// 1-2 source photos that actually cover its pixels, computed from the
/// stitch stage's persisted seam-ownership map + per-image homography (see
/// src/crack/pipeline.py's _compute_source_observations). Empty on older
/// cracks.json files written before this existed -- never fabricated.</summary>
public sealed class SourceObservationModel
{
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";

    /// <summary>[x0, y0, x1, y1] in this source image's own pixel space (not
    /// the facade mosaic's).</summary>
    [JsonPropertyName("bbox_px_in_source")] public double[] BboxPxInSource { get; set; } = System.Array.Empty<double>();

    /// <summary>[[x,y], [x,y], ...] in this source image's own pixel space.</summary>
    [JsonPropertyName("polygon_px_in_source")] public double[][] PolygonPxInSource { get; set; } = System.Array.Empty<double[]>();

    /// <summary>How many mosaic pixels under this crack this source image
    /// actually owned per the seam mask -- source_observations is sorted by
    /// this descending, so index 0 is the best single photo to show.</summary>
    [JsonPropertyName("owned_pixel_count")] public int OwnedPixelCount { get; set; }
}

/// <summary>src/crack/position_check.py의 실측 결과(2026-09-26, 사용자 요구): 이 크랙의 주 원본 사진
/// 픽셀을 크랙을 놓은 것과 같은 방식으로 캔버스에 놓았을 때, 실제로 표시되는 모자이크와 몇 캔버스
/// px 어긋나는지. null(필드 없음 또는 측정 불가)은 "미측정"이지 "오차 0"이 아니다.</summary>
public sealed class PositionCheckModel
{
    [JsonPropertyName("offset_px")] public double OffsetPx { get; set; }
    [JsonPropertyName("dx")] public double Dx { get; set; }
    [JsonPropertyName("dy")] public double Dy { get; set; }
    [JsonPropertyName("ncc")] public double Ncc { get; set; }
    /// <summary>"depth"(각 픽셀의 깊이로 배치) 또는 "flat"(평면 호모그래피로 배치).</summary>
    [JsonPropertyName("method")] public string Method { get; set; } = "";
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    /// <summary>스케일이 보정된 facade에서만 채워짐(CLAUDE.local.md #9/#26).</summary>
    [JsonPropertyName("offset_mm")] public double? OffsetMm { get; set; }
}

/// <summary>Maps {facade_id}_cracks.json — mirrors src/common/types.py: Crack.
/// NOTE: as of this app's first version, the Python pipeline does not yet
/// write this file automatically (crack/pipeline.py:detect_cracks() was
/// only run ad hoc; the baseline model also has a known false-positive
/// issue, see project memory). The facade panel shows "크랙 검사 미실행"
/// when the file is absent rather than fabricating a result.</summary>
public class CrackResultModel
{
    [JsonPropertyName("crack_id")]
    public string CrackId { get; set; } = "";

    [JsonPropertyName("facade_id")]
    public string FacadeId { get; set; } = "";

    [JsonPropertyName("length_px")]
    public double LengthPx { get; set; }

    [JsonPropertyName("max_width_px")]
    public double MaxWidthPx { get; set; }

    [JsonPropertyName("mean_width_px")]
    public double MeanWidthPx { get; set; }

    [JsonPropertyName("area_px")]
    public double AreaPx { get; set; }

    [JsonPropertyName("length_mm")]
    public double? LengthMm { get; set; }

    [JsonPropertyName("max_width_mm")]
    public double? MaxWidthMm { get; set; }

    [JsonPropertyName("area_mm2")]
    public double? AreaMm2 { get; set; }

    [JsonPropertyName("confidence")]
    public double Confidence { get; set; }

    [JsonPropertyName("observation_state")]
    public string ObservationState { get; set; } = "OBSERVED";

    /// <summary>"정밀점검대상"/"경미" -- null unless calibrated (CLAUDE.local.md #9/#26).</summary>
    [JsonPropertyName("severity")]
    public string? Severity { get; set; }

    [JsonPropertyName("source_image_ids")]
    public List<string> SourceImageIds { get; set; } = new();

    /// <summary>[x0, y0, x1, y1] in the facade mosaic's own pixel space. Nullable
    /// because older cracks.json files (written before the review feature) may
    /// not have it -- absence just means "no overlay to draw", not an error.</summary>
    [JsonPropertyName("bbox_px")]
    public double[]? BboxPx { get; set; }

    /// <summary>[[x,y], [x,y], ...] in the facade mosaic's own pixel space --
    /// same coordinate system CrackReviewStore's manual additions use, so both
    /// AI and reviewer-drawn polygons overlay on the same canvas the same way.</summary>
    [JsonPropertyName("polygon_px")]
    public double[][]? PolygonPx { get; set; }

    /// <summary>Which specific original photo(s) this crack is actually
    /// visible in, and where -- see SourceObservationModel. Empty (not null)
    /// when the stitch that produced this mosaic predates homography/seam-map
    /// persistence (older facades) -- always safe to enumerate.</summary>
    [JsonPropertyName("source_observations")]
    public List<SourceObservationModel> SourceObservations { get; set; } = new();

    [JsonPropertyName("position_check")]
    public PositionCheckModel? PositionCheck { get; set; }
}
