using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using CheckCrackViewer.Services;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

/// <summary>Full-resolution image viewer with cursor-centered mouse-wheel
/// zoom and click-drag pan. Loads the image at native resolution (no
/// DecodePixelWidth cap) — that's the whole point versus the list thumbnail.
///
/// Zoom/pan are implemented by setting TheImage's actual Width/Height and
/// Canvas.Left/Top directly on every step, NOT via a RenderTransform. An
/// earlier RenderTransform-based version (ScaleTransform+TranslateTransform
/// scaling the image's full native, e.g. 1757x4000, intrinsic size) rendered
/// only a small corner of the image under this app's software rendering mode
/// (RenderOptions.ProcessRenderMode=SoftwareOnly, see App.xaml.cs) — confirmed
/// by A/B testing against plain Stretch="Uniform", which rendered correctly.
/// Keeping the element's real layout size equal to what's actually visible
/// avoids whatever that RenderTransform interaction was.</summary>
public partial class ImageViewerWindow : Window
{
    private bool _isDragging;
    private Point _dragStart;
    private double _panStartLeft, _panStartTop;
    private int _pixelWidth, _pixelHeight;
    private double _scale = 1.0;
    private bool _userHasZoomedOrPanned;
    private readonly FacadeItemViewModel? _liveFacade;

    // 층수 라벨(2026-09-17, 사용자 요청) -- 둘 다 있어야 그릴 수 있음: 건물의 총 층수/층고
    // (BuildingMetadataStore, 사용자 수동 입력) + 이 facade의 px_per_m(스케일 보정 여부 포함).
    // rootPath가 없으면(예: ResultsCompareView 쪽 호출 경로) 조용히 생략 -- 플로어 라벨은
    // "있으면 보너스" 기능이지 필수 경로가 아니라서, 이 정보 없이도 뷰어 자체는 항상 정상 동작해야 함.
    private FacadeItemViewModel? _facadeContext;
    private string? _rootPath;
    private List<FloorRow> _floorRows = new();

    // 2026-09-19: 정면 영역 지정(다각형 그리기) 상태. 좌표는 전부 "이미지 자신의 픽셀 공간"
    // (파이썬 쪽 manual_region.json이 읽는 것과 같은 좌표계) -- 화면 좌표는 줌/팬마다 바뀌므로
    // 절대 여기에 저장하지 않고, RedrawRegionOverlay가 그릴 때마다 현재 _scale/left/top으로
    // 다시 변환한다.
    private bool _isDrawingRegion;
    private readonly List<List<Point>> _completedPolygons = new();
    private List<Point> _currentPolygon = new();
    private bool _isRegionBusy;

    public ImageViewerWindow(string imagePath)
    {
        InitializeComponent();
        InitWindowBounds();
        LoadImage(imagePath);
        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
    }

    /// <summary>이미지 경로 + facade 컨텍스트(층수 라벨 계산용) 둘 다 아는 경우 -- 완료된
    /// 모자이크를 메인 화면 썸네일에서 더블클릭했을 때 쓰는 경로(MainWindow.Image_MouseLeftButtonDown).</summary>
    public ImageViewerWindow(string imagePath, FacadeItemViewModel facade, string? rootPath)
    {
        InitializeComponent();
        InitWindowBounds();
        _facadeContext = facade;
        _rootPath = rootPath;
        LoadImage(imagePath);
        RegionTool_UpdateVisibility();
        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
    }

    /// <summary>"Live" mode: opened from the growing stitching preview while a
    /// facade is actively running. Keeps showing whatever LivePreviewImagePath
    /// points at as it's replaced by newer snapshots, then switches over once to
    /// the real final mosaic once the run finishes and one becomes available —
    /// matching CLAUDE.local.md's "don't show it until it's genuinely done"
    /// requirement for the *final* image, while still growing live in between.</summary>
    public ImageViewerWindow(FacadeItemViewModel facade, string? rootPath = null)
    {
        InitializeComponent();
        InitWindowBounds();
        _liveFacade = facade;
        _facadeContext = facade;
        _rootPath = rootPath;
        _liveFacade.PropertyChanged += Facade_PropertyChanged;
        Closed += (_, _) => _liveFacade.PropertyChanged -= Facade_PropertyChanged;

        if (facade.LivePreviewImagePath != null)
            LoadImage(facade.LivePreviewImagePath, facade.FacadeId + " (실시간 미리보기)");
        else
            Tag = facade.FacadeId + " (실시간 미리보기 대기 중)";
        RegionTool_UpdateVisibility();

        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
    }

    private void InitWindowBounds()
    {
        // WindowState="Maximized" resolves through an OS animation that takes
        // more than one layout pass on this machine (a remote/software-
        // rendered session) — Viewport.SizeChanged kept firing at a
        // small pre-maximize size first. Setting the final bounds directly
        // from the work-area, with WindowState staying Normal, makes the
        // window's true size known immediately on the very first layout pass.
        var wa = SystemParameters.WorkArea;
        Left = wa.Left;
        Top = wa.Top;
        Width = wa.Width;
        Height = wa.Height;
        WindowState = WindowState.Normal;
    }

    private void Facade_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (_liveFacade == null)
            return;

        if (e.PropertyName == nameof(FacadeItemViewModel.LivePreviewImagePath))
        {
            if (_liveFacade.IsRunning && _liveFacade.LivePreviewImagePath != null)
                LoadImage(_liveFacade.LivePreviewImagePath, _liveFacade.FacadeId + " (실시간 미리보기)");
            return;
        }

        // Once the run finishes, switch over to the real mosaic as soon as one
        // shows up (prefer the COLMAP-corrected version, same priority the main
        // window's thumbnail grid uses) — several of these properties can settle
        // in over a couple of ticks as RescanFacadeOutputs catches up.
        if (e.PropertyName is nameof(FacadeItemViewModel.IsRunning)
            or nameof(FacadeItemViewModel.VisualColmapImagePath)
            or nameof(FacadeItemViewModel.VisualImagePath))
        {
            if (_liveFacade.IsRunning)
                return;
            var finalPath = _liveFacade.VisualColmapImagePath ?? _liveFacade.VisualImagePath;
            if (finalPath != null)
                LoadImage(finalPath, _liveFacade.FacadeId + " (완료)");
        }
    }

    // 2026-08-29: 실제로 겪은 버그 -- "무제한 원본 해상도로 로드"라는 이 클래스 원래 취지가
    // 페사드 모자이크 실제 크기(BACK_visual.tif 하나가 42165x36012 = 약 15억 픽셀, Bgr24
    // 기준 원본 픽셀 데이터만 4.5GB)에서는 성립하지 않음을 확인 -- 이 이미지를 DecodePixelWidth
    // 제한 없이 열면 창이 완전히 검게만 나옴(디코드 자체는 됨 -- PixelWidth/Height는 정상 읽힘,
    // 이 앱의 SoftwareOnly 렌더링 모드가 이 정도 크기의 비트맵/엘리먼트를 그리지 못하는 것으로
    // 추정). 헤더만 먼저 가볍게 읽어(DelayCreation, 픽셀 디코드 없음) 원본 폭이 안전 한도를
    // 넘으면 그때만 캡을 걸음 -- 썸네일(700px)보다는 훨씬 세밀하면서도 렌더링이 실패하지 않는
    // 값으로 8000을 선택(일반적인 모니터/줌 배율을 감안해도 넉넉함). 5635x4112처럼 작은
    // 이미지는 그대로 원본 그대로 로드됨(이 클래스의 원래 "무제한 원본" 취지 유지).
    private const int MaxSafeDecodePixelWidth = 8000;

    private void LoadImage(string imagePath, string? titleOverride = null)
    {
        Tag = titleOverride ?? System.IO.Path.GetFileName(imagePath);
        FileNameText.Text = System.IO.Path.GetFileName(imagePath);

        int nativeWidth;
        using (var probeStream = System.IO.File.OpenRead(imagePath))
        {
            var probeDecoder = BitmapDecoder.Create(probeStream, BitmapCreateOptions.DelayCreation, BitmapCacheOption.None);
            nativeWidth = probeDecoder.Frames[0].PixelWidth;
        }

        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        if (nativeWidth > MaxSafeDecodePixelWidth)
            bitmap.DecodePixelWidth = MaxSafeDecodePixelWidth;
        bitmap.UriSource = new Uri(imagePath);
        bitmap.EndInit();
        bitmap.Freeze();
        TheImage.Source = bitmap;
        _pixelWidth = bitmap.PixelWidth;
        _pixelHeight = bitmap.PixelHeight;
        TryLoadFloorRows();

        if (!_userHasZoomedOrPanned && Viewport.ActualWidth > 0 && Viewport.ActualHeight > 0)
        {
            FitToWindow();
        }
        else
        {
            // Keep the user's chosen scale/pan, but the new image may have
            // different pixel dimensions than the last one (e.g. switching from
            // the capped-size live preview to the full-resolution final mosaic)
            // — reapply at the same scale and anchor so Stretch="Fill" doesn't
            // stretch the new bitmap into a box sized for the old one.
            double left = Canvas.GetLeft(TheImage);
            double top = Canvas.GetTop(TheImage);
            ApplyTransform(double.IsNaN(left) ? 0 : left, double.IsNaN(top) ? 0 : top);
        }
    }

    private void Viewport_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (_userHasZoomedOrPanned || _pixelWidth == 0 || Viewport.ActualWidth == 0 || Viewport.ActualHeight == 0)
            return;
        FitToWindow();
    }

    private void FitToWindow()
    {
        _scale = Math.Min(Viewport.ActualWidth / _pixelWidth, Viewport.ActualHeight / _pixelHeight);
        _scale = Math.Min(_scale, 1.0); // never start zoomed in past 100%
        double left = (Viewport.ActualWidth - _pixelWidth * _scale) / 2;
        double top = (Viewport.ActualHeight - _pixelHeight * _scale) / 2;
        ApplyTransform(left, top);
    }

    private void ApplyTransform(double left, double top)
    {
        TheImage.Width = _pixelWidth * _scale;
        TheImage.Height = _pixelHeight * _scale;
        Canvas.SetLeft(TheImage, left);
        Canvas.SetTop(TheImage, top);
        ZoomText.Text = $"{_scale * 100:0}%";
        RedrawFloorLabels(left, top);
        RedrawRegionOverlay(left, top);
    }

    /// <summary>총 층수/층고(BuildingMetadataStore, 사용자 수동 입력) + 이 facade의
    /// px_per_m(scale_colmap.json, calibrated=true일 때만)이 둘 다 있어야 층수를 계산할 수
    /// 있음 -- 하나라도 없으면 _floorRows가 빈 채로 남고(RedrawFloorLabels가 아무것도 안 그림),
    /// 뷰어 자체는 평소처럼 동작한다(floor_labeling_needs_bim.md: 이 값들이 없는 게 정상적인
    /// 기본 상태, 에러 아님).</summary>
    private void TryLoadFloorRows()
    {
        _floorRows = new List<FloorRow>();
        var facade = _facadeContext;
        if (facade is null || string.IsNullOrEmpty(_rootPath) || string.IsNullOrEmpty(facade.OutputDir)
            || string.IsNullOrEmpty(facade.ComplexId) || string.IsNullOrEmpty(facade.BuildingId) || _pixelHeight <= 0)
            return;

        var building = BuildingMetadataStore.Get(_rootPath, facade.ComplexId, facade.BuildingId);
        if (building?.TotalFloors is not int totalFloors || building.FloorHeightM is not double floorHeightM)
            return;

        var scale = FloorLabelCalculator.LoadScale(facade.OutputDir, facade.FacadeId);
        if (scale is null)
            return;

        _floorRows = FloorLabelCalculator.ComputeFloorRows(totalFloors, floorHeightM, scale.PxPerM, _pixelHeight);
    }

    /// <summary>_floorRows(이미지 자체의 픽셀 좌표)를 현재 줌/팬(left/top/_scale)에 맞춰
    /// 화면 좌표로 다시 그림 -- ApplyTransform이 호출될 때마다(줌/드래그/최초 로드) 같이 불림.
    /// 눈금선은 이미지 왼쪽 바깥으로 살짝 튀어나오게(실제 크랙 내용을 가리지 않도록), 층수
    /// 라벨은 그 옆에.</summary>
    private void RedrawFloorLabels(double left, double top)
    {
        FloorLabelCanvas.Children.Clear();
        if (_floorRows.Count == 0)
            return;

        foreach (var row in _floorRows)
        {
            double midScreenY = top + (row.TopPx + row.BottomPx) / 2.0 * _scale;
            if (midScreenY < -20 || midScreenY > Viewport.ActualHeight + 20)
                continue; // 화면 밖 -- 그릴 필요 없음

            var tick = new System.Windows.Shapes.Line
            {
                X1 = left - 14, X2 = left, Y1 = midScreenY, Y2 = midScreenY,
                Stroke = Brushes.Orange, StrokeThickness = 2,
            };
            FloorLabelCanvas.Children.Add(tick);

            var label = new TextBlock
            {
                Text = $"{row.FloorNumber}층",
                Foreground = Brushes.Orange, FontFamily = new FontFamily("Consolas"), FontSize = 12, FontWeight = FontWeights.Bold,
                Background = new SolidColorBrush(Color.FromArgb(160, 11, 13, 14)),
            };
            Canvas.SetLeft(label, left - 60);
            Canvas.SetTop(label, midScreenY - 9);
            FloorLabelCanvas.Children.Add(label);
        }

        if (_floorRows.Count > 0)
        {
            var caption = new TextBlock
            {
                Text = "층수 추정치 (BIM/설계도면 없음, 사용자 입력 기준)",
                Foreground = Brushes.Orange, FontSize = 10.5,
                Background = new SolidColorBrush(Color.FromArgb(160, 11, 13, 14)), Padding = new Thickness(4, 2, 4, 2),
            };
            Canvas.SetLeft(caption, 14);
            Canvas.SetTop(caption, Viewport.ActualHeight - 30);
            FloorLabelCanvas.Children.Add(caption);
        }
    }

    private void Canvas_MouseWheel(object sender, MouseWheelEventArgs e)
    {
        _userHasZoomedOrPanned = true;
        var cursor = e.GetPosition(Viewport);
        double currentLeft = Canvas.GetLeft(TheImage);
        double currentTop = Canvas.GetTop(TheImage);

        double factor = e.Delta > 0 ? 1.15 : 1 / 1.15;
        double newScale = Math.Clamp(_scale * factor, 0.02, 20.0);

        // Keep the point under the cursor fixed while zooming.
        var contentPoint = new Point((cursor.X - currentLeft) / _scale, (cursor.Y - currentTop) / _scale);
        double newLeft = cursor.X - contentPoint.X * newScale;
        double newTop = cursor.Y - contentPoint.Y * newScale;

        _scale = newScale;
        ApplyTransform(newLeft, newTop);
        e.Handled = true;
    }

    private void Canvas_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (_isDrawingRegion)
        {
            RegionCanvas_MouseLeftButtonDown(e);
            return;
        }

        _userHasZoomedOrPanned = true;
        _isDragging = true;
        _dragStart = e.GetPosition(Viewport);
        _panStartLeft = Canvas.GetLeft(TheImage);
        _panStartTop = Canvas.GetTop(TheImage);
        Viewport.CaptureMouse();
    }

    private void Canvas_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_isDragging)
            return;
        var pos = e.GetPosition(Viewport);
        double left = _panStartLeft + (pos.X - _dragStart.X);
        double top = _panStartTop + (pos.Y - _dragStart.Y);
        Canvas.SetLeft(TheImage, left);
        Canvas.SetTop(TheImage, top);
    }

    private void Canvas_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        _isDragging = false;
        Viewport.ReleaseMouseCapture();
    }

    // =====================================================================
    // 2026-09-19: 정면 영역 지정 (다각형 그리기 -> 재검출 -> 재보고서)
    // =====================================================================
    // 설계 검토(사용자 확정): 기본은 자동 벽면 마스크가 계속 담당하고, 이건 "필요할 때만
    // 쓰는 선택적 보정"이다 -- 배치 자동 실행 경로에는 이 UI 자체가 없으므로 자동화를
    // 막지 않는다. 원본 모자이크 TIFF는 절대 잘라내지 않음(#11 provenance 원칙) -- 다각형은
    // {facade_id}_manual_region.json으로 저장되어 크랙 검출 단계의 필터로만 쓰인다
    // (src/geometry/manual_region.py, tools/detect_cracks_folder.py).

    private bool RegionToolAvailable =>
        _facadeContext is { } f && !string.IsNullOrEmpty(f.OutputDir) && !string.IsNullOrEmpty(f.FacadeId)
        && !string.IsNullOrEmpty(_rootPath) && (_liveFacade is null || !_liveFacade.IsRunning);

    private void RegionTool_UpdateVisibility()
    {
        RegionToolPanel.Visibility = RegionToolAvailable ? Visibility.Visible : Visibility.Collapsed;
        RegionStatusText.Text = "";
    }

    private void RegionDrawToggle_Click(object sender, RoutedEventArgs e)
    {
        _isDrawingRegion = RegionDrawToggle.IsChecked == true;
        HintText.Text = _isDrawingRegion
            ? "클릭: 꼭짓점 추가 · 더블클릭: 다각형 완료 · Esc: 닫기"
            : "휠: 확대/축소 · 드래그: 이동 · Esc: 닫기";
        if (!_isDrawingRegion && _currentPolygon.Count >= 3)
        {
            _completedPolygons.Add(_currentPolygon);
            _currentPolygon = new List<Point>();
        }
        RegionApplyButton.IsEnabled = _completedPolygons.Count > 0 && !_isRegionBusy;
        RedrawRegionOverlay(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private void RegionClearButton_Click(object sender, RoutedEventArgs e)
    {
        _completedPolygons.Clear();
        _currentPolygon = new List<Point>();
        RegionApplyButton.IsEnabled = false;
        RegionStatusText.Text = "";
        RedrawRegionOverlay(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    /// <summary>화면(스크린) 좌표를 "이미지 자신의 픽셀 좌표"로 환산 -- 지금 줌/팬(_scale,
    /// TheImage의 Canvas.Left/Top)을 역으로 풀면 된다. manual_region.json은 항상 이
    /// 좌표계로 저장되므로, 나중에 다른 줌 배율로 다시 열어도 같은 자리를 가리킨다.</summary>
    private Point ScreenToImagePoint(Point screen)
    {
        double left = Canvas.GetLeft(TheImage);
        double top = Canvas.GetTop(TheImage);
        return new Point((screen.X - left) / _scale, (screen.Y - top) / _scale);
    }

    private void RegionCanvas_MouseLeftButtonDown(MouseButtonEventArgs e)
    {
        var screenPoint = e.GetPosition(Viewport);
        var imagePoint = ScreenToImagePoint(screenPoint);

        if (e.ClickCount >= 2)
        {
            // 더블클릭: 지금 그리던 다각형을 닫는다. 마지막 클릭에서 방금 추가된
            // 꼭짓점(더블클릭의 첫 클릭분)은 이미 아래 단일-클릭 처리에서 들어갔으므로,
            // 여기서는 다각형을 완료 처리만 한다.
            if (_currentPolygon.Count >= 3)
            {
                _completedPolygons.Add(_currentPolygon);
                _currentPolygon = new List<Point>();
                RegionApplyButton.IsEnabled = true;
                RegionStatusText.Text = $"다각형 {_completedPolygons.Count}개 완료";
            }
            else
            {
                RegionStatusText.Text = "꼭짓점 3개 이상 찍어야 다각형이 됩니다";
            }
            e.Handled = true;
            RedrawRegionOverlay(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
            return;
        }

        _currentPolygon.Add(imagePoint);
        e.Handled = true;
        RedrawRegionOverlay(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    /// <summary>_completedPolygons + 그리는 중인 _currentPolygon을 지금 줌/팬 기준
    /// 화면 좌표로 다시 그림 -- ApplyTransform(줌/팬)마다, 그리고 꼭짓점을 찍을 때마다 호출.</summary>
    private void RedrawRegionOverlay(double left, double top)
    {
        RegionDrawCanvas.Children.Clear();
        if (!RegionToolAvailable)
            return;

        Point ToScreen(Point imagePt) => new(left + imagePt.X * _scale, top + imagePt.Y * _scale);

        void DrawPolygon(List<Point> polyImagePts, bool closed, Brush stroke, Brush fill)
        {
            if (polyImagePts.Count == 0)
                return;
            var poly = new Polygon
            {
                Stroke = stroke,
                StrokeThickness = 2,
                Fill = closed ? fill : Brushes.Transparent,
            };
            foreach (var p in polyImagePts)
                poly.Points.Add(ToScreen(p));
            RegionDrawCanvas.Children.Add(poly);

            foreach (var p in polyImagePts)
            {
                var screenP = ToScreen(p);
                var dot = new Ellipse
                {
                    Width = 8, Height = 8, Fill = stroke,
                };
                Canvas.SetLeft(dot, screenP.X - 4);
                Canvas.SetTop(dot, screenP.Y - 4);
                RegionDrawCanvas.Children.Add(dot);
            }
        }

        var doneStroke = new SolidColorBrush(Color.FromRgb(0x4C, 0xAF, 0x50));
        var doneFill = new SolidColorBrush(Color.FromArgb(0x33, 0x4C, 0xAF, 0x50));
        foreach (var poly in _completedPolygons)
            DrawPolygon(poly, closed: true, doneStroke, doneFill);

        if (_currentPolygon.Count > 0)
        {
            var activeStroke = new SolidColorBrush(Color.FromRgb(0xF1, 0xC4, 0x0F));
            DrawPolygon(_currentPolygon, closed: false, activeStroke, Brushes.Transparent);
        }
    }

    private async void RegionApplyButton_Click(object sender, RoutedEventArgs e)
    {
        if (_isRegionBusy || _facadeContext is not { } facade || string.IsNullOrEmpty(_rootPath))
            return;
        if (_currentPolygon.Count >= 3)
        {
            _completedPolygons.Add(_currentPolygon);
            _currentPolygon = new List<Point>();
        }
        if (_completedPolygons.Count == 0 || string.IsNullOrEmpty(facade.OutputDir))
            return;

        _isRegionBusy = true;
        RegionApplyButton.IsEnabled = false;
        RegionDrawToggle.IsEnabled = false;
        RegionClearButton.IsEnabled = false;
        try
        {
            RegionStatusText.Text = "영역 저장 중...";
            var regionPath = System.IO.Path.Combine(facade.OutputDir, $"{facade.FacadeId}_manual_region.json");
            SaveManualRegionJson(regionPath, _pixelWidth, _pixelHeight, _completedPolygons);

            RegionStatusText.Text = "크랙 재검출 중... (몇 분 소요될 수 있습니다)";
            var detectOk = await RunPythonScriptAsync(
                System.IO.Path.Combine("tools", "detect_cracks_folder.py"), facade.OutputDir, facade.FacadeId);
            if (!detectOk.Success)
            {
                RegionStatusText.Text = $"크랙 재검출 실패: {detectOk.ErrorSummary}";
                return;
            }

            RegionStatusText.Text = "보고서 재생성 중...";
            var reportOk = await RunPythonScriptAsync(
                System.IO.Path.Combine("tools", "generate_report.py"), "facade", facade.OutputDir, facade.FacadeId);
            if (!reportOk.Success)
            {
                RegionStatusText.Text = $"보고서 생성 실패: {reportOk.ErrorSummary}";
                return;
            }

            RegionStatusText.Text = "완료 -- 정면 영역 기준으로 재검출/보고서 갱신됨";
            MessageBox.Show("정면 영역 기준으로 크랙 재검출 + 보고서 재생성을 완료했습니다.", "완료",
                MessageBoxButton.OK, MessageBoxImage.Information);
        }
        catch (Exception ex)
        {
            RegionStatusText.Text = $"실패: {ex.Message}";
        }
        finally
        {
            _isRegionBusy = false;
            RegionDrawToggle.IsEnabled = true;
            RegionClearButton.IsEnabled = true;
            RegionApplyButton.IsEnabled = _completedPolygons.Count > 0;
        }
    }

    /// <summary>src/geometry/manual_region.py::save_manual_region이 읽는 것과 정확히 같은
    /// 스키마 -- 파이썬 쪽을 다시 안 부르고 여기서 직접 쓴다(단순 JSON이라 왕복 호출이
    /// 과함). 좌표는 이미 ScreenToImagePoint로 이미지 픽셀 공간으로 저장돼 있음.</summary>
    private static void SaveManualRegionJson(string path, int canvasWidth, int canvasHeight, List<List<Point>> polygons)
    {
        var payload = new
        {
            canvas_width = canvasWidth,
            canvas_height = canvasHeight,
            polygons = polygons.Select(poly => poly.Select(p => new[] { Math.Round(p.X, 1), Math.Round(p.Y, 1) })).ToList(),
        };
        var json = JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true });
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, json, Encoding.UTF8);
        File.Delete(path);
        File.Move(tmp, path);
    }

    /// <summary>MainViewModel.RunFacade / ResultsCompareViewModel.RegenerateFinalReportCore와
    /// 동일한 서브프로세스 실행 패턴 -- 이 창은 독립 코드비하인드라 그 ViewModel들을 직접
    /// 재사용하지 않고 같은 패턴만 그대로 따른다.</summary>
    private async Task<(bool Success, string ErrorSummary)> RunPythonScriptAsync(string scriptRelativePath, params string[] args)
    {
        var scriptPath = System.IO.Path.Combine(_rootPath!, scriptRelativePath);
        var psi = new ProcessStartInfo
        {
            FileName = PythonEnvironment.DiscoverPythonExe(),
            WorkingDirectory = _rootPath,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.ArgumentList.Add(scriptPath);
        foreach (var arg in args)
            psi.ArgumentList.Add(arg);

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
                var lines = stderr.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
                var summary = lines.Length > 0 ? lines[^1] : $"exit code {process.ExitCode}";
                return (false, summary);
            }
            return (true, "");
        }
        finally
        {
            ChildProcessRegistry.Unregister(process);
        }
    }
}
