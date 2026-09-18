using System.Collections.Generic;
using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>{facade_id}_scale_colmap.json -- written by runner.py after stage-2 COLMAP
/// (px_per_m is the fixed canvas resolution constant, "calibrated" reflects whether the
/// GPS/COLMAP alignment behind it is trustworthy enough to treat px_per_m as real meters).</summary>
public sealed class ScaleColmapModel
{
    [JsonPropertyName("px_per_m")] public double PxPerM { get; set; }
    [JsonPropertyName("calibrated")] public bool Calibrated { get; set; }
}

/// <summary>One floor's vertical span in the rectified mosaic's own pixel space
/// (row 0 = top of image).</summary>
public sealed record FloorRow(int FloorNumber, double TopPx, double BottomPx);

/// <summary>Computes where each floor's boundary falls in a rectified facade
/// image, from the user-entered building info (BuildingMetadataStore) and the
/// facade's own px-per-meter scale ({facade_id}_scale_colmap.json).
///
/// Known limitation (memory/floor_labeling_needs_bim.md, 사용자 확인): without
/// real BIM/설계 도면, there is no ground-truth reference for exactly where the
/// roofline sits in the canvas. This assumes the top of the canvas (row 0) is
/// the top of the highest floor -- a simplifying approximation, not a survey
/// measurement. Callers must display this as an estimate, never as a verified
/// value (same principle as CLAUDE.local.md #9/#26's calibration requirement
/// for mm figures).</summary>
public static class FloorLabelCalculator
{
    /// <summary>Loads {facade_id}_scale_colmap.json from a facade's output dir. Returns
    /// null if missing/corrupt/not calibrated -- callers must treat that as "cannot
    /// compute floor rows for this facade", never fall back to an unscaled guess.</summary>
    public static ScaleColmapModel? LoadScale(string outputDir, string facadeId)
    {
        var path = Path.Combine(outputDir, $"{facadeId}_scale_colmap.json");
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var model = JsonSerializer.Deserialize<ScaleColmapModel>(stream);
            return model is { Calibrated: true, PxPerM: > 0 } ? model : null;
        }
        catch (JsonException) { return null; }
        catch (IOException) { return null; }
    }

    public static List<FloorRow> ComputeFloorRows(int totalFloors, double floorHeightM, double pxPerM, int canvasHeightPx)
    {
        var rows = new List<FloorRow>();
        if (totalFloors <= 0 || floorHeightM <= 0 || pxPerM <= 0 || canvasHeightPx <= 0)
            return rows;

        double floorHeightPx = floorHeightM * pxPerM;
        for (int i = 0; i < totalFloors; i++)
        {
            int floorNumber = totalFloors - i; // top row = highest floor
            double top = i * floorHeightPx;
            if (top >= canvasHeightPx)
                break; // building metadata says more floors than the canvas actually spans
            double bottom = System.Math.Min(top + floorHeightPx, canvasHeightPx);
            rows.Add(new FloorRow(floorNumber, top, bottom));
        }
        return rows;
    }
}
