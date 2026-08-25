using System;
using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>One AI-detected crack a reviewer has decided is a false positive.
/// Never removes the crack from {facade_id}_cracks.json itself -- that file
/// is pipeline-owned and gets rewritten on every re-detection run, so review
/// state lives in a separate sibling file instead (src/crack/review.py's
/// apply_review reads both and merges them at report-generation time).</summary>
public sealed class RejectedCrackEntry
{
    [JsonPropertyName("crack_id")] public string CrackId { get; set; } = "";
    [JsonPropertyName("reviewed_by")] public string? ReviewedBy { get; set; }
    [JsonPropertyName("reviewed_at")] public DateTimeOffset ReviewedAt { get; set; }
    [JsonPropertyName("note")] public string? Note { get; set; }
}

/// <summary>A crack the AI missed, drawn by a reviewer directly on the facade
/// mosaic. CrackId is null until src/crack/review.py assigns one (a stable
/// "{facade_id}_M000001"-style id) the first time a report is generated --
/// the viewer never invents an id itself, since numbering must not collide
/// with whatever else review.py has already assigned across prior saves.</summary>
public sealed class ManualCrackAddition
{
    [JsonPropertyName("crack_id")] public string? CrackId { get; set; }
    [JsonPropertyName("polygon_px")] public double[][] PolygonPx { get; set; } = Array.Empty<double[]>();
    [JsonPropertyName("added_by")] public string? AddedBy { get; set; }
    [JsonPropertyName("added_at")] public DateTimeOffset AddedAt { get; set; }
    [JsonPropertyName("note")] public string? Note { get; set; }
}

/// <summary>Maps {facade_id}_crack_review.json -- one version's worth of human
/// review decisions, living next to that version's {facade_id}_cracks.json
/// (never inside it). Re-stitching/re-detecting creates a new version
/// folder with a fresh mosaic and no review file yet, so review is
/// necessarily redone per version -- same scoping FacadeVersionStore
/// already uses for crack-id stability (match_crack_ids is documented as
/// "same version only" in merge_tiles.py), not a gap specific to this
/// feature.</summary>
public sealed class CrackReviewFile
{
    [JsonPropertyName("facade_id")] public string FacadeId { get; set; } = "";
    [JsonPropertyName("reviewed_by")] public string? ReviewedBy { get; set; }
    [JsonPropertyName("reviewed_at")] public DateTimeOffset? ReviewedAt { get; set; }
    [JsonPropertyName("rejected")] public List<RejectedCrackEntry> Rejected { get; set; } = new();
    [JsonPropertyName("manual_additions")] public List<ManualCrackAddition> ManualAdditions { get; set; } = new();
}
