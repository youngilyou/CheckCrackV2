using System.IO;
using Renci.SshNet;

namespace CheckCrackViewer.Services;

/// <summary>Downloads a facade archive zip from the SFTP host (see CrackVisionDbSettings) to a
/// local file. Used by both the automatic (AnalysisAssignment.zip_remote_path) and manual
/// (operator-picked archive_id -> zip_path from crackvision_archives) paths -- the transfer
/// mechanism itself doesn't care which path triggered it.
///
/// SFTP was chosen over HTTP (see AnalysisLoadBalancer README "다운로드 방식"): this ecosystem
/// already has SSH exposed on the archive host, so no new download endpoint/auth surface is
/// needed on the MngData/backend_core side, and SSH.NET's SftpClient is simple enough for a
/// single-file pull that vendoring rsync.exe (as FacadePreviewer does for its push direction)
/// would be unnecessary weight here.</summary>
public static class SftpDownloadService
{
    /// <param name="progress">Reports (bytesDownloaded, totalBytes) -- totalBytes may be 0 if
    /// unknown at the time of the first callback.</param>
    public static async Task DownloadAsync(CrackVisionDbSettings settings, string remotePath, string localPath,
        IProgress<(long BytesDownloaded, long TotalBytes)>? progress = null, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(settings.SftpHost))
            throw new InvalidOperationException("SFTP host가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB/SFTP 접속 정보를 입력하세요).");
        // Password auth only (no private-key option) -- a blank password here means the actual
        // SSH.NET connect below would throw a raw SshAuthenticationException instead of this
        // clear, actionable message. SaveCrackVisionSettings already blocks saving a blank
        // password, but this still catches settings.json files saved before that check existed.
        if (string.IsNullOrWhiteSpace(settings.SftpPassword))
            throw new InvalidOperationException("SFTP password가 설정되지 않았습니다 (설정 화면에서 CrackVisionDB/SFTP 접속 정보를 입력하세요).");

        using var client = CreateClient(settings);
        await Task.Run(() =>
        {
            client.Connect();
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(localPath) ?? ".");
                long total = 0;
                try
                {
                    total = client.GetAttributes(remotePath).Size;
                }
                catch (Exception)
                {
                    // Attribute lookup failing isn't fatal -- DownloadFile below still works,
                    // progress just reports TotalBytes=0 until the transfer itself starts.
                }

                using var localStream = File.Create(localPath);
                client.DownloadFile(remotePath, localStream, downloaded =>
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    progress?.Report(((long)downloaded, total));
                });
            }
            finally
            {
                client.Disconnect();
            }
        }, cancellationToken);
    }

    // Password auth only (2026-08-27 operator decision) -- no private-key option here.
    private static SftpClient CreateClient(CrackVisionDbSettings settings) =>
        new(settings.SftpHost, settings.SftpPort, settings.SftpUser, settings.SftpPassword);
}
