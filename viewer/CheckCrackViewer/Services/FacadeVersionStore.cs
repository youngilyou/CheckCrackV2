using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace CheckCrackViewer.Services;

public sealed class FacadeVersionEntry
{
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("created_at")] public DateTimeOffset CreatedAt { get; set; }
    [JsonPropertyName("created_by")] public string? CreatedBy { get; set; }
    [JsonPropertyName("stitch_status")] public string? StitchStatus { get; set; }
    [JsonPropertyName("crack_status")] public string? CrackStatus { get; set; }
    [JsonPropertyName("report_status")] public string? ReportStatus { get; set; }
}

public sealed class FacadeVersionIndex
{
    [JsonPropertyName("current")] public string? Current { get; set; }
    [JsonPropertyName("versions")] public List<FacadeVersionEntry> Versions { get; set; } = new();
}

/// <summary>Read-only projection of one version's metadata for UI binding.</summary>
public sealed record RunVersionInfo(
    string Version,
    DateTimeOffset CreatedAt,
    string? CreatedBy,
    string? StitchStatus,
    string? CrackStatus,
    string? ReportStatus,
    bool IsCurrent);

/// <summary>Owns output/version_index.json: one facade's run history (V001, V002, ...).
///
/// Version *numbering* is derived from Vnnn subfolders actually present on disk
/// (AllocateNextVersionDir), never from the index's own Versions.Count — so a
/// lost/corrupt index, or a crashed run's leftover partial folder, can never
/// cause number reuse / silently overwrite a prior good version. The index
/// file is only metadata (created_at/created_by/per-stage status) plus the
/// "current" pointer.
///
/// Backward-compatible by construction: a facade with no version_index.json
/// (processed before this feature shipped) resolves straight through to its
/// existing flat output/ dir via ResolveCurrentDir — no migration, no data
/// moved.</summary>
public static class FacadeVersionStore
{
    private const string IndexFileName = "version_index.json";
    private static readonly Regex VersionDirPattern = new(@"^V(\d{3,})$", RegexOptions.Compiled);
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    public static FacadeVersionIndex? LoadIndex(string baseOutputDir)
    {
        var path = Path.Combine(baseOutputDir, IndexFileName);
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<FacadeVersionIndex>(stream);
        }
        catch (JsonException)
        {
            return null; // partially-written/corrupt -- treat as "no version history yet"
        }
        catch (IOException)
        {
            return null;
        }
    }

    private static void SaveIndex(string baseOutputDir, FacadeVersionIndex index)
    {
        Directory.CreateDirectory(baseOutputDir);
        var path = Path.Combine(baseOutputDir, IndexFileName);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(index, SerializerOptions));
        File.Move(tempPath, path, overwrite: true); // same atomic-rename pattern src/report/pdf_report.py uses for the PDF itself
    }

    /// <summary>Peeks the next version label without writing anything. Numbers
    /// off Vnnn folders actually on disk, not the index, so a failed run's
    /// leftover partial folder or a missing/corrupt index never causes number
    /// reuse that would overwrite a previously-good version.</summary>
    public static (string Version, string Path) AllocateNextVersionDir(string baseOutputDir)
    {
        var next = 1;
        if (Directory.Exists(baseOutputDir))
        {
            foreach (var dir in Directory.GetDirectories(baseOutputDir))
            {
                var match = VersionDirPattern.Match(Path.GetFileName(dir));
                if (match.Success && int.TryParse(match.Groups[1].Value, out var n))
                    next = Math.Max(next, n + 1);
            }
        }
        var label = $"V{next:D3}";
        return (label, Path.Combine(baseOutputDir, label));
    }

    /// <summary>Call only after a stitch run exits 0. Advances `current` to the
    /// new version. A failed run must never call this — `current` stays on the
    /// last good version, matching this codebase's existing "실패 시 버전을
    /// 늘리지 않는다" pattern (see src/common/atomic_io.py).</summary>
    public static void RecordVersionSuccess(string baseOutputDir, string version, string? createdBy)
    {
        var index = LoadIndex(baseOutputDir) ?? new FacadeVersionIndex();
        var entry = index.Versions.FirstOrDefault(v => v.Version == version);
        if (entry == null)
        {
            entry = new FacadeVersionEntry { Version = version, CreatedAt = DateTimeOffset.Now, CreatedBy = createdBy };
            index.Versions.Add(entry);
        }
        entry.StitchStatus = "OK";
        index.Current = version;
        SaveIndex(baseOutputDir, index);
    }

    /// <summary>Updates crack/report status on whichever version is currently
    /// `current`. No-op if no version_index.json exists yet (legacy facade,
    /// unversioned flat output — nothing to stamp status onto).</summary>
    public static void UpdateStageStatus(string baseOutputDir, string stage, string status)
    {
        var index = LoadIndex(baseOutputDir);
        if (index?.Current == null)
            return;
        var entry = index.Versions.FirstOrDefault(v => v.Version == index.Current);
        if (entry == null)
            return;
        switch (stage)
        {
            case "crack": entry.CrackStatus = status; break;
            case "report": entry.ReportStatus = status; break;
        }
        SaveIndex(baseOutputDir, index);
    }

    /// <summary>Resolves the dir that should actually be read/written: the
    /// current version's subfolder if versioning is active for this facade,
    /// else baseOutputDir itself (pre-versioning flat layout, untouched).</summary>
    public static string ResolveCurrentDir(string baseOutputDir)
    {
        var index = LoadIndex(baseOutputDir);
        if (index?.Current == null)
            return baseOutputDir;
        var candidate = Path.Combine(baseOutputDir, index.Current);
        return Directory.Exists(candidate) ? candidate : baseOutputDir;
    }

    /// <summary>All versions, most recent first, for display in the run-history list.</summary>
    public static List<RunVersionInfo> GetRunHistory(string baseOutputDir)
    {
        var index = LoadIndex(baseOutputDir);
        if (index == null)
            return new List<RunVersionInfo>();

        return index.Versions
            .Select(v => new RunVersionInfo(
                v.Version, v.CreatedAt, v.CreatedBy, v.StitchStatus, v.CrackStatus, v.ReportStatus,
                v.Version == index.Current))
            .Reverse()
            .ToList();
    }
}
