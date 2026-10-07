using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

/// <summary>2026-10-07: which CrackVisionDB archive a facade's results belong to, stored next to the
/// results ({base output dir}\archive_link.json) instead of only in memory.
///
/// Before this, FacadeItemViewModel.ArchiveId/RemoteZipPath were set when a remote (or manually
/// downloaded) archive was registered and lived only in memory -- after a viewer restart, a report
/// regenerated after the operator's review edits never reached the server (the write-back is gated on
/// ArchiveId). The link is written once at registration and read back whenever a report is generated.</summary>
public static class ArchiveLinkStore
{
    private const string FileName = "archive_link.json";

    public sealed class ArchiveLink
    {
        [JsonPropertyName("archive_id")] public long ArchiveId { get; set; }
        [JsonPropertyName("remote_zip_path")] public string? RemoteZipPath { get; set; }
        [JsonPropertyName("linked_at")] public DateTime LinkedAt { get; set; }
    }

    public static void Save(string baseOutputDir, long archiveId, string? remoteZipPath)
    {
        try
        {
            Directory.CreateDirectory(baseOutputDir);
            var path = Path.Combine(baseOutputDir, FileName);
            var tmp = path + ".tmp";
            File.WriteAllText(tmp, JsonSerializer.Serialize(new ArchiveLink
            {
                ArchiveId = archiveId, RemoteZipPath = remoteZipPath, LinkedAt = DateTime.UtcNow,
            }, new JsonSerializerOptions { WriteIndented = true }));
            File.Move(tmp, path, overwrite: true);
        }
        catch (Exception)
        {
            // best effort -- the in-memory link still works for this session
        }
    }

    public static ArchiveLink? TryLoad(string baseOutputDir)
    {
        try
        {
            var path = Path.Combine(baseOutputDir, FileName);
            return File.Exists(path) ? JsonSerializer.Deserialize<ArchiveLink>(File.ReadAllText(path)) : null;
        }
        catch (Exception)
        {
            return null;
        }
    }
}
