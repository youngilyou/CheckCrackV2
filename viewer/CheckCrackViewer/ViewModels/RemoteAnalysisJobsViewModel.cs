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
    private readonly Func<string, string, string, long?, string?, Task<bool>> _registerAndRun;
    private readonly Action<string, string>? _onProgress;
    private readonly Action? _onAssignmentReceived;
    private readonly string _rootPath;
    private readonly string _downloadRoot;
    private readonly string _extractRoot;

    // 2026-08-27: persisted across restarts -- see ProcessedArchiveStore's own doc comment for
    // why an in-memory-only HashSet would NOT catch the actual bug (TRANSIENT_LOCAL durability
    // replaying old AnalysisAssignment samples on every app restart, which is exactly when an
    // in-memory set would already be empty again).
    private readonly HashSet<long> _processedArchiveIds;

    public ObservableCollection<RemoteAnalysisJobViewModel> Jobs { get; } = new();

    /// <param name="registerAndRun">(extractedDir, company, building, archiveId) -> Task -- calls
    /// back into MainViewModel.RegisterAndRunExtractedArchiveAsync. archiveId lets GenerateReport
    /// later write analysis results back to this archive's crackvision_archives row.</param>
    /// <param name="onProgress">2026-08-27: (level, message) -- fired at every stage transition
    /// (수신됨/다운로드 중/압축 해제 중/분석 시작/완료/오류) so MainViewModel can mirror this into
    /// the top-bar "수신 상태" indicator and the LIVE LOG panel. Already marshaled to the UI
    /// thread by the time it's invoked (see Report below) -- MainViewModel's handler must NOT
    /// dispatch again. Null is fine (tests / no-op).</param>
    /// <param name="onAssignmentReceived">2026-08-28: fired once per genuinely-new assignment
    /// (after the dedup check, before any download starts) -- MainViewModel uses this to switch
    /// SelectedMenu to "analysis" so the operator sees the incoming job even if some other tab
    /// (설정 등) was open (operator request: "다른 창에 선택이 되어 있어도... 분석-스티칭 창으로
    /// 전환이 되어야함"). Deliberately NOT folded into onProgress -- that fires on every
    /// subsequent stage too, and repeatedly yanking the operator back to this tab while they've
    /// since navigated elsewhere on purpose would be worse than not switching at all.</param>
    public RemoteAnalysisJobsViewModel(AnalysisBridgeService bridge, Func<CrackVisionDbSettings> settingsProvider,
        Func<string, string, string, long?, string?, Task<bool>> registerAndRun, string rootPath,
        Action<string, string>? onProgress = null, Action? onAssignmentReceived = null)
    {
        _bridge = bridge;
        _settingsProvider = settingsProvider;
        _registerAndRun = registerAndRun;
        _onProgress = onProgress;
        _onAssignmentReceived = onAssignmentReceived;
        _rootPath = rootPath;
        _downloadRoot = Path.Combine(rootPath, "remote_downloads", "zips");
        _extractRoot = Path.Combine(rootPath, "remote_downloads", "extracted");
        _processedArchiveIds = ProcessedArchiveStore.Load(rootPath);
    }

    /// <summary>Call from AnalysisBridgeService.AssignmentReceived. Safe to call from any thread
    /// -- marshals its own UI-collection mutations.</summary>
    public void HandleAssignment(AnalysisAssignment assignment)
    {
        // 2026-08-27: dedup FIRST, before creating a job row or touching disk -- see
        // ProcessedArchiveStore's doc comment. Must be checked+recorded synchronously right here
        // (not inside ProcessAssignmentAsync after an await) so a burst of redelivered duplicates
        // arriving close together can't race each other into double-processing.
        if (!_processedArchiveIds.Add(assignment.ArchiveId))
        {
            Report("INFO", $"[중복 무시] archive #{assignment.ArchiveId} {assignment.Company}/{assignment.Building} "
                + "-- 이미 처리한 archive_id (DDS TRANSIENT_LOCAL 재전송으로 추정, 재실행하지 않음)");
            return;
        }
        ProcessedArchiveStore.Append(_rootPath, assignment.ArchiveId);
        RunOnUi(() => _onAssignmentReceived?.Invoke());

        var job = new RemoteAnalysisJobViewModel
        {
            ArchiveId = assignment.ArchiveId,
            Company = assignment.Company,
            Building = assignment.Building,
            DirectionsDisplay = string.Join(", ", assignment.Directions),
            ImageCount = assignment.ImageCount,
        };
        RunOnUi(() => Jobs.Insert(0, job));
        Report("INFO", $"[수신] archive #{assignment.ArchiveId} {assignment.Company}/{assignment.Building} "
            + $"({job.DirectionsDisplay}, {assignment.ImageCount}장)");
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
        var tag = $"#{assignment.ArchiveId} {assignment.Company}/{assignment.Building}";
        try
        {
            SetStatus(job, "다운로드 중");
            Report("INFO", $"[다운로드 시작] {tag}");
            _bridge.SendStatusUpdate(assignment.ArchiveId, "DOWNLOAD_START", "");
            var settings = _settingsProvider();
            var localZipPath = Path.Combine(_downloadRoot, $"{assignment.ArchiveId}.zip");
            var progress = new Progress<(long Downloaded, long Total)>(p =>
                RunOnUi(() => job.ProgressText = FormatProgress(p.Downloaded, p.Total)));
            await SftpDownloadService.DownloadAsync(settings, assignment.ZipRemotePath, localZipPath, progress);
            Report("INFO", $"[다운로드 완료] {tag}");

            SetStatus(job, "압축 해제 중");
            _bridge.SendStatusUpdate(assignment.ArchiveId, "EXTRACT_START", "");
            var extractDir = Path.Combine(_extractRoot,
                $"{SafeName(assignment.Company)}_{SafeName(assignment.Building)}_{assignment.ArchiveId}");
            if (Directory.Exists(extractDir))
                Directory.Delete(extractDir, recursive: true);
            Directory.CreateDirectory(Path.GetDirectoryName(extractDir)!);
            ZipFile.ExtractToDirectory(localZipPath, extractDir);
            _bridge.SendStatusUpdate(assignment.ArchiveId, "EXTRACT_DONE", "");
            Report("INFO", $"[압축 해제 완료] {tag}");

            SetStatus(job, "분석 시작");
            Report("INFO", $"[분석 시작] {tag}");
            _bridge.SendJobAccepted(assignment.ArchiveId, startedImmediately: true);
            _bridge.SendStatusUpdate(assignment.ArchiveId, "ANALYSIS_START", "");

            // 2026-08-27: must run on the UI thread, not this method's own thread (the native DDS
            // listener thread -- see HandleAssignment, which never dispatches before calling this
            // method). _registerAndRun (MainViewModel.RegisterAndRunExtractedArchiveAsync) mutates
            // Facades and other ObservableCollections bound to a live CollectionView; doing that
            // off the Dispatcher thread throws "이 형식의 CollectionView에서는 발송자 스레드와 다른
            // 스레드에서의 해당 SourceCollection에 대한 변경 내용을 지원하지 않습니다." (confirmed via
            // an actual remote-dispatch run, archive #39 -- the manual "실행" button never hit this
            // because a WPF Command execution is already on the UI thread by construction).
            var success = await RunOnUiAsync(() => _registerAndRun(extractDir, assignment.Company, assignment.Building, assignment.ArchiveId, assignment.ZipRemotePath));

            SetStatus(job, success ? "완료" : "완료 (일부 실패)");
            Report(success ? "INFO" : "WARNING", $"[{(success ? "완료" : "완료 (일부 실패)")}] {tag}");
            _bridge.SendResult(assignment.ArchiveId, success);
        }
        catch (Exception ex)
        {
            SetStatus(job, "오류");
            RunOnUi(() => job.ErrorMessage = ex.Message);
            Report("ERROR", $"[오류] {tag}: {ex.Message}");
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

    private void Report(string level, string message) => RunOnUi(() => _onProgress?.Invoke(level, message));

    private static void RunOnUi(Action action)
    {
        if (Application.Current?.Dispatcher.CheckAccess() ?? true)
            action();
        else
            Application.Current.Dispatcher.Invoke(action);
    }

    /// <summary>Like RunOnUi, but for an async func whose *entire* execution (not just its
    /// synchronous prefix) must stay on the UI thread -- awaiting Dispatcher.InvokeAsync alone
    /// only guarantees that up to func's first internal `await`; the outer await here is what
    /// actually waits for func's returned Task to complete (its continuations resume on the UI
    /// thread's SynchronizationContext since func was started there).</summary>
    private static async Task<T> RunOnUiAsync<T>(Func<Task<T>> func)
    {
        if (Application.Current?.Dispatcher.CheckAccess() ?? true)
            return await func();
        return await await Application.Current.Dispatcher.InvokeAsync(func);
    }
}
