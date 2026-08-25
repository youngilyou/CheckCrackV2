using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.ViewModels;

/// <summary>"결과보기" 화면: facade별 원본/스티칭/보고서를 나란히 비교. AiTraining/
/// OriginalAi와 마찬가지로 MainViewModel과 독립된 도구 -- FacadeOutputScanner.ScanAll을
/// 자체 타이머로 폴링(2초 주기, MainViewModel._facadeScanTimer와 동일 패턴)해서
/// MainViewModel.Facades에 의존하지 않는다.</summary>
public partial class ResultsCompareViewModel : ObservableObject
{
    private const int MaxDisplayDim = 1600;

    public string RootPath { get; set; } = "";

    private readonly DispatcherTimer _scanTimer;

    public ObservableCollection<FacadeSnapshot> Facades { get; } = new();

    /// <summary>단지→동(선택)→방위 트리 — 분석·스티칭 화면(MainViewModel.FacadeTree)과
    /// 같은 FacadeHierarchyStore 분류를 사용해 똑같은 구조로 보여준다(사용자 요청:
    /// 두 화면의 트리가 일치하지 않아 어느 아파트/동인지 알 수 없던 문제). ComplexNode/
    /// BuildingNode/SideGroupNode는 MainViewModel과 공유하는 타입이지만, leaf는
    /// FacadeItemViewModel이 아니라 이 화면 자체의 FacadeSnapshot -- Facades 컬렉션과
    /// 달리 구조가 바뀔 때만 재생성(RebuildFacadeTreeIfChanged)해서 매 2초 폴링마다
    /// 사용자가 접어둔 Expander가 다시 펼쳐지지 않게 한다.</summary>
    public ObservableCollection<ComplexNode> FacadeTree { get; } = new();

    private string _lastTreeSignature = "";

    [ObservableProperty] private FacadeSnapshot? _selectedFacade;

    public ComparePanelState Panel1 { get; } = new() { Mode = "원본" };
    public ComparePanelState Panel2 { get; } = new() { Mode = "스티칭" };

    public ResultsCompareViewModel()
    {
        _scanTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(2) };
        _scanTimer.Tick += (_, _) => Rescan();
        _scanTimer.Start();

        Panel1.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(ComparePanelState.Mode)) ReloadPanel(Panel1); };
        Panel2.PropertyChanged += (_, e) => { if (e.PropertyName == nameof(ComparePanelState.Mode)) ReloadPanel(Panel2); };

        Rescan();
    }

    partial void OnSelectedFacadeChanged(FacadeSnapshot? value)
    {
        ReloadPanel(Panel1);
        ReloadPanel(Panel2);
        if (IsReviewMode)
            LoadReviewCanvas();
    }

    [RelayCommand]
    private void SetPanel1Mode(string mode) => Panel1.Mode = mode;

    [RelayCommand]
    private void SetPanel2Mode(string mode) => Panel2.Mode = mode;

    private void Rescan()
    {
        if (!Directory.Exists(RootPath))
            return;

        List<FacadeSnapshot> snapshots;
        try
        {
            snapshots = FacadeOutputScanner.ScanAll(RootPath)
                .Where(s => s.AnalysisImagePath != null || s.AnalysisColmapImagePath != null)
                .OrderBy(s => s.FacadeId, StringComparer.Ordinal)
                .ToList();
        }
        catch (IOException)
        {
            return; // transient; retried next tick
        }

        // Facades를 매번 통째로 비우고 다시 채우면, 선택된 facade의 FacadeId가 그대로여도
        // 매 폴링마다 새 FacadeSnapshot 오브젝트로 바뀌면서 SelectedFacade의 참조가 달라져
        // OnSelectedFacadeChanged가 불필요하게 다시 튄다 -- 그때마다 ReloadPanel이 줌/이동
        // 상태를 리셋해버렸다 (줌 도중 자동으로 원복되던 원인). FacadeId가 그대로인 항목은
        // 기존 오브젝트를 그 자리에서 갱신하고, 사라진/새로 생긴 것만 컬렉션에서 add/remove.
        var byId = snapshots.ToDictionary(s => s.FacadeId);

        for (var i = Facades.Count - 1; i >= 0; i--)
        {
            if (!byId.ContainsKey(Facades[i].FacadeId))
                Facades.RemoveAt(i);
        }

        var existingIds = new HashSet<string>(Facades.Select(f => f.FacadeId));
        foreach (var snap in snapshots)
        {
            if (existingIds.Contains(snap.FacadeId))
            {
                CopySnapshot(snap, Facades.First(f => f.FacadeId == snap.FacadeId));
            }
            else
            {
                var insertAt = Facades.TakeWhile(f => string.Compare(f.FacadeId, snap.FacadeId, StringComparison.Ordinal) < 0).Count();
                Facades.Insert(insertAt, snap);
            }
        }

        RebuildFacadeTreeIfChanged();
    }

    /// <summary>Facades(평면)를 단지→동(선택)→방위 트리로 재구성 -- MainViewModel.
    /// RebuildFacadeTree/BuildSideGroups와 동일한 그룹핑 규칙(같은 FacadeHierarchyStore
    /// 분류를 읽으므로 두 화면 트리가 항상 일치). 시그니처가 실제로 바뀌었을 때만
    /// 다시 그려서, 2초 폴링마다 사용자가 접어둔 Expander가 다시 펼쳐지는 걸 막는다.</summary>
    private void RebuildFacadeTreeIfChanged()
    {
        var signature = string.Join("|", Facades
            .OrderBy(f => f.FacadeId, StringComparer.Ordinal)
            .Select(f => $"{f.FacadeId}:{f.ComplexId}:{f.BuildingId}:{f.Side}"));
        if (signature == _lastTreeSignature)
            return;
        _lastTreeSignature = signature;

        FacadeTree.Clear();

        var byComplex = Facades
            .GroupBy(f => (f.ComplexId, f.ComplexName))
            .OrderBy(g => g.Key.ComplexId == "__UNSORTED__" ? 1 : 0)
            .ThenBy(g => g.Key.ComplexName, StringComparer.Ordinal);

        foreach (var complexGroup in byComplex)
        {
            var complexNode = new ComplexNode { ComplexId = complexGroup.Key.ComplexId, ComplexName = complexGroup.Key.ComplexName };

            var withBuilding = complexGroup.Where(f => !string.IsNullOrEmpty(f.BuildingId));
            var withoutBuilding = complexGroup.Where(f => string.IsNullOrEmpty(f.BuildingId));

            var buildingGroups = withBuilding
                .GroupBy(f => (f.BuildingId, f.BuildingName))
                .OrderBy(g => g.Key.BuildingName, StringComparer.Ordinal);
            foreach (var buildingGroup in buildingGroups)
            {
                var buildingNode = new BuildingNode
                {
                    BuildingId = buildingGroup.Key.BuildingId!,
                    BuildingName = buildingGroup.Key.BuildingName ?? buildingGroup.Key.BuildingId!,
                };
                foreach (var sideGroup in BuildSideGroups(buildingGroup))
                    buildingNode.Children.Add(sideGroup);
                complexNode.Children.Add(buildingNode);
            }

            foreach (var sideGroup in BuildSideGroups(withoutBuilding))
                complexNode.Children.Add(sideGroup);

            FacadeTree.Add(complexNode);
        }
    }

    private static IEnumerable<SideGroupNode> BuildSideGroups(IEnumerable<FacadeSnapshot> facades)
    {
        return facades
            .GroupBy(f => f.Side)
            .OrderBy(g => g.Key, StringComparer.Ordinal)
            .Select(g =>
            {
                var node = new SideGroupNode { Side = g.Key };
                foreach (var f in g.OrderBy(f => f.FacadeId, StringComparer.Ordinal))
                    node.Facades.Add(f);
                return node;
            });
    }

    [RelayCommand]
    private void SelectFacade(FacadeSnapshot facade) => SelectedFacade = facade;

    private static void CopySnapshot(FacadeSnapshot src, FacadeSnapshot dst)
    {
        dst.OutputDir = src.OutputDir;
        dst.ComplexId = src.ComplexId;
        dst.ComplexName = src.ComplexName;
        dst.BuildingId = src.BuildingId;
        dst.BuildingName = src.BuildingName;
        dst.Side = src.Side;
        dst.SourceFolderPath = src.SourceFolderPath;
        dst.Quality = src.Quality;
        dst.QualityColmap = src.QualityColmap;
        dst.Colmap = src.Colmap;
        dst.Cracks = src.Cracks;
        dst.AnalysisImagePath = src.AnalysisImagePath;
        dst.VisualImagePath = src.VisualImagePath;
        dst.AnalysisColmapImagePath = src.AnalysisColmapImagePath;
        dst.VisualColmapImagePath = src.VisualColmapImagePath;
        dst.ReportPath = src.ReportPath;
    }

    private void ReloadPanel(ComparePanelState panel)
    {
        panel.OriginalImageList = new List<string>();
        panel.OriginalImageIndex = -1;
        panel.OriginalDisplayBitmap = null;
        panel.StitchDisplayBitmap = null;
        panel.StitchImagePath = "";
        panel.ZoomFactor = 1.0;
        panel.ReportPageBitmap = null;
        panel.ReportPageIndex = 0;
        panel.ReportPageCount = 0;
        panel.ReportDisplayWidth = 0;
        panel.ReportDisplayHeight = 0;

        if (SelectedFacade == null)
            return;

        switch (panel.Mode)
        {
            case "원본": LoadOriginalImages(panel); break;
            case "스티칭": LoadStitchImage(panel); break;
            case "보고서": LoadReportPage(panel); break;
        }
    }

    private void LoadOriginalImages(ComparePanelState panel)
    {
        var facade = SelectedFacade;
        var mosaicPath = facade?.AnalysisColmapImagePath ?? facade?.AnalysisImagePath;
        var outputDir = mosaicPath != null ? Path.GetDirectoryName(mosaicPath) : null;
        if (facade == null || outputDir == null)
            return;

        var sourceJsonPath = Path.Combine(outputDir, $"{facade.FacadeId}_source_images.json");
        if (!File.Exists(sourceJsonPath))
            return;

        try
        {
            var entries = JsonSerializer.Deserialize<List<SourceImageEntry>>(File.ReadAllText(sourceJsonPath));
            var paths = entries?.Select(e => e.FilePath).Where(File.Exists).ToList() ?? new List<string>();
            panel.OriginalImageList = paths;
            if (paths.Count > 0)
            {
                panel.OriginalImageIndex = 0;
                LoadOriginalImageAt(panel);
            }
        }
        catch (JsonException)
        {
            // 파일 아직 없거나 중간에 쓰는 중 -- 다음 rescan에서 재시도
        }
    }

    private static void LoadOriginalImageAt(ComparePanelState panel)
    {
        if (panel.OriginalImageIndex < 0 || panel.OriginalImageIndex >= panel.OriginalImageList.Count)
            return;
        var bitmap = LoadScaledBitmap(panel.OriginalImageList[panel.OriginalImageIndex]);
        panel.OriginalDisplayBitmap = bitmap;
        panel.OriginalDisplayWidth = bitmap?.PixelWidth ?? 0;
        panel.OriginalDisplayHeight = bitmap?.PixelHeight ?? 0;
        panel.ZoomFactor = 1.0;
    }

    [RelayCommand]
    private void PreviousOriginal(ComparePanelState panel)
    {
        if (panel.OriginalImageIndex <= 0)
            return;
        panel.OriginalImageIndex--;
        LoadOriginalImageAt(panel);
    }

    [RelayCommand]
    private void NextOriginal(ComparePanelState panel)
    {
        if (panel.OriginalImageIndex >= panel.OriginalImageList.Count - 1)
            return;
        panel.OriginalImageIndex++;
        LoadOriginalImageAt(panel);
    }

    private void LoadStitchImage(ComparePanelState panel)
    {
        var facade = SelectedFacade;
        var path = facade?.AnalysisColmapImagePath ?? facade?.AnalysisImagePath;
        if (path == null || !File.Exists(path))
            return;

        panel.StitchImagePath = path;
        panel.StitchIsColmapRectified = facade?.AnalysisColmapImagePath != null;
        var bitmap = LoadScaledBitmap(path);
        panel.StitchDisplayBitmap = bitmap;
        panel.StitchDisplayWidth = bitmap?.PixelWidth ?? 0;
        panel.StitchDisplayHeight = bitmap?.PixelHeight ?? 0;
    }

    [RelayCommand]
    private void ZoomInPanel(ComparePanelState panel) =>
        panel.ZoomFactor = Math.Min(6.0, Math.Round((panel.ZoomFactor + 0.25) * 100) / 100);

    // 0.05 하한(1.0 아님) -- "전체 보기"가 큰 모자이크를 100% 밑으로 줄여놓은 뒤에도
    // "－"로 계속 축소할 수 있어야 한다. 1.0으로 막아두면 전체 보기 직후 "－"를 눌렀을 때
    // 오히려 확대되는 것처럼 보여 혼란스러웠다.
    [RelayCommand]
    private void ZoomOutPanel(ComparePanelState panel) =>
        panel.ZoomFactor = Math.Max(0.05, Math.Round((panel.ZoomFactor - 0.25) * 100) / 100);

    [RelayCommand]
    private void ResetZoomPanel(ComparePanelState panel) => panel.ZoomFactor = 1.0;

    private void LoadReportPage(ComparePanelState panel)
    {
        var reportPath = SelectedFacade?.ReportPath;
        if (reportPath == null || !File.Exists(reportPath))
            return;
        try
        {
            panel.ReportPageCount = PdfPageRenderer.GetPageCount(reportPath);
            panel.ReportPageIndex = 0;
            panel.ReportPageBitmap = PdfPageRenderer.RenderPage(reportPath, 0);
        }
        catch (Exception)
        {
            panel.ReportPageBitmap = null;
            panel.ReportPageCount = 0;
        }
    }

    [RelayCommand]
    private void PreviousReportPage(ComparePanelState panel)
    {
        var reportPath = SelectedFacade?.ReportPath;
        if (reportPath == null || panel.ReportPageIndex <= 0)
            return;
        panel.ReportPageIndex--;
        panel.ReportPageBitmap = PdfPageRenderer.RenderPage(reportPath, panel.ReportPageIndex);
        panel.ZoomFactor = 1.0;
    }

    [RelayCommand]
    private void NextReportPage(ComparePanelState panel)
    {
        var reportPath = SelectedFacade?.ReportPath;
        if (reportPath == null || panel.ReportPageIndex >= panel.ReportPageCount - 1)
            return;
        panel.ReportPageIndex++;
        panel.ReportPageBitmap = PdfPageRenderer.RenderPage(reportPath, panel.ReportPageIndex);
        panel.ZoomFactor = 1.0;
    }

    /// <summary>Header-only read for true pixel size (mirrors AiTrainingViewModel.LoadImage),
    /// then decodes at a display-safe DecodePixelWidth -- facade mosaics can be tens of
    /// megapixels, originals are normal camera resolution, both are safe to cap the same way.</summary>
    private static BitmapImage? LoadScaledBitmap(string path)
    {
        try
        {
            int origWidth, origHeight;
            using (var stream = File.OpenRead(path))
            {
                var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
                var frame = decoder.Frames[0];
                origWidth = frame.PixelWidth;
                origHeight = frame.PixelHeight;
            }

            var scale = Math.Min(1.0, (double)MaxDisplayDim / Math.Max(origWidth, origHeight));
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            // IgnoreColorProfile: 원본(JPG, 카메라가 심어둔 ICC/EXIF 색 프로파일 있음)과
            // 스티칭 결과(TIFF, OpenCV가 그냥 쓴 파일이라 프로파일 없음)를 WPF 기본 설정으로
            // 각각 디코드하면 하나는 색 관리를 타고 하나는 안 타서 같은 픽셀값이라도 화면에
            // 다르게 보인다 -- 실제로 src/stitching/blend.py의 blend_analysis()는 픽셀을
            // 그대로 복사만 하고 색 보정을 전혀 안 하므로(Blender_NO), 원본과 분석 모자이크의
            // 실제 데이터는 같아야 한다. 두 경로 다 프로파일을 무시하고 원본 픽셀 바이트
            // 그대로 그리게 강제해서 이 불일치를 없앤다.
            bitmap.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
            bitmap.DecodePixelWidth = (int)Math.Round(origWidth * scale);
            bitmap.UriSource = new Uri(path);
            bitmap.EndInit();
            bitmap.Freeze();
            return bitmap;
        }
        catch
        {
            return null;
        }
    }

    // =====================================================================
    // 균열 검토 (crack review, CLAUDE.local.md #37 Human Verification)
    // =====================================================================
    // Review state (rejected/manually-added cracks) never touches
    // {facade_id}_cracks.json -- that file is pipeline-owned and gets
    // rewritten on every re-detection run, so it lives in a sibling
    // {facade_id}_crack_review.json instead (CrackReviewStore), read back by
    // src/crack/review.py at report-generation time. This mirrors the exact
    // reasoning FacadeVersionStore already documents for not trusting a
    // single mutable file across re-runs.

    /// <summary>Set by MainViewModel.OnLoggedInUsernameChanged, same
    /// propagation pattern as RootPath -- only used to attribute review
    /// actions (누가 이 크랙을 거부/추가했는지), never for access control here.</summary>
    public string LoggedInUsername { get; set; } = "";

    public ObservableCollection<CrackReviewItem> ReviewItems { get; } = new();

    /// <summary>image_id -> original photo file path, for OriginalCrackViewerWindow
    /// (원본 보기) to resolve a crack's SourceObservations into an actual file to
    /// load -- loaded once per LoadReviewCanvas from the same
    /// {facade_id}_source_images.json ComparePanelState's "원본" mode already
    /// reads (LoadOriginalImages above), kept separate since review mode has its
    /// own lifecycle/reload timing.</summary>
    private Dictionary<string, string> _reviewSourceImagePaths = new();

    public string? ResolveSourceImagePath(string imageId) =>
        _reviewSourceImagePaths.TryGetValue(imageId, out var path) ? path : null;

    /// <summary>Selected by clicking a numbered badge on the canvas (ToggleCrackHighlight
    /// also sets this) or by selecting a row in the crack list (ListBox.SelectedItem
    /// two-way binding) -- either way, if OriginalCrackViewerWindow (원본 보기) is
    /// currently open, ResultsCompareView.xaml.cs's PropertyChanged subscription
    /// pushes this crack's SourceObservations to it, centered on the crack location.</summary>
    [ObservableProperty] private CrackReviewItem? _selectedReviewItem;

    [ObservableProperty] private bool _isReviewMode;
    [ObservableProperty] private BitmapImage? _reviewDisplayBitmap;
    [ObservableProperty] private int _reviewDisplayWidth;
    [ObservableProperty] private int _reviewDisplayHeight;
    [ObservableProperty] private double _reviewZoomFactor = 1.0;

    /// <summary>Bound to each crack badge's own RenderTransform to cancel out
    /// the review canvas's LayoutTransform zoom -- without this, the badge
    /// (and previously, the full-text label) grows right along with the
    /// image at high zoom until it's a giant box covering the exact area
    /// being inspected (user reported this directly, screenshot showed the
    /// label filling most of a 575%-zoomed crop). The badge's Canvas
    /// position still scales normally with zoom/pan (computed in the same
    /// pre-transform layout space as the image), so it stays correctly
    /// anchored to its crack -- only its rendered SIZE stays constant.</summary>
    public double InverseReviewZoomFactor => ReviewZoomFactor > 0 ? 1.0 / ReviewZoomFactor : 1.0;

    partial void OnReviewZoomFactorChanged(double value) => OnPropertyChanged(nameof(InverseReviewZoomFactor));

    [ObservableProperty] private string _reviewStatusText = "";
    [ObservableProperty] private bool _isSavingReview;
    /// <summary>Drives the "최종 보고서 재생성" progress bar/popup -- covers the
    /// whole operation (SaveReview + the generate_report.py subprocess), unlike
    /// IsSavingReview which only spans the review-JSON write inside it.</summary>
    [ObservableProperty] private bool _isRegeneratingReport;
    [ObservableProperty] private string? _reviewedBy;
    [ObservableProperty] private string? _reviewedAt;

    private double _reviewScale = 1.0;
    private int _reviewOrigWidth;
    private int _reviewOrigHeight;
    private int _nextManualLabel = 1;

    /// <summary>Loaded once per LoadReviewCanvas (null if this facade predates
    /// homography/seam-map persistence). Lets SourceObservationCalculator
    /// compute source_observations immediately, client-side, for manually-drawn
    /// cracks (which Python's pipeline never sees at all) and as a fallback for
    /// any AI-detected crack whose own SourceObservations came back empty.</summary>
    private SourceObservationCalculator.SeamArtifacts? _reviewSeamArtifacts;

    [RelayCommand]
    private void ToggleReviewMode()
    {
        IsReviewMode = !IsReviewMode;
        if (IsReviewMode)
            LoadReviewCanvas();
    }

    private void LoadReviewCanvas()
    {
        ReviewItems.Clear();
        ReviewDisplayBitmap = null;
        ReviewDisplayWidth = 0;
        ReviewDisplayHeight = 0;
        ReviewZoomFactor = 1.0;
        ReviewStatusText = "";
        ReviewedBy = null;
        ReviewedAt = null;
        _nextManualLabel = 1;

        var facade = SelectedFacade;
        var mosaicPath = facade?.AnalysisColmapImagePath ?? facade?.AnalysisImagePath;
        if (facade == null || facade.OutputDir == null || mosaicPath == null || !File.Exists(mosaicPath))
            return;

        int origWidth, origHeight;
        try
        {
            using var stream = File.OpenRead(mosaicPath);
            var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
            var frame = decoder.Frames[0];
            origWidth = frame.PixelWidth;
            origHeight = frame.PixelHeight;
        }
        catch
        {
            return;
        }
        _reviewOrigWidth = origWidth;
        _reviewOrigHeight = origHeight;
        _reviewScale = Math.Min(1.0, (double)MaxDisplayDim / Math.Max(origWidth, origHeight));

        var bitmap = LoadScaledBitmap(mosaicPath);
        ReviewDisplayBitmap = bitmap;
        ReviewDisplayWidth = bitmap?.PixelWidth ?? 0;
        ReviewDisplayHeight = bitmap?.PixelHeight ?? 0;

        var review = CrackReviewStore.Load(facade.OutputDir, facade.FacadeId);
        var rejectedIds = new HashSet<string>(review?.Rejected.Select(r => r.CrackId) ?? Enumerable.Empty<string>());
        ReviewedBy = review?.ReviewedBy;
        ReviewedAt = review?.ReviewedAt is { } at ? at.ToLocalTime().ToString("yyyy-MM-dd HH:mm") : null;

        _reviewSourceImagePaths = new Dictionary<string, string>();
        var sourceJsonPath = Path.Combine(facade.OutputDir, $"{facade.FacadeId}_source_images.json");
        if (File.Exists(sourceJsonPath))
        {
            try
            {
                var entries = JsonSerializer.Deserialize<List<SourceImageEntry>>(File.ReadAllText(sourceJsonPath));
                foreach (var entry in entries ?? new List<SourceImageEntry>())
                    _reviewSourceImagePaths[entry.ImageId] = entry.FilePath;
            }
            catch (JsonException)
            {
                // 파일 아직 없거나 중간에 쓰는 중 -- 다음 rescan에서 재시도, source_observations는
                // 그동안 "원본 경로 못 찾음"으로만 처리되고 크래시하지 않음
            }
        }

        _reviewSeamArtifacts = SourceObservationCalculator.LoadSeamArtifacts(facade.OutputDir, facade.FacadeId);

        foreach (var crack in facade.Cracks ?? new List<CrackResultModel>())
        {
            if (crack.PolygonPx == null || crack.PolygonPx.Length == 0)
                continue;
            var status = rejectedIds.Contains(crack.CrackId) ? CrackReviewStatus.Rejected : CrackReviewStatus.Pending;
            // Python already computed source_observations for AI detections when the
            // pipeline ran -- only fall back to computing it here if that came back
            // empty (older facade re-detected before this existed, etc.), never
            // recompute/override an already-real result.
            var sourceObservations = crack.SourceObservations;
            if ((sourceObservations == null || sourceObservations.Count == 0) && _reviewSeamArtifacts != null)
                sourceObservations = SourceObservationCalculator.Compute(crack.PolygonPx, _reviewSeamArtifacts);
            ReviewItems.Add(BuildReviewItem(
                crack.CrackId, crack.LengthPx, crack.MaxWidthPx, crack.AreaPx,
                crack.Confidence, crack.Severity, crack.PolygonPx, crack.BboxPx, status, sourceObservations));
        }

        foreach (var addition in review?.ManualAdditions ?? new List<ManualCrackAddition>())
        {
            var label = addition.CrackId ?? $"(신규 #{_nextManualLabel++})";
            var sourceObservations = _reviewSeamArtifacts != null
                ? SourceObservationCalculator.Compute(addition.PolygonPx, _reviewSeamArtifacts)
                : null;
            ReviewItems.Add(BuildReviewItem(label, null, null, null, 1.0, null, addition.PolygonPx, null, CrackReviewStatus.Manual, sourceObservations));
        }

        UpdateReviewStatusText();
    }

    private CrackReviewItem BuildReviewItem(
        string crackId, double? lengthPx, double? maxWidthPx, double? areaPx, double confidence,
        string? severity, double[][] polygonPx, double[]? bboxPx, CrackReviewStatus status,
        List<SourceObservationModel>? sourceObservations = null)
    {
        var canvasPoints = new PointCollection(polygonPx.Select(p => new Point(p[0] * _reviewScale, p[1] * _reviewScale)));
        return new CrackReviewItem
        {
            CrackId = crackId,
            LengthPx = lengthPx,
            MaxWidthPx = maxWidthPx,
            AreaPx = areaPx,
            Confidence = confidence,
            Severity = severity,
            PolygonPx = polygonPx,
            BboxPx = bboxPx,
            Status = status,
            CanvasPoints = canvasPoints,
            SourceObservations = sourceObservations ?? new List<SourceObservationModel>(),
        };
    }

    /// <summary>Re-numbers every badge 1..N in list order -- called after any
    /// add/remove so numbers stay dense and stable-ish rather than leaving
    /// gaps, and so the canvas badge number always matches this crack's
    /// position in the review list panel (cross-referencing one from the
    /// other, same idea as the PDF report's numbered crack map).</summary>
    private void RenumberReviewItems()
    {
        for (var i = 0; i < ReviewItems.Count; i++)
            ReviewItems[i].DisplayNumber = i + 1;
    }

    private void UpdateReviewStatusText()
    {
        RenumberReviewItems();
        var aiTotal = SelectedFacade?.Cracks?.Count ?? 0;
        var rejected = ReviewItems.Count(i => i.Status == CrackReviewStatus.Rejected);
        var manual = ReviewItems.Count(i => i.Status == CrackReviewStatus.Manual);
        ReviewStatusText = $"AI 탐지 {aiTotal}건 · 제외 {rejected}건 · 수동 추가 {manual}건";
    }

    [RelayCommand]
    private void ToggleCrackReject(CrackReviewItem item)
    {
        if (item.Status == CrackReviewStatus.Manual)
            return; // 수동 추가 항목은 RemoveManualCrack으로 지운다 -- "제외" 개념이 없음
        item.Status = item.Status == CrackReviewStatus.Rejected ? CrackReviewStatus.Pending : CrackReviewStatus.Rejected;
        UpdateReviewStatusText();
    }

    [RelayCommand]
    private void RemoveManualCrack(CrackReviewItem item)
    {
        if (item.Status != CrackReviewStatus.Manual)
            return;
        ReviewItems.Remove(item);
        UpdateReviewStatusText();
    }

    /// <summary>Direct command target for the small numbered badge (bound from
    /// XAML, CommandParameter=the item) -- a reliable, always-reasonably-sized
    /// click target regardless of zoom, unlike clicking the crack's own thin
    /// polygon outline (TryToggleHighlightAt below still works too, for
    /// clicking the outline directly when zoomed in close; both end up
    /// toggling the exact same property).</summary>
    [RelayCommand]
    private void ToggleCrackHighlight(CrackReviewItem item)
    {
        item.IsHighlightVisible = !item.IsHighlightVisible;
        // 배지 클릭도 리스트 선택과 똑같이 "이 크랙을 골랐다"는 신호로 취급 -- 원본 보기
        // 창이 열려 있으면 그쪽에서 이 크랙의 SourceObservations로 점프한다.
        SelectedReviewItem = item;
    }

    /// <summary>Called from code-behind on mouse-down, before deciding whether
    /// to start a new drag-drawn polygon -- if the click landed inside an
    /// existing crack's overlay, this toggles that crack's highlight on/off
    /// instead (user's explicit request: click a crack to hide its overlay
    /// and see the bare mosaic pixels underneath, since even a light
    /// semi-transparent fill made a thin crack impossible to actually judge).
    /// Returns true if a crack was hit (so the caller skips starting a draw
    /// gesture), false if the click was on empty mosaic.</summary>
    public bool TryToggleHighlightAt(Point canvasPoint)
    {
        // Reverse order: later items were added more recently (including manual
        // additions drawn on top), so a click in an overlapping area hits the
        // most-recently-relevant crack first, matching what's visually on top.
        for (var i = ReviewItems.Count - 1; i >= 0; i--)
        {
            var item = ReviewItems[i];
            if (IsPointInPolygon(canvasPoint, item.CanvasPoints))
            {
                item.IsHighlightVisible = !item.IsHighlightVisible;
                SelectedReviewItem = item;
                return true;
            }
        }
        return false;
    }

    /// <summary>Standard ray-casting point-in-polygon test (even-odd rule) --
    /// CanvasPoints is already in the same display-space coordinates as
    /// canvasPoint, no scaling needed here.</summary>
    private static bool IsPointInPolygon(Point p, PointCollection polygon)
    {
        var inside = false;
        var n = polygon.Count;
        if (n < 3)
            return false;
        for (int i = 0, j = n - 1; i < n; j = i++)
        {
            var pi = polygon[i];
            var pj = polygon[j];
            if ((pi.Y > p.Y) != (pj.Y > p.Y) &&
                p.X < ((pj.X - pi.X) * (p.Y - pi.Y) / (pj.Y - pi.Y)) + pi.X)
            {
                inside = !inside;
            }
        }
        return inside;
    }

    /// <summary>Called from ResultsCompareView's code-behind once a drag-drawn
    /// polygon is finished (same gesture AiTrainingView already uses for its
    /// own annotation canvas) -- displayPoints are in the review canvas's own
    /// display space, converted back to the mosaic's real pixel space here so
    /// it round-trips through CrackReviewStore/measure_polygon unchanged.</summary>
    public void AddManualCrack(IEnumerable<Point> displayPoints)
    {
        var canvasPoints = new PointCollection(displayPoints);
        if (canvasPoints.Count < 3)
            return; // not enough points to be a real polygon -- silently drop rather than save a degenerate shape

        var polygonPx = canvasPoints
            .Select(p => new[]
            {
                Math.Clamp(p.X / _reviewScale, 0, Math.Max(0, _reviewOrigWidth - 1)),
                Math.Clamp(p.Y / _reviewScale, 0, Math.Max(0, _reviewOrigHeight - 1)),
            })
            .ToArray();

        // AI 크랙과 달리 이 폴리곤은 Python 파이프라인을 거친 적이 전혀 없으므로(저장/최종
        // 보고서 재생성을 기다리지 않고) 그린 즉시 여기서 source_observations를 계산한다 --
        // 그래야 "원본 보기"가 바로 이 크랙으로 점프할 수 있다(사용자 요청).
        var sourceObservations = _reviewSeamArtifacts != null
            ? SourceObservationCalculator.Compute(polygonPx, _reviewSeamArtifacts)
            : null;

        ReviewItems.Add(new CrackReviewItem
        {
            CrackId = $"(신규 #{_nextManualLabel++})",
            PolygonPx = polygonPx,
            Status = CrackReviewStatus.Manual,
            CanvasPoints = canvasPoints,
            SourceObservations = sourceObservations ?? new List<SourceObservationModel>(),
        });
        UpdateReviewStatusText();
    }

    [RelayCommand]
    private async Task SaveReview()
    {
        var facade = SelectedFacade;
        if (facade?.OutputDir == null || IsSavingReview)
            return;

        IsSavingReview = true;
        try
        {
            var now = DateTimeOffset.Now;
            var file = new CrackReviewFile
            {
                FacadeId = facade.FacadeId,
                ReviewedBy = LoggedInUsername,
                ReviewedAt = now,
                Rejected = ReviewItems
                    .Where(i => i.Status == CrackReviewStatus.Rejected)
                    .Select(i => new RejectedCrackEntry { CrackId = i.CrackId, ReviewedBy = LoggedInUsername, ReviewedAt = now })
                    .ToList(),
                ManualAdditions = ReviewItems
                    .Where(i => i.Status == CrackReviewStatus.Manual)
                    .Select(i => new ManualCrackAddition { PolygonPx = i.PolygonPx, AddedBy = LoggedInUsername, AddedAt = now })
                    .ToList(),
            };
            var outputDir = facade.OutputDir;
            await Task.Run(() => CrackReviewStore.Save(outputDir, facade.FacadeId, file));
            ReviewedBy = LoggedInUsername;
            ReviewedAt = now.ToLocalTime().ToString("yyyy-MM-dd HH:mm");
            ReviewStatusText = "검토 내용을 저장했습니다.";
        }
        catch (Exception ex)
        {
            ReviewStatusText = $"검토 저장 실패: {ex.Message}";
        }
        finally
        {
            IsSavingReview = false;
        }
    }

    /// <summary>Saves review state, then regenerates {facade_id}_report.pdf via
    /// tools/generate_report.py -- same subprocess-launch pattern
    /// MainViewModel.GenerateReport uses, duplicated here rather than shared
    /// because this ViewModel is deliberately independent of MainViewModel
    /// (see the class doc comment). src/crack/review.py picks up the just-saved
    /// {facade_id}_crack_review.json automatically -- no extra argument needed.</summary>
    [RelayCommand]
    private async Task RegenerateFinalReport()
    {
        var facade = SelectedFacade;
        if (facade?.OutputDir == null || string.IsNullOrEmpty(RootPath) || IsSavingReview || IsRegeneratingReport)
            return;

        IsRegeneratingReport = true;
        try
        {
            await RegenerateFinalReportCore(facade, facade.OutputDir);
        }
        finally
        {
            IsRegeneratingReport = false;
        }
    }

    private async Task RegenerateFinalReportCore(FacadeSnapshot facade, string outputDir)
    {
        await SaveReview();

        ReviewStatusText = "최종 보고서 생성 중...";
        try
        {
            var scriptPath = Path.Combine(RootPath, "tools", "generate_report.py");
            var psi = new ProcessStartInfo
            {
                FileName = PythonEnvironment.DiscoverPythonExe(),
                WorkingDirectory = RootPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add(scriptPath);
            psi.ArgumentList.Add("facade");
            psi.ArgumentList.Add(outputDir);
            psi.ArgumentList.Add(facade.FacadeId);

            using var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            process.Start();
            ChildProcessRegistry.Register(process);
            try
            {
                var stderrTask = process.StandardError.ReadToEndAsync();
                var stdoutTask = process.StandardOutput.ReadToEndAsync();
                await process.WaitForExitAsync();

                if (process.ExitCode != 0)
                {
                    var stderr = await stderrTask;
                    ReviewStatusText = $"보고서 생성 실패: {SummarizePythonError(stderr)}";
                }
                else
                {
                    ReviewStatusText = "최종 보고서를 재생성했습니다.";
                    if (Panel1.Mode == "보고서")
                        LoadReportPage(Panel1);
                    if (Panel2.Mode == "보고서")
                        LoadReportPage(Panel2);
                    MessageBox.Show("최종 보고서를 재생성했습니다.", "완료", MessageBoxButton.OK, MessageBoxImage.Information);
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
        }
        catch (Exception ex)
        {
            ReviewStatusText = $"보고서 생성을 시작할 수 없습니다: {ex.Message}";
        }
    }

    [RelayCommand]
    private void ZoomInReview() => ReviewZoomFactor = Math.Min(6.0, Math.Round((ReviewZoomFactor + 0.25) * 100) / 100);

    [RelayCommand]
    private void ZoomOutReview() => ReviewZoomFactor = Math.Max(0.05, Math.Round((ReviewZoomFactor - 0.25) * 100) / 100);

    [RelayCommand]
    private void ResetZoomReview() => ReviewZoomFactor = 1.0;

    /// <summary>Same last-line-of-stderr + innermost-frame extraction
    /// MainViewModel.SummarizePythonError uses -- duplicated for the same
    /// independent-ViewModel reason RegenerateFinalReport's doc comment
    /// gives, not because the logic is actually different.</summary>
    private static string SummarizePythonError(string stderr)
    {
        var lines = stderr
            .Split('\n')
            .Select(l => l.TrimEnd('\r'))
            .Where(l => !string.IsNullOrWhiteSpace(l))
            .ToList();
        if (lines.Count == 0)
            return "(stderr 출력 없음)";

        var message = lines[^1].Trim();
        var location = lines.LastOrDefault(l => l.TrimStart().StartsWith("File \"", StringComparison.Ordinal))?.Trim();
        return location != null ? $"{message}  [위치: {location}]" : message;
    }
}

file sealed class SourceImageEntry
{
    [JsonPropertyName("image_id")] public string ImageId { get; set; } = "";
    [JsonPropertyName("file_path")] public string FilePath { get; set; } = "";
}
