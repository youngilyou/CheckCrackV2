using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>
/// One line of logs/pipeline.log (src/common/logging.py's JsonFormatter).
/// 2026-09-28부터 Python 쪽이 실제 기록 시각을 "timestamp" 필드로 남기므로 <see cref="Timestamp"/>가
/// 있으면 그게 진짜 발생 시각이다. 이 필드가 없는 줄(이 변경 전에 이미 메모리에 떠 있던 구버전
/// 파이프라인 프로세스가 찍은 줄)만 <see cref="ObservedAt"/>(이 앱이 그 줄을 읽은 시각, 실제 발생
/// 시각과 다를 수 있음)로 대신한다 — UI에서 둘을 구분해서 표시할 것, 없는 정밀도를 있는 것처럼
/// 보여주지 말 것.
/// </summary>
public class PipelineLogEntry
{
    [JsonPropertyName("timestamp")]
    public string? TimestampRaw { get; set; }

    /// <summary>Parsed <see cref="TimestampRaw"/>, or null if absent/unparsable (구버전 로그 줄).</summary>
    public DateTime? Timestamp =>
        DateTime.TryParse(TimestampRaw, out var dt) ? dt : null;

    /// <summary>표시용: 실제 발생 시각이 있으면 그것, 없으면 이 앱이 읽은 시각(관찰 시각)에 물음표를
    /// 붙여 구분. LIVE LOG 템플릿이 이 하나만 바인딩하면 된다.</summary>
    public string DisplayTime => Timestamp is { } t ? t.ToString("HH:mm:ss.fff") : ObservedAt.ToString("HH:mm:ss") + "?";

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
