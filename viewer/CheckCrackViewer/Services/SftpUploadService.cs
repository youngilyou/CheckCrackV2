using System.IO;
using System.Net.Sockets;
using Renci.SshNet;
using Renci.SshNet.Common;

namespace CheckCrackViewer.Services;

/// <summary>Upload counterpart to SftpDownloadService -- used by MainViewModel.GenerateReport's
/// write-back to push the finished stitching-result zip and report PDF to the same SFTP host
/// the original image zip came from (see FacadeItemViewModel.RemoteZipPath), so a later download
/// (from any workstation, not just the one that ran the analysis) works the same way the
/// original zip download already does.
///
/// 2026-08-29: client.Connect() below really can throw SshOperationTimeoutException under real
/// CPU load on this workstation. Root cause and fix both confirmed by direct experiment, not
/// assumption:
/// - Reproduced with a dedicated load test: 32 logical CPUs saturated 2x oversubscribed with real
///   floating-point work, connect attempts at Normal thread priority failed 10/10 (every one hit
///   exactly the 30s default timeout).
/// - Measured one real facade's actual GPU-accelerated stitching pipeline (59 real drone photos,
///   ~800s, Get-Counter/nvidia-smi sampled every 2s): CPU averaged 20.5%, but hit sustained ~100%
///   for real (5% of samples) during its COLMAP stage -- so this isn't a synthetic-only concern,
///   a single facade's own pipeline really does have a CPU-saturated phase, and with up to
///   MaxConcurrent=5 concurrent facades on a GPU machine (see AnalysisConcurrencyManager/
///   GpuDetectionService) several such phases can overlap.
/// - A/B-tested the fix under that same 2x-oversubscribed load: 10 connects at Normal priority
///   (0/10 succeeded, all 30s timeouts) vs. 10 at AboveNormal priority alternating with them under
///   the identical load (9/10 succeeded, avg 17s) -- elevating the connecting thread's priority is
///   the confirmed fix, not a guess. Extending the connect timeout itself was tried earlier and
///   reverted: every failure under load hit the full 30s with no sign of "almost there", so there
///   was no evidence a longer wait would help. A bounded retry stays as a safety net for the
///   residual failure rate (1/10 in the A/B test) -- its delay is NOT similarly evidence-tuned
///   (unlike the priority fix above), just a short pause before trying again with the same
///   elevated-priority approach.</summary>
public static class SftpUploadService
{
    private const int MaxAttempts = 3;
    private static readonly TimeSpan RetryDelay = TimeSpan.FromSeconds(3);

    /// <param name="remoteDir">Created (recursively, best-effort per path segment) if it doesn't
    /// already exist -- SSH.NET's SftpClient has no mkdir -p, so each segment is created one at a
    /// time, ignoring "already exists" failures.</param>
    public static async Task UploadAsync(CrackVisionDbSettings settings, string localPath, string remoteDir, string remoteFileName,
        CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.SftpHost))
            throw new InvalidOperationException("SFTP host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB/SFTP 접속 정보를 입력하세요).");
        if (string.IsNullOrWhiteSpace(settings.SftpPassword))
            throw new InvalidOperationException("SFTP password가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB/SFTP 접속 정보를 입력하세요).");

        for (var attempt = 1; ; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var client = new SftpClient(settings.SftpHost, settings.SftpPort, settings.SftpUser, settings.SftpPassword);
            try
            {
                await Task.Run(() =>
                {
                    // Elevated for the duration of just this connect+upload, then restored below --
                    // this is a ThreadPool thread that gets reused for unrelated work afterward, so
                    // leaving it at AboveNormal permanently would leak the boost onto whatever runs
                    // on it next. See the class doc comment for the A/B measurement confirming this
                    // actually raises the connect success rate under real CPU contention (0/10 ->
                    // 9/10 in the same load condition), not just a plausible-sounding guess.
                    var originalPriority = Thread.CurrentThread.Priority;
                    Thread.CurrentThread.Priority = ThreadPriority.AboveNormal;
                    try
                    {
                        client.Connect();
                        EnsureRemoteDirectory(client, remoteDir);
                        var remotePath = remoteDir.TrimEnd('/') + "/" + remoteFileName;
                        using var localStream = File.OpenRead(localPath);
                        cancellationToken.ThrowIfCancellationRequested();
                        client.UploadFile(localStream, remotePath, true);
                    }
                    finally
                    {
                        // Best-effort cleanup only -- if Connect() itself is what failed (the
                        // common case under CPU load, per the 2026-08-29 load test above), the
                        // client was never actually connected, and calling Disconnect() on it can
                        // throw its own exception. That secondary exception must never replace/
                        // mask the real one already propagating from the try block, and must
                        // never itself break a successful attempt -- so it's swallowed here, not
                        // let loose to interfere with the catch (Exception ex) when (...) filter
                        // below (which needs to see the ORIGINAL exception's type).
                        try { if (client.IsConnected) client.Disconnect(); } catch { }
                        Thread.CurrentThread.Priority = originalPriority;
                    }
                }, cancellationToken);
                return;
            }
            catch (Exception ex) when (attempt < MaxAttempts && ex is SshOperationTimeoutException or SocketException or SshConnectionException)
            {
                await Task.Delay(RetryDelay, cancellationToken);
            }
        }
    }

    /// <summary>Returns the final uploaded remote path (remoteDir + "/" + remoteFileName) for
    /// convenience, so callers don't have to reassemble it themselves before writing it to DB.</summary>
    public static string RemotePathFor(string remoteDir, string remoteFileName) =>
        remoteDir.TrimEnd('/') + "/" + remoteFileName;

    private static void EnsureRemoteDirectory(SftpClient client, string remoteDir)
    {
        var parts = remoteDir.Trim('/').Split('/', StringSplitOptions.RemoveEmptyEntries);
        var current = "";
        foreach (var part in parts)
        {
            current += "/" + part;
            if (client.Exists(current))
                continue;
            try
            {
                client.CreateDirectory(current);
            }
            catch (Exception)
            {
                // Race with another workstation creating the same directory concurrently, or a
                // permissions quirk -- re-check existence; if it's there now, that's the only
                // thing that mattered.
                if (!client.Exists(current))
                    throw;
            }
        }
    }
}
