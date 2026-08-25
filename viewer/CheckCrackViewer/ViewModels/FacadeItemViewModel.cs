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
