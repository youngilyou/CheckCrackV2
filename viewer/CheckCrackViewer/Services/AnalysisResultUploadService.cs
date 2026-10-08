using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

/// <summary>One stored result file -- one crackvision_result_files row (MngData
/// schemas/crackvision_storage.sql).</summary>
public sealed record ResultFileRecord(string Kind, string FileName, string RemotePath, long SizeBytes,
    string Sha256, bool KeepAfterContract);

/// <summary>2026-10-07 (사용자 확정 "권장 구성"): what of a finished facade run goes to MngData (.43)
/// and what stays on this workstation.
///
/// Uploaded file by file (replaces the old "zip the whole version folder" write-back, ~6-7 GB per
/// facade after the Dense cleanup, ~40 GB before it) and registered in crackvision_result_files
/// with the kinds MngData's crackvision_storage.sql defines -- so the contract-end cleanup on .43
/// knows exactly what exists and what to keep:
///   * the version folder's top-level result files (mosaics, crack json/masks, report, sidecars),
///     about 0.3 GB per facade;
///   * crack_source_photo: the original photo each report card points at (the photo the report's
///     crack was measured on) -- kept after the contract ends together with the report.
///
/// NOT uploaded, by design: the Dense Stereo data (`colmap_dense/`, `colmap_stage1/` -- geometric
/// depth maps + sparse + fused.ply, ~10 GB per facade). It stays on this workstation for the
/// contract's lifetime (re-analysis/review use it directly, no download + unzip), its location is
/// recorded in the DB (facade_analysis_results[facade].dense_storage), and DenseRetentionService
/// deletes it once the archive's contract-end cleanup is due. Sub-folders in general are skipped
/// (`_backup_*` etc. are local work files).</summary>
public static class AnalysisResultUploadService
{
    /// <summary>Kind for a top-level file of a facade's version folder, or null to skip it.
    /// Keep in sync with MngData crackvision_storage.sql's kind list.</summary>
    public static (string Kind, bool Keep)? Classify(string fileName, string facadeId)
    {
        var name = fileName.ToLowerInvariant();
        var prefix = facadeId.ToLowerInvariant() + "_";
        if (!name.StartsWith(prefix))
            return null; // not this facade's output (stray file)
        var rest = name[prefix.Length..];

        if (rest == "report.pdf") return ("report_pdf", true);
        if (rest.StartsWith("report_cards") && rest.EndsWith(".json")) return ("report_cards", false);
        if (rest.StartsWith("visual") && rest.EndsWith(".tif")) return ("mosaic_visual", false);
        if (rest.StartsWith("analysis") && rest.EndsWith(".tif")) return ("mosaic_analysis", false);
        if (rest.StartsWith("cracks") && rest.EndsWith(".json")) return ("crack_json", false);
        if (rest.StartsWith("crack_mask") && rest.EndsWith(".tif")) return ("crack_mask", false);
        if (rest == "crack_review.json") return ("crack_review", false);
        if (rest == "manual_region.json") return ("manual_region", false);
        if (rest.EndsWith(".json") && (rest.StartsWith("scale") || rest.StartsWith("depth_mapping") || rest.StartsWith("structure_type")))
            return ("measurement_json", false);
        if (rest.EndsWith(".json") && (rest.StartsWith("quality") || rest.StartsWith("colmap_report")))
            return ("quality_json", false);
        if (rest.EndsWith(".pdf") || rest.EndsWith(".tmp") || rest.EndsWith(".partial"))
            return null;
        return ("other", false);
    }

    /// <summary>Builds the upload list for one facade run. Remote layout:
    /// {remoteResultsDir}/{file} and {remoteResultsDir}/source_photos/{photo}.</summary>
    public static List<(string LocalPath, string Kind, string FileName, string RemoteDir, bool Keep)> BuildUploadList(
        string outputDir, string facadeId, string remoteResultsDir)
    {
        var list = new List<(string, string, string, string, bool)>();
        foreach (var path in Directory.EnumerateFiles(outputDir).OrderBy(p => p, StringComparer.OrdinalIgnoreCase))
        {
            var fileName = Path.GetFileName(path);
            var cls = Classify(fileName, facadeId);
            if (cls is { } c)
                list.Add((path, c.Kind, fileName, remoteResultsDir, c.Keep));
        }

        // crack_source_photo: the photo each report card was measured on. Missing photo / card file
        // just means none are added -- never a substitute photo (SourceImagePathResolver only
        // matches the same file name).
        var photoDir = remoteResultsDir.TrimEnd('/') + "/source_photos";
        foreach (var photo in ReportCardPhotos(outputDir, facadeId))
            list.Add((photo, "crack_source_photo", Path.GetFileName(photo), photoDir, true));
        return list;
    }

    private static IEnumerable<string> ReportCardPhotos(string outputDir, string facadeId)
    {
        var cardsPath = Path.Combine(outputDir, $"{facadeId}_report_cards.json");
        var sourcesPath = Path.Combine(outputDir, $"{facadeId}_source_images.json");
        if (!File.Exists(cardsPath) || !File.Exists(sourcesPath))
            yield break;

        ReportCardsFile? cards;
        List<SourceImageEntry>? sources;
        try
        {
            cards = JsonSerializer.Deserialize<ReportCardsFile>(File.ReadAllText(cardsPath));
            sources = JsonSerializer.Deserialize<List<SourceImageEntry>>(File.ReadAllText(sourcesPath));
        }
        catch (Exception)
        {
            yield break;
        }
        if (cards == null || sources == null)
            yield break;

        var byId = sources.Where(s => !string.IsNullOrEmpty(s.ImageId))
            .GroupBy(s => s.ImageId!).ToDictionary(g => g.Key, g => g.First().FilePath);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var card in cards.Cards)
        {
            if (string.IsNullOrEmpty(card.ImageId) || !byId.TryGetValue(card.ImageId, out var filePath) || filePath == null)
                continue;
            var resolved = SourceImagePathResolver.Resolve(filePath, outputDir);
            if (resolved != null && seen.Add(resolved))
                yield return resolved;
        }

        // 2026-10-09: every photo each reported crack was seen in (crackvision_crack_sources), not only the card's
        // first one -- SmartCrackWeb lists them all. Needs the version-2 report list (which cracks are in the report).
        foreach (var path in ReportedCrackSourcePhotos(outputDir, facadeId, cardsPath, byId))
            if (seen.Add(path))
                yield return path;
    }

    private static List<string> ReportedCrackSourcePhotos(string outputDir, string facadeId, string cardsPath,
        Dictionary<string, string?> byId)
    {
        var result = new List<string>();
        try
        {
            var report = JsonSerializer.Deserialize<ReportCardsJson>(File.ReadAllText(cardsPath));
            if (report is not { Version: >= 2, Report: not null })
                return result;
            var cracksPath = Path.Combine(outputDir, report.Report.CracksFile ?? $"{facadeId}_cracks.json");
            if (!File.Exists(cracksPath))
                return result;
            var reported = report.Cracks.Select(c => c.CrackId).ToHashSet();
            var cracks = JsonSerializer.Deserialize<List<CrackJsonEntry>>(File.ReadAllText(cracksPath)) ?? new();
            foreach (var crack in cracks.Where(c => reported.Contains(c.CrackId)))
                foreach (var obs in crack.SourceObservations)
                    if (byId.TryGetValue(obs.ImageId, out var filePath) && filePath != null
                        && SourceImagePathResolver.Resolve(filePath, outputDir) is { } resolved)
                        result.Add(resolved);
        }
        catch (Exception)
        {
            // optional extra photos -- the card photos above are already in the list
        }
        return result;
    }

    public static string Sha256Of(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
    }

    private sealed class SourceImageEntry
    {
        [System.Text.Json.Serialization.JsonPropertyName("image_id")] public string? ImageId { get; set; }
        [System.Text.Json.Serialization.JsonPropertyName("file_path")] public string? FilePath { get; set; }
    }
}
