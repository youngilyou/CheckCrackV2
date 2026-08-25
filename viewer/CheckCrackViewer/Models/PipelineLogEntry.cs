using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>
/// One line of logs/pipeline.log (src/common/logging.py's JsonFormatter).
/// The Python logger never writes a timestamp field, so <see cref="ObservedAt"/>
/// is when this app read the line, not when the event actually happened —
/// keep it labeled that way in the UI, don't imply precision that isn't there.
/// </summary>
public class PipelineLogEntry
{
    [JsonPropertyName("level")]
    public string Level { get; set; } = "INFO";

    [JsonPropertyName("message")]
    public string Message { get; set; } = "";

    [JsonPropertyName("stage")]
    public string? Stage { get; set; }

    [JsonPropertyName("facade_id")]
    public string? FacadeId { get; set; }

    [JsonPropertyName("building_id")]
    public string? BuildingId { get; set; }

    [JsonPropertyName("elapsed_s")]
    public double? ElapsedS { get; set; }

    [JsonPropertyName("image_a")]
    public string? ImageA { get; set; }

    [JsonPropertyName("image_b")]
    public string? ImageB { get; set; }

    [JsonPropertyName("matches")]
    public int? Matches { get; set; }

    [JsonPropertyName("inliers")]
    public int? Inliers { get; set; }

    [JsonPropertyName("inlier_ratio")]
    public double? InlierRatio { get; set; }

    [JsonPropertyName("median_reproj_px")]
    public double? MedianReprojPx { get; set; }

    [JsonPropertyName("status")]
    public string? Status { get; set; }

    [JsonPropertyName("progress")]
    public string? Progress { get; set; }

    [JsonPropertyName("pair_count")]
    public int? PairCount { get; set; }

    [JsonPropertyName("image_count")]
    public int? ImageCount { get; set; }

    [JsonPropertyName("coverage_ratio")]
    public double? CoverageRatio { get; set; }

    [JsonPropertyName("global_drift_score_px")]
    public double? GlobalDriftScorePx { get; set; }

    [JsonPropertyName("needs_colmap_fallback")]
    public bool? NeedsColmapFallback { get; set; }

    [JsonPropertyName("reasons")]
    public List<string>? Reasons { get; set; }

    [JsonPropertyName("colmap_fallback_reasons")]
    public List<string>? ColmapFallbackReasons { get; set; }

    [JsonPropertyName("num_images_registered")]
    public int? NumImagesRegistered { get; set; }

    [JsonPropertyName("num_images_requested")]
    public int? NumImagesRequested { get; set; }

    [JsonPropertyName("mean_reprojection_error_px")]
    public double? MeanReprojectionErrorPx { get; set; }

    [JsonPropertyName("output_dir")]
    public string? OutputDir { get; set; }

    [JsonPropertyName("preview_path")]
    public string? PreviewPath { get; set; }

    [JsonPropertyName("error")]
    public string? Error { get; set; }

    /// <summary>Any field this model doesn't have an explicit property for — never silently dropped.</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? Extra { get; set; }

    /// <summary>When this app observed the line, NOT when the pipeline emitted it (see class remarks).</summary>
    public DateTime ObservedAt { get; set; } = DateTime.Now;

    public int LineNumber { get; set; }
}
