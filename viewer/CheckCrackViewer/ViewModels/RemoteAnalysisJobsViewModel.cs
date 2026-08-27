using System.Collections.ObjectModel;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Windows;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.ViewModels;

/// <summary>Backs the "원격 분석 작업" window: a live-updating list of AnalysisAssignments
/// received from FacadePreviewer (via AnalysisBridgeService), each carried through
/// download(SFTP) -> extract(zip) -> register+run(MainViewModel) -> result, entirely
/// automatically -- see https://github.com/youngilyou/AnalysisLoadBalancer README and the
/// 2026-08-27 session note: "FacadePreviewer에서 분석 실행 명령을 받으면 CheckCrackViewer에서
/// 별도의 창에 리스트로 표시되어야 함... Unzip된 항목은 자동으로 분석/스티칭 창의 왼쪽 리스트
/// UI에 자동 등록... 수동은 관리자가 선택" -- this class is purely a live DISPLAY list (no
/// persistence of its own), distinct from the manual browse/download path (관리자가 직접
/// archive_id를 선택하는 것, see SettingsView) which this class does not drive.
///
/// IMPORTANT simplification (documented, not hidden): a single archive can expand into several
/// facades (one per direction subfolder) once extracted. Each facade is gated independently
/// through MainViewModel's own AnalysisConcurrencyManager (same gate the manual "실행" button
/// uses) -- this class does NOT acquire a second, archive-level slot before calling into
/// MainViewModel (doing so would deadlock when MaxConcurrent==1 and a single-direction archive
/// tries to acquire a slot it's already effectively holding). Consequently AnalysisJobAccepted
/// is sent unconditionally right after extraction (this pass does not distinguish "started
/// immediately" from "queued" at the archive level -- see AnalysisJobAccepted's own IDL comment
/// for the field this simplifies away), and AnalysisResult.success reflects only "extraction and
/// registration succeeded and the pipeline was invoked without throwing", not a verified
/// per-facade pipeline outcome (MainViewModel.RunFacade already logs its own per-facade
/// success/failure into that facade's Issues list independently).</summary>
public sealed class RemoteAnalysisJobsViewModel
{
    private readonly AnalysisBridgeService _bridge;
    private readonly Func<CrackVisionDbSettings> _settingsProvider;
    private readonly Func<string, string, string, Task<bool>> _registerAndRun;
    private readonly string _downloadRoot;
    private readonly string _extractRoot;

    public ObservableCollection<RemoteAnalysisJobViewModel> Jobs { get; } = new();

    /// <param name="registerAndRun">(extractedDir, company, building) -> Task -- calls back into
    /// MainViewModel.RegisterAndRunExtractedArchiveAsync.</param>
    public RemoteAnalysisJobsViewModel(AnalysisBridgeService bridge, Func<CrackVisionDbSettings> settingsProvider,
        Func<string, string, string, Task<bool>> registerAndRun, string rootPath)
    {
        _bridge = bridge;
        _settingsProvider = settingsProvider;
        _registerAndRun = registerAndRun;
        _downloadRoot = Path.Combine(rootPath, "remote_downloads", "zips");
        _extractRoot = Path.Combine(rootPath, "remote_downloads", "extracted");
    }

    /// <summary>Call from AnalysisBridgeService.AssignmentReceived. Safe to call from any thread
    /// -- marshals its own UI-collection mutations.</summary>
    public void HandleAssignment(AnalysisAssignment assignment)
    {
        var job = new RemoteAnalysisJobViewModel
        {
            ArchiveId = assignment.ArchiveId,
            Company = assignment.Company,
            Building = assignment.Building,
            DirectionsDisplay = string.Join(", ", assignment.Directions),
            ImageCount = assignment.ImageCount,
        };
        RunOnUi(() => Jobs.Insert(0, job));
        _ = ProcessAssignmentAsync(assignment, job);
    }

    /// <summary>Records that an AnalysisRetryRequest/AnalysisStopRequest arrived for
    /// archiveId, on whichever job row is still tracking it (no-op if that job has already
    /// scrolled out of memory or was never seen this session). See MainViewModel's own comment
    /// on why the underlying action isn't actually implemented yet.</summary>
    public void MarkControlReceived(long archiveId, string note)
    {
        RunOnUi(() =>
        {
            var job = Jobs.FirstOrDefault(j => j.ArchiveId == archiveId);
            if (job != null)
                job.ErrorMessage = note;
        });
    }

    private async Task ProcessAssignmentAsync(AnalysisAssignment assignment, RemoteAnalysisJobViewModel job)
    {
        try
        {
            SetStatus(job, "다운로드 중");
            _bridge.SendStatusUpdate(assignment.ArchiveId, "DOWNLOAD_START", "");
            var settings = _settingsProvider();
            var localZipPath = Path.Combine(_downloadRoot, $"{assignment.ArchiveId}.zip");
            var progress = new Progress<(long Downloaded, long Total)>(p =>
                RunOnUi(() => job.ProgressText = FormatProgress(p.Downloaded, p.Total)));
            await SftpDownloadService.DownloadAsync(settings, assignment.ZipRemotePath, localZipPath, progress);

            SetStatus(job, "압축 해제 중");
            _bridge.SendStatusUpdate(assignment.ArchiveId, "EXTRACT_START", "");
            var extractDir = Path.Combine(_extractRoot,
                $"{SafeName(assignment.Company)}_{SafeName(assignment.Building)}_{assignment.ArchiveId}");
            if (Directory.Exists(extractDir))
                Directory.Delete(extractDir, recursive: true);
            Directory.CreateDirectory(Path.GetDirectoryName(extractDir)!);
            ZipFile.ExtractToDirectory(localZipPath, extractDir);
            _bridge.SendStatusUpdate(assignment.ArchiveId, "EXTRACT_DONE", "");

            SetStatus(job, "분석 시작");
            _bridge.SendJobAccepted(assignment.ArchiveId, startedImmediately: true);
            _bridge.SendStatusUpdate(assignment.ArchiveId, "ANALYSIS_START", "");

            var success = await _registerAndRun(extractDir, assignment.Company, assignment.Building);

            SetStatus(job, success ? "완료" : "완료 (일부 실패)");
            _bridge.SendResult(assignment.ArchiveId, success);
        }
        catch (Exception ex)
        {
            SetStatus(job, "오류");
            RunOnUi(() => job.ErrorMessage = ex.Message);
            _bridge.SendErrorNotify(assignment.ArchiveId, job.Status, ex.Message);
            _bridge.SendResult(assignment.ArchiveId, success: false);
        }
    }

    private static string FormatProgress(long downloaded, long total)
    {
        var downloadedMb = downloaded / 1024.0 / 1024.0;
        return total > 0 ? $"{downloadedMb:F1} MB / {total / 1024.0 / 1024.0:F1} MB" : $"{downloadedMb:F1} MB";
    }

    private static string SafeName(string s) =>
        string.Concat(s.Select(c => Path.GetInvalidFileNameChars().Contains(c) ? '_' : c));

    private void SetStatus(RemoteAnalysisJobViewModel job, string status) => RunOnUi(() => job.Status = status);

    private static void RunOnUi(Action action)
    {
        if (Application.Current?.Dispatcher.CheckAccess() ?? true)
            action();
        else
            Application.Current.Dispatcher.Invoke(action);
    }
}
