using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

/// <summary>C# port of src/crack/pipeline.py's _compute_source_observations --
/// computes, client-side and immediately, which original photo(s) a crack
/// polygon (in the facade mosaic's own pixel space) is actually visible in,
/// and where. Needed for manually-drawn cracks: the Python pipeline only ever
/// computes source_observations for its OWN AI detections
/// (tools/detect_cracks_folder.py writes them into {facade_id}_cracks.json),
/// and there is no round-trip file for a reviewer's hand-drawn polygon to get
/// the same treatment -- recomputing it here means "원본 보기" works the
/// instant a crack is drawn, no save/regenerate-report round trip needed.
/// Also used as a fallback for AI-detected cracks whose own SourceObservations
/// came back empty (e.g. a facade re-detected with older pipeline code even
/// though its seam artifacts already exist) -- same reasoning, same result
/// either way, so one code path covers both cases.</summary>
public static class SourceObservationCalculator
{
    private const int DefaultMinOverlapPx = 20; // mirrors config/pipeline.yaml's measurement.source_observation_min_overlap_px

    public sealed class SeamArtifacts
    {
        public required ushort[] OwnerMap { get; init; } // row-major, [y * width + x], 0 = no owner
        public required int OwnerMapWidth { get; init; }
        public required int OwnerMapHeight { get; init; }
        public required List<string> OwnerIndex { get; init; } // owner_map value k -> OwnerIndex[k - 1]
        public required Dictionary<string, HomographyEntry> Homographies { get; init; }
    }

    public sealed class HomographyEntry
    {
        [JsonPropertyName("H")] public double[][] H { get; set; } = Array.Empty<double[]>();
        [JsonPropertyName("width")] public int Width { get; set; }
        [JsonPropertyName("height")] public int Height { get; set; }
    }

    private static string? Pick(string outputDir, string facadeId, params string[] suffixes)
    {
        foreach (var suffix in suffixes)
        {
            var candidate = Path.Combine(outputDir, $"{facadeId}{suffix}");
            if (File.Exists(candidate))
                return candidate;
        }
        return null;
    }

    /// <summary>Loads the three artifacts pipeline/runner.py writes alongside
    /// the mosaic (see stitching/mosaic.py's MosaicResult doc comment) --
    /// returns null (never throws) if this facade predates them, exactly the
    /// same "older facade -- no crash" convention detect_cracks_folder.py
    /// uses for the same files on the Python side. COLMAP-preferred picking
    /// matches that script's own precedence, so this always corresponds to
    /// whichever mosaic (analysis.tif vs analysis_colmap.tif) is actually
    /// loaded for review.</summary>
    public static SeamArtifacts? LoadSeamArtifacts(string outputDir, string facadeId)
    {
        var homographiesPath = Pick(outputDir, facadeId, "_homographies_colmap.json", "_homographies.json");
        var seamOwnerMapPath = Pick(outputDir, facadeId, "_seam_owner_map_colmap.png", "_seam_owner_map.png");
        var seamOwnerIndexPath = Pick(outputDir, facadeId, "_seam_owner_index_colmap.json", "_seam_owner_index.json");
        if (homographiesPath == null || seamOwnerMapPath == null || seamOwnerIndexPath == null)
            return null;

        try
        {
            var homographies = JsonSerializer.Deserialize<Dictionary<string, HomographyEntry>>(File.ReadAllText(homographiesPath));
            var ownerIndex = JsonSerializer.Deserialize<List<string>>(File.ReadAllText(seamOwnerIndexPath));
            if (homographies == null || ownerIndex == null)
                return null;

            using var stream = File.OpenRead(seamOwnerMapPath);
            // BitmapCreateOptions.PreservePixelFormat is required here -- without it, WPF's
            // PNG decoder silently auto-converts this 16-bit grayscale (colortype=0,
            // bitdepth=16 -- confirmed correct at the raw PNG byte level) image to Bgr32
            // during decode, destroying the actual owner-index values before this code ever
            // sees them (confirmed directly: frame.Format reported "Bgr32" without this flag,
            // "Gray16" with it -- every pixel then round-tripped through FormatConvertedBitmap
            // below came out as owner=0 everywhere, which is exactly the bug that made
            // manually-drawn cracks' source_observations always come back empty).
            var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.PreservePixelFormat, BitmapCacheOption.OnLoad);
            var frame = decoder.Frames[0];
            var gray16 = new FormatConvertedBitmap(frame, PixelFormats.Gray16, null, 0);
            int w = gray16.PixelWidth, h = gray16.PixelHeight;
            var ownerMap = new ushort[w * h];
            gray16.CopyPixels(ownerMap, w * 2, 0);

            return new SeamArtifacts
            {
                OwnerMap = ownerMap,
                OwnerMapWidth = w,
                OwnerMapHeight = h,
                OwnerIndex = ownerIndex,
                Homographies = homographies,
            };
        }
        catch (Exception ex) when (ex is IOException or JsonException or NotSupportedException)
        {
            return null; // partial write mid-scan, corrupt file, etc. -- same "just no data" treatment as a missing file
        }
    }

    /// <summary>polygonPx: [[x,y], ...] in the facade mosaic's own pixel space
    /// (same coordinate system CrackReviewItem.PolygonPx already uses for both
    /// AI detections and manual additions). Mirrors
    /// crack/pipeline.py:_compute_source_observations exactly: rasterize the
    /// polygon, intersect against the seam-ownership map, keep only owners
    /// above minOverlapPx, map each through that image's homography inverse,
    /// clip to that image's own bounds, sort by owned pixel count descending.</summary>
    public static List<SourceObservationModel> Compute(
        double[][] polygonPx, SeamArtifacts artifacts, int minOverlapPx = DefaultMinOverlapPx)
    {
        var result = new List<SourceObservationModel>();
        if (polygonPx.Length < 3)
            return result;

        double x0f = polygonPx.Min(p => p[0]), y0f = polygonPx.Min(p => p[1]);
        double x1f = polygonPx.Max(p => p[0]), y1f = polygonPx.Max(p => p[1]);
        int x0 = Math.Max(0, (int)Math.Floor(x0f));
        int y0 = Math.Max(0, (int)Math.Floor(y0f));
        int x1 = Math.Min(artifacts.OwnerMapWidth, (int)Math.Ceiling(x1f) + 1);
        int y1 = Math.Min(artifacts.OwnerMapHeight, (int)Math.Ceiling(y1f) + 1);
        if (x1 <= x0 || y1 <= y0)
            return result;

        // Per-pixel point-in-polygon over the (small) bbox crop -- same
        // ray-casting test this ViewModel already uses for canvas hit-testing
        // (IsPointInPolygon), just against mosaic-pixel coordinates here
        // instead of display/canvas coordinates.
        var counts = new Dictionary<ushort, int>();
        for (int py = y0; py < y1; py++)
        {
            for (int px = x0; px < x1; px++)
            {
                if (!IsPointInPolygon(px + 0.5, py + 0.5, polygonPx))
                    continue;
                var owner = artifacts.OwnerMap[py * artifacts.OwnerMapWidth + px];
                if (owner == 0)
                    continue;
                counts[owner] = counts.GetValueOrDefault(owner) + 1;
            }
        }

        foreach (var (label, count) in counts)
        {
            if (count < minOverlapPx)
                continue;
            var imageIndex = label - 1;
            if (imageIndex < 0 || imageIndex >= artifacts.OwnerIndex.Count)
                continue;
            var imageId = artifacts.OwnerIndex[imageIndex];
            if (!artifacts.Homographies.TryGetValue(imageId, out var entry))
                continue;

            var hInv = Invert3x3(entry.H);
            if (hInv == null)
                continue;

            var ptsSrc = polygonPx.Select(p => TransformPoint(hInv, p[0], p[1])).ToArray();
            for (int i = 0; i < ptsSrc.Length; i++)
            {
                ptsSrc[i] = (
                    Math.Clamp(ptsSrc[i].X, 0, entry.Width - 1),
                    Math.Clamp(ptsSrc[i].Y, 0, entry.Height - 1));
            }
            double sx0 = ptsSrc.Min(p => p.X), sy0 = ptsSrc.Min(p => p.Y);
            double sx1 = ptsSrc.Max(p => p.X), sy1 = ptsSrc.Max(p => p.Y);

            result.Add(new SourceObservationModel
            {
                ImageId = imageId,
                BboxPxInSource = new[] { Math.Round(sx0, 1), Math.Round(sy0, 1), Math.Round(sx1, 1), Math.Round(sy1, 1) },
                PolygonPxInSource = ptsSrc.Select(p => new[] { Math.Round(p.X, 1), Math.Round(p.Y, 1) }).ToArray(),
                OwnedPixelCount = count,
            });
        }

        result.Sort((a, b) => b.OwnedPixelCount.CompareTo(a.OwnedPixelCount));
        return result;
    }

    /// <summary>Standard ray-casting point-in-polygon (even-odd rule) -- same
    /// algorithm as this ViewModel's own IsPointInPolygon, duplicated here
    /// (mosaic-pixel-space double[][] input here vs. display-space
    /// PointCollection there) rather than shared, since the two operate on
    /// different point representations and this is a small, stable
    /// algorithm unlikely to need to change in lockstep.</summary>
    private static bool IsPointInPolygon(double x, double y, double[][] polygon)
    {
        var inside = false;
        var n = polygon.Length;
        for (int i = 0, j = n - 1; i < n; j = i++)
        {
            var pi = polygon[i];
            var pj = polygon[j];
            if ((pi[1] > y) != (pj[1] > y) &&
                x < ((pj[0] - pi[0]) * (y - pi[1]) / (pj[1] - pi[1])) + pi[0])
            {
                inside = !inside;
            }
        }
        return inside;
    }

    private static (double X, double Y) TransformPoint(double[][] h, double x, double y)
    {
        double wx = h[0][0] * x + h[0][1] * y + h[0][2];
        double wy = h[1][0] * x + h[1][1] * y + h[1][2];
        double w = h[2][0] * x + h[2][1] * y + h[2][2];
        if (Math.Abs(w) < 1e-12)
            w = 1e-12;
        return (wx / w, wy / w);
    }

    /// <summary>3x3 matrix inverse via the adjugate/cofactor method -- returns
    /// null (never throws) for a near-singular matrix, same "just skip this
    /// candidate" treatment src/crack/pipeline.py's
    /// np.linalg.inv/LinAlgError catch uses.</summary>
    private static double[][]? Invert3x3(double[][] m)
    {
        double a = m[0][0], b = m[0][1], c = m[0][2];
        double d = m[1][0], e = m[1][1], f = m[1][2];
        double g = m[2][0], h = m[2][1], i = m[2][2];

        double det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
        if (Math.Abs(det) < 1e-12)
            return null;

        double invDet = 1.0 / det;
        return new[]
        {
            new[] { (e * i - f * h) * invDet, (c * h - b * i) * invDet, (b * f - c * e) * invDet },
            new[] { (f * g - d * i) * invDet, (a * i - c * g) * invDet, (c * d - a * f) * invDet },
            new[] { (d * h - e * g) * invDet, (b * g - a * h) * invDet, (a * e - b * d) * invDet },
        };
    }
}
