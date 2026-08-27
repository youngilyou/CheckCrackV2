using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>Persists the set of archive_ids this workstation has already accepted from a remote
/// AnalysisAssignment (see RemoteAnalysisJobsViewModel), across app restarts.
///
/// Why this needs to survive a restart, not just an in-memory HashSet: CheckCrackDdsBridge's
/// assignment_reader is RELIABLE + TRANSIENT_LOCAL (see AnalysisBridge.cpp's own comment on why --
/// "the operator-facing outcome must not be lost even if the reader's participant hasn't finished
/// matching yet"). That QoS choice has a real side effect: every time this app restarts, its new
/// DDS participant re-matches AnalysisLoadBalancer's assignment writer, and DDS durability replays
/// the writer's still-cached history (last ~20 samples) -- even though AnalysisLoadBalancer/
/// FacadePreviewer never actually sent anything new. Confirmed in practice (2026-08-27): restarting
/// CheckCrackViewer during this session's own debugging repeatedly re-delivered archive #39/#40's
/// AnalysisAssignment, silently re-downloading/re-extracting/re-running them from scratch on every
/// relaunch -- exactly why deleted images kept reappearing. An in-memory-only guard would reset on
/// each restart, i.e. right when the replay actually happens, so it wouldn't help at all.
///
/// Deliberately NOT reusing FacadeHierarchyStore's JSON format -- this is a flat set of longs with
/// no need for richer structure, so a plain newline-per-id text file (same convention as
/// DDS-Router's revoked_serials.txt) is simplest.</summary>
public static class ProcessedArchiveStore
{
    private const string FileName = "processed_archive_ids.txt";

    private static string FilePath(string rootPath) =>
        Path.Combine(rootPath, "remote_downloads", FileName);

    // Never throws -- a missing/corrupt file just means "nothing recorded yet", same fallback
    // behavior as every other *Store class in this project.
    public static HashSet<long> Load(string rootPath)
    {
        var result = new HashSet<long>();
        try
        {
            var path = FilePath(rootPath);
            if (!File.Exists(path))
                return result;
            foreach (var line in File.ReadAllLines(path))
                if (long.TryParse(line.Trim(), out var id))
                    result.Add(id);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
        return result;
    }

    // Append-only, best-effort -- a failure to persist this must never block the caller from
    // proceeding with (or skipping) the actual download/extract/run work.
    public static void Append(string rootPath, long archiveId)
    {
        try
        {
            var path = FilePath(rootPath);
            var dir = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);
            File.AppendAllLines(path, new[] { archiveId.ToString() });
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }
}
