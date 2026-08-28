using System.IO;
using Renci.SshNet;

namespace CheckCrackViewer.Services;

/// <summary>Upload counterpart to SftpDownloadService -- used by MainViewModel.GenerateReport's
/// write-back to push the finished stitching-result zip and report PDF to the same SFTP host
/// the original image zip came from (see FacadeItemViewModel.RemoteZipPath), so a later download
/// (from any workstation, not just the one that ran the analysis) works the same way the
/// original zip download already does.</summary>
public static class SftpUploadService
{
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

        using var client = new SftpClient(settings.SftpHost, settings.SftpPort, settings.SftpUser, settings.SftpPassword);
        await Task.Run(() =>
        {
            client.Connect();
            try
            {
                EnsureRemoteDirectory(client, remoteDir);
                var remotePath = remoteDir.TrimEnd('/') + "/" + remoteFileName;
                using var localStream = File.OpenRead(localPath);
                cancellationToken.ThrowIfCancellationRequested();
                client.UploadFile(localStream, remotePath, true);
            }
            finally
            {
                client.Disconnect();
            }
        }, cancellationToken);
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
