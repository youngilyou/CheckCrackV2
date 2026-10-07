using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>One facade whose Dense Stereo data stays on this workstation (see
/// AnalysisResultUploadService) until its archive's contract-end cleanup is due.</summary>
public sealed class DenseRetentionEntry
{
    [JsonPropertyName("archive_id")] public long ArchiveId { get; set; }
    [JsonPropertyName("facade_id")] public string FacadeId { get; set; } = "";
    /// <summary>The facade's BASE output folder (holds V001, V002, ...) -- every version's Dense
    /// folders are removed at cleanup, not only the latest.</summary>
    [JsonPropertyName("base_output_dir")] public string BaseOutputDir { get; set; } = "";
    [JsonPropertyName("registered_at")] public DateTime RegisteredAt { get; set; }
    [JsonPropertyName("deleted_at")] public DateTime? DeletedAt { get; set; }
    [JsonPropertyName("bytes_freed")] public long? BytesFreed { get; set; }
}

/// <summary>2026-10-07 (사용자 확정): the Dense Stereo data of a remote-analysis facade (`colmap_dense/`,
/// `colmap_stage1/` in each version folder, ~10 GB per facade) is NOT uploaded to MngData -- it stays
/// here so re-analysis/review never has to rebuild it (5-6 h) or download + unzip it. While the contract
/// runs it is kept; once MngData marks the archive for contract-end cleanup it is deleted here.
///
/// Deletion rule (mirrors MngData schemas/crackvision_storage.sql's lifecycle): retention_state =
/// 'cleaned', or 'cleanup_pending' with cleanup_due_at reached (the grace period after the contract end
/// is MngData's to set). 'active', an unknown state, or an archive row that no longer exists -> keep
/// (never delete on missing information). Registry: RootPath\dense_retention.json (local, per workstation).</summary>
public static class DenseRetentionService
{
    private const string FileName = "dense_retention.json";
    private static readonly string[] DenseFolderNames = { "colmap_dense", "colmap_stage1" };
    private static readonly object Gate = new();

    private static string RegistryPath(string rootPath) => Path.Combine(rootPath, FileName);

    public static List<DenseRetentionEntry> Load(string rootPath)
    {
        lock (Gate)
        {
            try
            {
                var path = RegistryPath(rootPath);
                if (!File.Exists(path))
                    return new List<DenseRetentionEntry>();
                return JsonSerializer.Deserialize<List<DenseRetentionEntry>>(File.ReadAllText(path)) ?? new();
            }
            catch (Exception)
            {
                return new List<DenseRetentionEntry>();
            }
        }
    }

    private static void Save(string rootPath, List<DenseRetentionEntry> entries)
    {
        lock (Gate)
        {
            var path = RegistryPath(rootPath);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(entries, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(tmp, path, overwrite: true);
        }
    }

    /// <summary>Records (or refreshes) that this facade's Dense data is kept here. Re-analysis of the
    /// same archive/facade re-arms an entry that was already cleaned.</summary>
    public static void Register(string rootPath, long archiveId, string facadeId, string baseOutputDir)
    {
        lock (Gate)
        {
            var entries = Load(rootPath);
            var e = entries.FirstOrDefault(x => x.ArchiveId == archiveId
                && string.Equals(x.FacadeId, facadeId, StringComparison.OrdinalIgnoreCase)
                && string.Equals(x.BaseOutputDir, baseOutputDir, StringComparison.OrdinalIgnoreCase));
            if (e == null)
            {
                e = new DenseRetentionEntry { ArchiveId = archiveId, FacadeId = facadeId, BaseOutputDir = baseOutputDir };
                entries.Add(e);
            }
            e.RegisteredAt = DateTime.UtcNow;
            e.DeletedAt = null;
            e.BytesFreed = null;
            Save(rootPath, entries);
        }
    }

    /// <summary>Dense folders currently on disk for a facade (all versions) and their total size --
    /// what gets recorded in the DB as dense_storage.</summary>
    public static (List<string> Dirs, long Bytes) Measure(string baseOutputDir)
    {
        var dirs = new List<string>();
        long bytes = 0;
        if (!Directory.Exists(baseOutputDir))
            return (dirs, 0);
        foreach (var versionDir in Directory.EnumerateDirectories(baseOutputDir))
        {
            foreach (var name in DenseFolderNames)
            {
                var d = Path.Combine(versionDir, name);
                if (!Directory.Exists(d))
                    continue;
                dirs.Add(d);
                bytes += new DirectoryInfo(d).EnumerateFiles("*", SearchOption.AllDirectories).Sum(f => f.Length);
            }
        }
        return (dirs, bytes);
    }

    /// <summary>Checks every not-yet-deleted entry against MngData and deletes the Dense folders of
    /// archives whose cleanup is due. Returns human-readable log lines (level, message).</summary>
    public static async Task<List<(string Level, string Message)>> RunOnceAsync(string rootPath,
        CrackVisionDbSettings settings, CancellationToken cancellationToken = default)
    {
        var log = new List<(string, string)>();
        var pending = Load(rootPath).Where(e => e.DeletedAt == null).ToList();
        if (pending.Count == 0 || string.IsNullOrWhiteSpace(settings.PostgresHost))
            return log;

        var states = await CrackVisionArchiveQueryService.GetRetentionStatesAsync(
            settings, pending.Select(e => e.ArchiveId).Distinct().ToList(), cancellationToken);
        var now = DateTime.UtcNow;
        foreach (var e in pending)
        {
            if (!states.TryGetValue(e.ArchiveId, out var st))
            {
                log.Add(("WARNING", $"[Dense 보관] archive #{e.ArchiveId} {e.FacadeId}: DB에 archive가 없어 판단 불가 -- 삭제하지 않음"));
                continue;
            }
            var due = st.State == "cleaned"
                || (st.State == "cleanup_pending" && st.CleanupDueAt is DateTime dueAt && dueAt.ToUniversalTime() <= now);
            if (!due)
                continue;

            long freed = 0;
            foreach (var dir in Measure(e.BaseOutputDir).Dirs)
            {
                var size = new DirectoryInfo(dir).EnumerateFiles("*", SearchOption.AllDirectories).Sum(f => f.Length);
                Directory.Delete(dir, recursive: true);
                freed += size;
            }
            lock (Gate)
            {
                var entries = Load(rootPath);
                foreach (var x in entries.Where(x => x.ArchiveId == e.ArchiveId
                             && string.Equals(x.FacadeId, e.FacadeId, StringComparison.OrdinalIgnoreCase)
                             && string.Equals(x.BaseOutputDir, e.BaseOutputDir, StringComparison.OrdinalIgnoreCase)))
                {
                    x.DeletedAt = now;
                    x.BytesFreed = freed;
                }
                Save(rootPath, entries);
            }
            log.Add(("INFO", $"[Dense 보관] archive #{e.ArchiveId} {e.FacadeId}: 계약 종료 정리({st.State}) -- "
                + $"Dense 데이터 {freed / 1e9:F1} GB 삭제 ({e.BaseOutputDir})"));
        }
        return log;
    }
}
