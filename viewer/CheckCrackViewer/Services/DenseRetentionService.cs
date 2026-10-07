using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>One facade whose local data (archive folder: photos + output incl. Dense) is kept on this
/// workstation until its archive's contract-end cleanup is due.</summary>
public sealed class DenseRetentionEntry
{
    [JsonPropertyName("archive_id")] public long ArchiveId { get; set; }
    [JsonPropertyName("facade_id")] public string FacadeId { get; set; } = "";
    /// <summary>The facade's BASE output folder (holds V001, V002, ...) -- e.g. ...\{회사}_{동}_{archive}\BACK\output.</summary>
    [JsonPropertyName("base_output_dir")] public string BaseOutputDir { get; set; } = "";
    [JsonPropertyName("registered_at")] public DateTime RegisteredAt { get; set; }
    [JsonPropertyName("deleted_at")] public DateTime? DeletedAt { get; set; }
    [JsonPropertyName("bytes_freed")] public long? BytesFreed { get; set; }
}

/// <summary>Local retention of remote-analysis archives on this workstation (registry:
/// RootPath\dense_retention.json).
///
/// 2026-10-07 (사용자 확정): the local output (incl. Dense Stereo data) is NOT deleted while the contract
/// runs -- the viewer loads it as is ("서버에서 결과 불러오기" fetches the server copy on demand instead).
/// When MngData marks the archive for contract-end cleanup, the whole local archive folder (BACK's parent:
/// ...\{회사}_{동}_{archive_id}\ with the photos and every facade's output) is deleted and the facade's
/// dense_storage record is removed from crackvision_archives.facade_analysis_results.
///
/// Deletion rule (mirrors MngData schemas/crackvision_storage.sql): retention_state = 'cleaned', or
/// 'cleanup_pending' with cleanup_due_at reached. 'active', an unknown state, a missing archive row or a
/// DB error -> keep (never delete on missing information). Safety: the folder is deleted only if its name
/// ends with "_{archive_id}" (the extract naming both download paths use); otherwise only the Dense folders
/// are removed and a warning is logged.</summary>
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

    /// <summary>Records (or refreshes) that this facade's local data is kept here. Re-analysis/re-load of the
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
                bytes += FolderSize(d);
            }
        }
        return (dirs, bytes);
    }

    private static long FolderSize(string dir) =>
        Directory.Exists(dir) ? new DirectoryInfo(dir).EnumerateFiles("*", SearchOption.AllDirectories).Sum(f => f.Length) : 0;

    /// <summary>...\{회사}_{동}_{archive}\BACK\output -> ...\{회사}_{동}_{archive}, or null when the layout
    /// doesn't match (then only the Dense folders are removed).</summary>
    public static string? ArchiveFolderOf(DenseRetentionEntry e)
    {
        var facadeDir = Directory.GetParent(e.BaseOutputDir.TrimEnd('\\', '/'));
        var archiveDir = facadeDir?.Parent;
        if (archiveDir == null || !archiveDir.Name.EndsWith($"_{e.ArchiveId}", StringComparison.Ordinal))
            return null;
        return archiveDir.FullName;
    }

    /// <summary>Checks every not-yet-deleted entry against MngData and deletes the local data of archives
    /// whose cleanup is due. Returns log lines (level, message).</summary>
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
            // 같은 archive 폴더의 다른 면을 처리하면서 이미 정리된 항목은 건너뛴다.
            if (Load(rootPath).Any(x => x.DeletedAt != null && x.ArchiveId == e.ArchiveId
                    && string.Equals(x.FacadeId, e.FacadeId, StringComparison.OrdinalIgnoreCase)
                    && string.Equals(x.BaseOutputDir, e.BaseOutputDir, StringComparison.OrdinalIgnoreCase)))
                continue;
            if (!states.TryGetValue(e.ArchiveId, out var st))
            {
                log.Add(("WARNING", $"[보관 정리] archive #{e.ArchiveId} {e.FacadeId}: DB에 archive가 없어 판단 불가 -- 삭제하지 않음"));
                continue;
            }
            var due = st.State == "cleaned"
                || (st.State == "cleanup_pending" && st.CleanupDueAt is DateTime dueAt && dueAt.ToUniversalTime() <= now);
            if (!due)
                continue;

            long freed = 0;
            var archiveFolder = ArchiveFolderOf(e);
            string what;
            if (archiveFolder != null && Directory.Exists(archiveFolder))
            {
                freed = FolderSize(archiveFolder);
                Directory.Delete(archiveFolder, recursive: true);
                what = $"archive 폴더 삭제 ({archiveFolder})";
            }
            else if (archiveFolder != null)
            {
                what = $"archive 폴더 이미 없음 ({archiveFolder})";
            }
            else
            {
                foreach (var dir in Measure(e.BaseOutputDir).Dirs)
                {
                    freed += FolderSize(dir);
                    Directory.Delete(dir, recursive: true);
                }
                what = $"폴더 구조가 예상과 달라 Dense 폴더만 삭제 ({e.BaseOutputDir})";
                log.Add(("WARNING", $"[보관 정리] archive #{e.ArchiveId} {e.FacadeId}: {what}"));
            }

            // DB의 Dense 저장 위치 기록 삭제 (같은 archive 폴더에 있던 다른 면들도).
            var sameFolder = Load(rootPath).Where(x => x.DeletedAt == null && x.ArchiveId == e.ArchiveId
                && (archiveFolder == null ? x == e || string.Equals(x.BaseOutputDir, e.BaseOutputDir, StringComparison.OrdinalIgnoreCase)
                                          : string.Equals(ArchiveFolderOf(x), archiveFolder, StringComparison.OrdinalIgnoreCase)))
                .ToList();
            if (sameFolder.Count == 0)
                sameFolder.Add(e);
            foreach (var x in sameFolder)
            {
                try
                {
                    await CrackVisionArchiveQueryService.ClearDenseStorageAsync(settings, x.ArchiveId, x.FacadeId, cancellationToken);
                }
                catch (Exception ex)
                {
                    log.Add(("WARNING", $"[보관 정리] archive #{x.ArchiveId} {x.FacadeId}: DB의 Dense 위치 기록 삭제 실패 -- {ex.Message}"));
                }
            }

            lock (Gate)
            {
                var entries = Load(rootPath);
                foreach (var x in entries.Where(x => sameFolder.Any(s => s.ArchiveId == x.ArchiveId
                             && string.Equals(s.FacadeId, x.FacadeId, StringComparison.OrdinalIgnoreCase)
                             && string.Equals(s.BaseOutputDir, x.BaseOutputDir, StringComparison.OrdinalIgnoreCase))))
                {
                    x.DeletedAt = now;
                    x.BytesFreed = freed;
                }
                Save(rootPath, entries);
            }
            log.Add(("INFO", $"[보관 정리] archive #{e.ArchiveId}: 계약 종료 정리({st.State}) -- {what}, {freed / 1e9:F1} GB"));
        }
        return log;
    }
}
