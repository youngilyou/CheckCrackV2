using System.Collections.ObjectModel;
using CommunityToolkit.Mvvm.ComponentModel;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.ViewModels;

/// <summary>One row in the facade list — aggregates everything known about a
/// single facade from its report JSON files and the live pipeline log.</summary>
public partial class FacadeItemViewModel : ObservableObject
{
    [ObservableProperty] private string _facadeId = "";
    [ObservableProperty] private FacadeOverallStatus _overallStatus = FacadeOverallStatus.Unknown;
    [ObservableProperty] private string _currentStageLabel = "대기 중";

    // --- 단지/동/방위 분류 (표시용 — FacadeId 자체의 유일성/의미는 안 바뀜,
    // FacadeHierarchyStore가 관리. 미분류 기본값. CLAUDE.local.md #4/#7:
    // 방향은 별도 metadata 태그일 뿐 실제 Stitching ID가 아니다) ---
    [ObservableProperty] private string _complexId = "__UNSORTED__";
    [ObservableProperty] private string _complexName = "미분류";
    [ObservableProperty] private string? _buildingId;
    [ObservableProperty] private string? _buildingName;
    [ObservableProperty] private string _side = "";

    // --- run-from-viewer (Browse 버튼으로 추가된 폴더) ---
    [ObservableProperty] private string? _sourceFolderPath;
    [ObservableProperty] private bool _isRunning;
    [ObservableProperty] private string? _livePreviewImagePath;

    // 2026-08-28: 원격/수동 CrackVisionDB 경로로 등록된 facade만 채워짐(RegisterExtractedArchive의
    // archiveId 인자, MainViewModel.AddRunnableCandidate가 전달) -- 순수 Browse로 추가된 facade는
    // 대응하는 archive_id가 없으므로 계속 null. GenerateReport 성공 시 이 값이 있으면
    // crackvision_archives의 해당 row에 stitching_zip_path/report_path/analysis_status를
    // write-back한다(MainViewModel.GenerateReport 참고). 방향이 여러 개(archive 하나에 facade
    // 여러 개)인 경우 전부 같은 archive_id를 공유 -- 이 경우 write-back은 마지막으로 완료된
    // facade 것이 최종 반영됨(알려진 단순화, 지금 실사용 archive는 전부 방향 1개뿐).
    [ObservableProperty] private long? _archiveId;

    // ArchiveId와 함께 채워짐(같은 등록 경로) -- GenerateReport가 스티칭/보고서 업로드 대상 원격
    // 폴더를 이 zip의 형제 디렉터리(analysis_results/)로 계산하는 데 씀. 원본 zip 자체를
    // 재사용/재업로드하지는 않음, 경로 계산용.
    [ObservableProperty] private string? _remoteZipPath;

    // --- homography-chain stitch (always produced) ---
    [ObservableProperty] private int _imageCount;
    [ObservableProperty] private double? _coverageRatio;
    [ObservableProperty] private double? _meanInlierRatio;
    [ObservableProperty] private double? _globalDriftScorePx;
    [ObservableProperty] private bool _needsColmapFallback;

    // --- COLMAP fallback (only when drift triggers it) ---
    [ObservableProperty] private bool _hasColmapReport;
    [ObservableProperty] private int _colmapRequested;
    [ObservableProperty] private int _colmapRegistered;
    [ObservableProperty] private double? _colmapMeanReprojectionErrorPx;

    // --- COLMAP-rectified mosaic (only when the above succeeded) ---
    [ObservableProperty] private bool _hasRectifiedMosaic;
    [ObservableProperty] private double? _coverageRatioColmap;

    // --- crack segmentation (only if {facade_id}_cracks.json exists) ---
    [ObservableProperty] private bool _hasCrackResults;
    [ObservableProperty] private int _crackCount;
    [ObservableProperty] private bool _isDetectingCracks;

    // --- PDF report (only if {facade_id}_report.pdf exists) ---
    [ObservableProperty] private bool _hasReport;
    [ObservableProperty] private string? _reportPath;
    [ObservableProperty] private bool _isGeneratingReport;

    // 2026-08-29: {facade_id}_quality_flags.json (FacadeQualityFlagsStore) 참고 -- OutputDir은
    // MainViewModel.ApplySnapshot이 FacadeSnapshot.OutputDir에서 복사(체크박스 토글 시 저장
    // 경로 계산용, GetFacadeOutputDir()가 계산하는 "베이스" 경로가 아니라 실제 버전 폴더까지
    // resolve된 경로가 필요해서 매번 다시 계산하는 대신 스냅샷이 이미 아는 값을 그대로 씀).
    // NeedsRetake(누락 재촬영 필요)는 순수 운영자 판단, NeedsDetailCapture(정밀촬영 필요)는
    // 앱의 초기 자동 의심 + 운영자 최종 확인 — 결과보기 화면(FacadeSnapshot)과 같은 파일을
    // 공유하므로 어느 화면에서 체크해도 즉시 일관됨.
    [ObservableProperty] private string? _outputDir;
    [ObservableProperty] private bool _needsRetake;
    [ObservableProperty] private bool _needsDetailCapture;

    // --- image previews ---
    [ObservableProperty] private string? _analysisImagePath;
    [ObservableProperty] private string? _visualImagePath;
    [ObservableProperty] private string? _analysisColmapImagePath;
    [ObservableProperty] private string? _visualColmapImagePath;

    /// <summary>True once any mosaic variant exists — gates the "크랙 검사 실행"
    /// button (detect_cracks_folder.py needs an analysis mosaic to run against).</summary>
    public bool HasMosaic => AnalysisImagePath != null || AnalysisColmapImagePath != null;

    partial void OnAnalysisImagePathChanged(string? value) => OnPropertyChanged(nameof(HasMosaic));
    partial void OnAnalysisColmapImagePathChanged(string? value) => OnPropertyChanged(nameof(HasMosaic));

    /// <summary>실행 이력 — output/version_index.json이 있는 facade만 채워짐
    /// (레거시 flat-output facade는 항상 비어있음). 최신 버전이 맨 앞.
    /// 읽기 전용 표시만: 과거 버전을 클릭해서 미리보기를 바꾸는 기능은 범위 밖.</summary>
    public ObservableCollection<RunVersionInfo> RunVersions { get; } = new();

    public ObservableCollection<string> Issues { get; } = new();

    public ObservableCollection<PipelineLogEntry> RecentEvents { get; } = new();

    /// <summary>2026-08-29: called right when a NEW run starts (RunFacade, before the pipeline
    /// process itself launches) -- ApplySnapshot (MainViewModel) only ever SETS these fields when
    /// its scan finds the corresponding stage's output file, it never clears one that's missing,
    /// so without this a facade being re-run kept showing the PREVIOUS run's crack
    /// count/CM Fallback stats/"보고서 생성"·"PDF 열기" buttons as if they were already
    /// available -- confirmed as a real bug (not just a display nitpick): GenerateReport's own
    /// guard only checks IsGeneratingReport/HasCrackResults, not IsRunning, so a still-stale-true
    /// HasCrackResults let a report actually get (re)generated from an in-progress, not-yet-valid
    /// mosaic. Resetting here makes "이 실행은 아직 이 단계에 도달하지 않았다" the honest default
    /// again, and each field gets set back to real data as ApplySnapshot naturally re-populates it
    /// stage by stage.</summary>
    public void ResetForNewRun()
    {
        ImageCount = 0;
        CoverageRatio = null;
        MeanInlierRatio = null;
        GlobalDriftScorePx = null;
        NeedsColmapFallback = false;

        HasColmapReport = false;
        ColmapRequested = 0;
        ColmapRegistered = 0;
        ColmapMeanReprojectionErrorPx = null;

        HasRectifiedMosaic = false;
        CoverageRatioColmap = null;

        HasCrackResults = false;
        CrackCount = 0;

        HasReport = false;
        ReportPath = null;

        AnalysisImagePath = null;
        VisualImagePath = null;
        AnalysisColmapImagePath = null;
        VisualColmapImagePath = null;

        // 2026-08-29: 새 실행은 새 버전 폴더(output/Vnnn)에 쓰므로 그 폴더의 quality_flags.json은
        // 아직 존재하지 않음 -- 이전 실행에서 체크했던 값이 새 실행 완료 전까지 잘못 남아있지
        // 않게 초기화. ApplySnapshot이 새 OutputDir의 실제 값(대부분 처음엔 둘 다 false)으로
        // 다시 채운다.
        OutputDir = null;
        NeedsRetake = false;
        NeedsDetailCapture = false;
    }

    public void AddIssue(string text)
    {
        if (!Issues.Contains(text))
            Issues.Insert(0, text);
        while (Issues.Count > 20)
            Issues.RemoveAt(Issues.Count - 1);
    }

    public void AddEvent(PipelineLogEntry entry)
    {
        RecentEvents.Insert(0, entry);
        while (RecentEvents.Count > 50)
            RecentEvents.RemoveAt(RecentEvents.Count - 1);
    }
}
