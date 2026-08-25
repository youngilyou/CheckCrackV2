using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>Maps {facade_id}_quality_report.json / _quality_report_colmap.json
/// (src/common/types.py: StitchQualityReport).</summary>
public class QualityReportModel
{
    [JsonPropertyName("facade_id")]
    public string FacadeId { get; set; } = "";

    [JsonPropertyName("image_count")]
    public int ImageCount { get; set; }

    [JsonPropertyName("matched_pair_count")]
    public int MatchedPairCount { get; set; }

    [JsonPropertyName("failed_pair_count")]
    public int FailedPairCount { get; set; }

    [JsonPropertyName("mean_inlier_ratio")]
    public double? MeanInlierRatio { get; set; }

    [JsonPropertyName("median_reprojection_error_px")]
    public double? MedianReprojectionErrorPx { get; set; }

    [JsonPropertyName("coverage_ratio")]
    public double? CoverageRatio { get; set; }

    [JsonPropertyName("disconnected_components")]
    public int DisconnectedComponents { get; set; } = 1;

    [JsonPropertyName("reference_image_id")]
    public string? ReferenceImageId { get; set; }

    [JsonPropertyName("unreachable_image_ids")]
    public List<string> UnreachableImageIds { get; set; } = new();

    [JsonPropertyName("global_drift_score_px")]
    public double? GlobalDriftScorePx { get; set; }

    [JsonPropertyName("max_drift_score_px")]
    public double? MaxDriftScorePx { get; set; }

    [JsonPropertyName("cycle_edge_count")]
    public int CycleEdgeCount { get; set; }

    [JsonPropertyName("needs_colmap_fallback")]
    public bool NeedsColmapFallback { get; set; }

    [JsonPropertyName("colmap_fallback_reasons")]
    public List<string> ColmapFallbackReasons { get; set; } = new();
}
