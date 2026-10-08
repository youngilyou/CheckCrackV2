using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>Automatic floor-line suggestion in {facade_id}_floors.json (src/geometry/floor_estimate.py).</summary>
public sealed class FloorSuggestion
{
    [JsonPropertyName("roof_row_px")] public double? RoofRowPx { get; set; }
    [JsonPropertyName("wall_top_row_px")] public double? WallTopRowPx { get; set; }
    [JsonPropertyName("wall_bottom_row_px")] public double? WallBottomRowPx { get; set; }
    [JsonPropertyName("pitch_px")] public double? PitchPx { get; set; }
    [JsonPropertyName("pitch_m")] public double? PitchM { get; set; }
    [JsonPropertyName("pattern_score")] public double? PatternScore { get; set; }
    [JsonPropertyName("floors_in_view")] public double? FloorsInView { get; set; }
}

/// <summary>The operator-confirmed floor setting -- the only source of floor numbers (report, DB).</summary>
public sealed class FloorConfirmed
{
    [JsonPropertyName("roof_row_px")] public double RoofRowPx { get; set; }
    [JsonPropertyName("pitch_px")] public double PitchPx { get; set; }
    [JsonPropertyName("total_floors")] public int TotalFloors { get; set; }
    [JsonPropertyName("confirmed_by")] public string? ConfirmedBy { get; set; }
    [JsonPropertyName("confirmed_at")] public DateTimeOffset? ConfirmedAt { get; set; }
}

public sealed class FloorSettingFile
{
    [JsonPropertyName("canvas_width")] public int CanvasWidth { get; set; }
    [JsonPropertyName("canvas_height")] public int CanvasHeight { get; set; }
    [JsonPropertyName("px_per_m")] public double PxPerM { get; set; }
    [JsonPropertyName("suggested")] public FloorSuggestion? Suggested { get; set; }
    [JsonPropertyName("confirmed")] public FloorConfirmed? Confirmed { get; set; }
}

/// <summary>{facade_id}_floors.json -- floor lines on the COLMAP/Dense mosaic (2026-10-09, 사용자 확정 "권장 방식"):
/// the pipeline writes an automatic suggestion (top line + slab-joint pitch), the operator confirms the top line, pitch
/// and total floor count in ImageViewerWindow, and only the confirmed setting produces floor numbers. Same rule as
/// src/geometry/floor_estimate.py::floor_number_at -- keep the two in sync.</summary>
public static class FloorSettingStore
{
    public static string PathFor(string outputDir, string facadeId) => Path.Combine(outputDir, $"{facadeId}_floors.json");

    public static FloorSettingFile? Load(string outputDir, string facadeId)
    {
        var path = PathFor(outputDir, facadeId);
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<FloorSettingFile>(stream);
        }
        catch (Exception)
        {
            return null;
        }
    }

    public static FloorConfirmed? LoadConfirmed(string outputDir, string facadeId) =>
        Load(outputDir, facadeId)?.Confirmed is { PitchPx: > 0, TotalFloors: > 0 } c ? c : null;

    /// <summary>Writes (or with null clears) the "confirmed" block, keeping everything else in the file. No BOM --
    /// Python's json.loads rejects one.</summary>
    public static void SaveConfirmed(string outputDir, string facadeId, FloorConfirmed? confirmed)
    {
        var path = PathFor(outputDir, facadeId);
        var root = File.Exists(path) ? JsonNode.Parse(File.ReadAllText(path)) as JsonObject ?? new JsonObject() : new JsonObject();
        root["confirmed"] = confirmed == null ? null : JsonSerializer.SerializeToNode(confirmed);
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, root.ToJsonString(new JsonSerializerOptions
        {
            WriteIndented = true, Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        }), new UTF8Encoding(false));
        File.Move(tmp, path, overwrite: true);
    }

    /// <summary>Floor number of a canvas row, or null above the top line / below floor 1.</summary>
    public static int? FloorAt(double rowPx, double roofRowPx, double pitchPx, int totalFloors)
    {
        if (pitchPx <= 0 || totalFloors <= 0 || rowPx < roofRowPx)
            return null;
        var n = totalFloors - (int)Math.Floor((rowPx - roofRowPx) / pitchPx);
        return n >= 1 && n <= totalFloors ? n : null;
    }

    /// <summary>(lowest, highest) floor covered by rows top..bottom (a crack bbox), or (null, null).</summary>
    public static (int? Min, int? Max) FloorRange(FloorConfirmed? setting, double topRowPx, double bottomRowPx)
    {
        if (setting == null)
            return (null, null);
        var floors = new[] { FloorAt(topRowPx, setting.RoofRowPx, setting.PitchPx, setting.TotalFloors),
                             FloorAt(bottomRowPx, setting.RoofRowPx, setting.PitchPx, setting.TotalFloors) }
            .Where(f => f.HasValue).Select(f => f!.Value).ToList();
        return floors.Count == 0 ? (null, null) : (floors.Min(), floors.Max());
    }
}
