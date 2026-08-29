using System.IO;
using System.Net.Sockets;
using Renci.SshNet;
using Renci.SshNet.Common;

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
/// would be unnecessary weight here.
///
/// 2026-08-29: client.Connect() can throw SshOperationTimeoutException under real CPU load on
/// this workstation, exactly like SftpUploadService's write-back connect -- same client library,
/// same handshake, same competing-for-CPU-with-concurrent-facades mechanism, confirmed by direct
/// experiment there (see SftpUploadService's class doc comment for the full A/B measurement: 0/10
/// success at Normal thread priority vs. 9/10 at AboveNormal, under identical CPU load). Applying
/// the same confirmed fix here -- this is actually the more consequential of the two spots to fix,
/// since a download failure kills the whole facade before analysis ever starts (a write-back
/// failure at least leaves the already-computed local results in place, just unrecorded in the
/// DB).</summary>
public static class SftpDownloadService
{
    private const int MaxAttempts = 3;
    private static readonly TimeSpan RetryDelay = TimeSpan.FromSeconds(3);

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

        for (var attempt = 1; ; attempt++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            using var client = CreateClient(settings);
            try
            {
                await Task.Run(() =>
                {
                    // See the class doc comment -- A/B-measured fix (0/10 -> 9/10 connect success
                    // under identical real CPU contention), not a guess. Restored in finally since
                    // this is a reused ThreadPool thread.
                    var originalPriority = Thread.CurrentThread.Priority;
                    Thread.CurrentThread.Priority = ThreadPriority.AboveNormal;
                    try
                    {
                        client.Connect();
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

                        try
                        {
                            using var localStream = File.Create(localPath);
                            client.DownloadFile(remotePath, localStream, downloaded =>
                            {
                                cancellationToken.ThrowIfCancellationRequested();
                                progress?.Report(((long)downloaded, total));
                            });
                        }
                        catch (SftpPathNotFoundException ex)
                        {
                            // 2026-08-27: 실제로 겪은 케이스 -- 오늘의 파일명/경로 수정(한글 파일명 보존
                            // + 절대경로 저장) 이전에 만들어진 아카이브는 DB에 옛 상대경로/뭉개진 파일명이
                            // 그대로 남아있어서 여기서 항상 404. 원본 SftpPathNotFoundException 메시지
                            // ("No such file. Path: ...")는 운영자에게 원인을 설명해주지 않으므로, 여기서
                            // 잡아서 "예전 형식" 프레이밍의 명확한 메시지로 바꿔치기(호출부의 기존
                            // catch(Exception)이 그대로 이 메시지를 표시함 -- 호출부 자체는 안 죽음, 여기
                            // 안 고쳐도 앱이 크래시하는 건 아니었지만 메시지가 안 친절했음). 실패 시 남는
                            // 0바이트짜리 로컬 zip도 같이 정리. 파일이 진짜로 없는 것뿐이라 재시도해도
                            // 달라지지 않음 -- InvalidOperationException으로 감싸서 아래 catch(Exception ex)
                            // when(... is SshOperationTimeoutException or ...)의 재시도 대상 타입에도
                            // 안 걸리게 하고 즉시 전파.
                            try { File.Delete(localPath); } catch (Exception) { /* best-effort cleanup */ }
                            throw new InvalidOperationException(
                                "원격 파일을 찾을 수 없습니다 -- 예전 형식으로 저장된 아카이브일 수 있습니다 " +
                                "(2026-08-27 이전 아카이브는 파일명/경로 형식이 달라 다운로드가 안 될 수 있습니다). " +
                                $"새로 저장한 아카이브로 다시 시도해 주세요. (경로: {remotePath})", ex);
                        }
                    }
                    finally
                    {
                        // Best-effort: if Connect() itself is what failed, the client was never
                        // actually connected and Disconnect() could throw its own exception that
                        // would otherwise mask the real one -- see SftpUploadService's identical
                        // comment for the full reasoning.
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

    // Password auth only (2026-08-27 operator decision) -- no private-key option here.
    private static SftpClient CreateClient(CrackVisionDbSettings settings) =>
        new(settings.SftpHost, settings.SftpPort, settings.SftpUser, settings.SftpPassword);
}
