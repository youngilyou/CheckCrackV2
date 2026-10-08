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

    // 층 선 (2026-10-09, 사용자 확정 "권장 방식"): {facade}_floors.json의 자동 제안(옥상 아래 첫 층 경계 + 층 간격)을
    // 담당자가 확인/조정하고 총 층수를 넣어 확정한다. 확정값만 층 번호의 근거(보고서/DB) -- 예전 방식(모자이크 맨 위 =
    // 최상층 천장 가정)은 Dense 모자이크 위쪽의 하늘/옥상 때문에 층이 밀려서 대체됨. 좌표는 전부 모자이크 원본 픽셀
    // (_floorFile의 캔버스)이고, 화면에는 _floorScale(디코드 축소 비율)을 곱해 그린다.
    private FacadeItemViewModel? _facadeContext;
    private string? _rootPath;
    private int _nativeWidth, _nativeHeight;
    private FloorSettingFile? _floorFile;
    private double _floorRoof, _floorPitch; // working values, native px
    private int? _floorTotal;
    private bool _floorConfirmed;
    private bool _floorDirty;
    private bool _suppressFloorInput;
    private int _draggingFloorLine = -1; // 0 = top line (moves all), 1 = second line (changes the pitch)
    private double FloorScale => _nativeHeight > 0 ? (double)_pixelHeight / _nativeHeight : 1.0;

    // 2026-09-19: 정면 영역 지정(다각형 그리기) 상태. 좌표는 전부 "이미지 자신의 픽셀 공간"
    // (파이썬 쪽 manual_region.json이 읽는 것과 같은 좌표계) -- 화면 좌표는 줌/팬마다 바뀌므로
    // 절대 여기에 저장하지 않고, RedrawRegionOverlay가 그릴 때마다 현재 _scale/left/top으로
    // 다시 변환한다.
    private bool _isDrawingRegion;
    private readonly List<List<Point>> _completedPolygons = new();
    private List<Point> _currentPolygon = new();
    private bool _isRegionBusy;

    // 2026-09-21 사용자 요청("점을 선택해서 위치 이동이 되게"): 잘못 찍은 꼭짓점 하나 고치자고
    // 다각형을 처음부터 다시 그릴 필요 없이, 기존 점을 클릭&드래그로 옮길 수 있게.
    // _draggingVertexPolygon: NotDraggingVertex(드래그 중 아님) / CurrentPolygonMarker(그리는
    // 중인 다각형) / 0 이상(그 인덱스의 완성된 다각형).
    private const int NotDraggingVertex = -2;
    private const int CurrentPolygonMarker = -1;
    private const double VertexHitRadiusPx = 10;
    private int _draggingVertexPolygon = NotDraggingVertex;
    private int _draggingVertexIndex = -1;

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
            or nameof(FacadeItemViewModel.EffectiveVisualImagePath))
        {
            if (_liveFacade.IsRunning)
                return;
            var finalPath = _liveFacade.EffectiveVisualImagePath;
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
            _nativeHeight = probeDecoder.Frames[0].PixelHeight;
        }
        _nativeWidth = nativeWidth;

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
        FloorTool_Init();

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

    // =====================================================================
    // 2026-10-09: 층 설정 (자동 제안 -> 담당자 확인/조정 -> 확정 저장 -> 보고서/DB)
    // =====================================================================

    private bool FloorToolAvailable => _floorFile != null;

    /// <summary>Loads {facade}_floors.json when this window shows that facade's COLMAP/Dense mosaic (same canvas size
    /// as the file). Any other image (H체인 모자이크, 미리보기, 원본 사진) gets no floor tool and no floor lines.</summary>
    private void FloorTool_Init()
    {
        _floorFile = null;
        var facade = _facadeContext;
        if (facade != null && !string.IsNullOrEmpty(facade.OutputDir) && !string.IsNullOrEmpty(facade.FacadeId)
            && (_liveFacade is null || !_liveFacade.IsRunning))
        {
            var file = FloorSettingStore.Load(facade.OutputDir, facade.FacadeId);
            if (file != null && file.CanvasWidth == _nativeWidth && file.CanvasHeight == _nativeHeight)
                _floorFile = file;
        }
        FloorToolPanel.Visibility = FloorToolAvailable ? Visibility.Visible : Visibility.Collapsed;
        if (!FloorToolAvailable)
            return;
        LoadFloorWorkingValues(preferConfirmed: true);
    }

    private void LoadFloorWorkingValues(bool preferConfirmed)
    {
        var file = _floorFile!;
        var confirmed = preferConfirmed ? file.Confirmed : null;
        if (confirmed is { PitchPx: > 0, TotalFloors: > 0 })
        {
            _floorRoof = confirmed.RoofRowPx;
            _floorPitch = confirmed.PitchPx;
            _floorTotal = confirmed.TotalFloors;
            _floorConfirmed = true;
        }
        else
        {
            _floorRoof = file.Suggested?.RoofRowPx ?? file.Suggested?.WallTopRowPx ?? 0;
            _floorPitch = file.Suggested?.PitchPx ?? 0;
            _floorTotal = file.Confirmed?.TotalFloors > 0 ? file.Confirmed.TotalFloors : BuildingTotalFloors();
            _floorConfirmed = false;
        }
        _floorDirty = false;
        _suppressFloorInput = true;
        FloorTotalBox.Text = _floorTotal?.ToString() ?? "";
        FloorPitchBox.Text = _floorPitch > 0 && file.PxPerM > 0 ? (_floorPitch / file.PxPerM).ToString("0.###") : "";
        _suppressFloorInput = false;
        UpdateFloorStatus();
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    /// <summary>Total floor count entered for this building in "동 정보" (BuildingMetadataStore), as a prefill.</summary>
    private int? BuildingTotalFloors()
    {
        var f = _facadeContext;
        if (f == null || string.IsNullOrEmpty(_rootPath) || string.IsNullOrEmpty(f.ComplexId) || string.IsNullOrEmpty(f.BuildingId))
            return null;
        return BuildingMetadataStore.Get(_rootPath, f.ComplexId, f.BuildingId)?.TotalFloors;
    }

    private void UpdateFloorStatus(string? extra = null)
    {
        if (_floorFile == null)
            return;
        var pxPerM = _floorFile.PxPerM;
        var pitchM = _floorPitch > 0 && pxPerM > 0 ? _floorPitch / pxPerM : (double?)null;
        var lines = new List<string>();
        if (_floorConfirmed && !_floorDirty && _floorFile.Confirmed is { } c)
            lines.Add($"확정됨 ({c.ConfirmedBy ?? "-"}, {c.ConfirmedAt?.ToLocalTime():yyyy-MM-dd HH:mm}) · 총 {c.TotalFloors}층 · 층 간격 {pitchM:0.00} m. "
                      + "보고서/DB 층 번호는 이 값으로 계산됩니다 (BIM/도면 아님, 담당자 확인값).");
        else
        {
            var s = _floorFile.Suggested;
            lines.Add(s?.PitchPx is > 0
                ? $"자동 제안(미확정): 층 간격 {s.PitchM:0.00} m (반복 패턴 점수 {s.PatternScore:0.00}), 첫 층 경계 y={s.RoofRowPx:0}."
                : "자동 제안 실패: 층이 반복되는 패턴을 찾지 못했습니다. 층 간격(m)을 직접 입력하세요.");
            lines.Add("선이 슬래브 경계와 맞는지 확인하고(맞지 않으면 '선 이동'), 총 층수를 넣어 '확정 저장'해야 보고서/DB에 층이 나옵니다.");
        }
        if (pitchM is double pm && _facadeContext is { } f && !string.IsNullOrEmpty(_rootPath)
            && !string.IsNullOrEmpty(f.ComplexId) && !string.IsNullOrEmpty(f.BuildingId)
            && BuildingMetadataStore.Get(_rootPath, f.ComplexId, f.BuildingId)?.FloorHeightM is double entered
            && Math.Abs(entered - pm) / entered > 0.05)
            lines.Add($"주의: 동 정보에 입력된 층고 {entered:0.00} m와 이미지에서 잰 층 간격 {pm:0.00} m가 5% 넘게 다릅니다.");
        if (_floorPitch > 0 && _floorTotal is int total && _floorFile.Suggested?.WallBottomRowPx is double wallBottom
            && (wallBottom - _floorRoof) / _floorPitch > total + 0.5)
            lines.Add($"주의: 사진에 보이는 층({(wallBottom - _floorRoof) / _floorPitch:0.0}개)이 총 층수 {total}보다 많습니다. 맨 위 선 위치나 총 층수를 확인하세요.");
        if (extra != null)
            lines.Add(extra);
        FloorStatusText.Text = string.Join("\n", lines);
    }

    /// <summary>Floor boundary lines across the mosaic + "N층" labels at the left edge, in screen coordinates for the
    /// current zoom/pan. Dashed while not confirmed. Without a total floor count only the lines are drawn.</summary>
    private void RedrawFloorLabels(double left, double top)
    {
        FloorLabelCanvas.Children.Clear();
        if (!FloorToolAvailable || _floorPitch <= 0 || double.IsNaN(left) || double.IsNaN(top))
            return;

        var k2s = _scale * FloorScale; // native px -> screen px
        var width = _nativeWidth * k2s;
        var editing = FloorEditToggle.IsChecked == true;
        var solid = _floorConfirmed && !_floorDirty;
        var maxLines = _floorTotal is int t ? t + 1 : (int)Math.Ceiling((_nativeHeight - _floorRoof) / _floorPitch) + 1;
        for (var k = 0; k < maxLines; k++)
        {
            var yNative = _floorRoof + k * _floorPitch;
            if (yNative > _nativeHeight)
                break;
            var y = top + yNative * k2s;
            if (y >= -20 && y <= Viewport.ActualHeight + 20)
            {
                var line = new Line
                {
                    X1 = left - 14, X2 = left + width, Y1 = y, Y2 = y,
                    Stroke = editing && k <= 1 ? Brushes.Yellow : Brushes.Orange,
                    StrokeThickness = editing && k <= 1 ? 3 : 1.5,
                    Opacity = 0.85,
                };
                if (!solid)
                    line.StrokeDashArray = new DoubleCollection { 6, 4 };
                FloorLabelCanvas.Children.Add(line);
            }

            if (_floorTotal is int total && k < total)
            {
                var midY = top + (yNative + _floorPitch / 2) * k2s;
                if (midY < -20 || midY > Viewport.ActualHeight + 20)
                    continue;
                var label = new TextBlock
                {
                    Text = $"{total - k}층",
                    Foreground = Brushes.Orange, FontFamily = new FontFamily("Consolas"), FontSize = 12, FontWeight = FontWeights.Bold,
                    Background = new SolidColorBrush(Color.FromArgb(170, 11, 13, 14)), Padding = new Thickness(3, 0, 3, 0),
                };
                Canvas.SetLeft(label, left - 58);
                Canvas.SetTop(label, midY - 9);
                FloorLabelCanvas.Children.Add(label);
            }
        }

        var caption = new TextBlock
        {
            Text = solid ? "층 표시: 담당자 확정값 (BIM/설계도면 아님)" : "층 표시: 자동 제안 (미확정)",
            Foreground = Brushes.Orange, FontSize = 10.5,
            Background = new SolidColorBrush(Color.FromArgb(170, 11, 13, 14)), Padding = new Thickness(4, 2, 4, 2),
        };
        Canvas.SetLeft(caption, 14);
        Canvas.SetTop(caption, Viewport.ActualHeight - 30);
        FloorLabelCanvas.Children.Add(caption);
    }

    /// <summary>0 = near the top line, 1 = near the second line, null = neither (within 8 screen px).</summary>
    private int? FloorHitTest(Point screen)
    {
        if (!FloorToolAvailable || _floorPitch <= 0)
            return null;
        var top = Canvas.GetTop(TheImage);
        var k2s = _scale * FloorScale;
        for (var k = 0; k <= 1; k++)
            if (Math.Abs(top + (_floorRoof + k * _floorPitch) * k2s - screen.Y) <= 8)
                return k;
        return null;
    }

    private void FloorDragTo(Point screen)
    {
        var yNative = (screen.Y - Canvas.GetTop(TheImage)) / (_scale * FloorScale);
        if (_draggingFloorLine == 0)
            _floorRoof = Math.Clamp(yNative, 0, _nativeHeight);
        else
        {
            var minPitch = _floorFile!.PxPerM > 0 ? 1.5 * _floorFile.PxPerM : 50; // never below 1.5 m
            _floorPitch = Math.Max(minPitch, yNative - _floorRoof);
            _suppressFloorInput = true;
            FloorPitchBox.Text = _floorFile.PxPerM > 0 ? (_floorPitch / _floorFile.PxPerM).ToString("0.###") : "";
            _suppressFloorInput = false;
        }
        _floorDirty = true;
        UpdateFloorStatus();
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private void FloorInput_Changed(object sender, TextChangedEventArgs e)
    {
        if (_suppressFloorInput || !FloorToolAvailable)
            return;
        _floorTotal = int.TryParse(FloorTotalBox.Text.Trim(), out var n) && n > 0 && n < 300 ? n : null;
        _floorDirty = true;
        UpdateFloorStatus();
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private void FloorPitchBox_LostFocus(object sender, RoutedEventArgs e)
    {
        if (_suppressFloorInput || !FloorToolAvailable || _floorFile!.PxPerM <= 0)
            return;
        if (double.TryParse(FloorPitchBox.Text.Trim(), out var m) && m >= 1.5 && m <= 10)
        {
            _floorPitch = m * _floorFile.PxPerM;
            _floorDirty = true;
        }
        else
            FloorPitchBox.Text = _floorPitch > 0 ? (_floorPitch / _floorFile.PxPerM).ToString("0.###") : "";
        UpdateFloorStatus();
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private void FloorEditToggle_Click(object sender, RoutedEventArgs e)
    {
        HintText.Text = FloorEditToggle.IsChecked == true
            ? "노란 선을 끌어서 조정: 맨 위 선 = 전체 이동, 두 번째 선 = 층 간격 · 휠: 확대/축소 · Esc: 닫기"
            : "휠: 확대/축소 · 드래그: 이동 · Esc: 닫기";
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private void FloorResetButton_Click(object sender, RoutedEventArgs e)
    {
        if (FloorToolAvailable)
            LoadFloorWorkingValues(preferConfirmed: false);
    }

    private async void FloorConfirmButton_Click(object sender, RoutedEventArgs e)
    {
        if (!FloorToolAvailable || _facadeContext is not { } facade || string.IsNullOrEmpty(facade.OutputDir))
            return;
        if (_floorTotal is not int total || _floorPitch <= 0)
        {
            UpdateFloorStatus("확정하려면 총 층수와 층 간격이 필요합니다.");
            return;
        }
        var user = (Application.Current?.MainWindow?.DataContext as MainViewModel)?.LoggedInUsername;
        var confirmed = new FloorConfirmed
        {
            RoofRowPx = Math.Round(_floorRoof, 1), PitchPx = Math.Round(_floorPitch, 2), TotalFloors = total,
            ConfirmedBy = string.IsNullOrWhiteSpace(user) ? null : user, ConfirmedAt = DateTimeOffset.Now,
        };
        try
        {
            FloorSettingStore.SaveConfirmed(facade.OutputDir, facade.FacadeId, confirmed);
            _floorFile!.Confirmed = confirmed;
            _floorConfirmed = true;
            _floorDirty = false;
            // keep "동 정보" in step (prefill for the building's other facades)
            if (!string.IsNullOrEmpty(_rootPath) && !string.IsNullOrEmpty(facade.ComplexId) && !string.IsNullOrEmpty(facade.BuildingId))
            {
                var entry = BuildingMetadataStore.Get(_rootPath, facade.ComplexId, facade.BuildingId)
                            ?? new BuildingMetadataEntry { ComplexId = facade.ComplexId, BuildingId = facade.BuildingId };
                entry.TotalFloors = total;
                BuildingMetadataStore.Upsert(_rootPath, entry);
            }
        }
        catch (Exception ex)
        {
            UpdateFloorStatus($"저장 실패: {ex.Message}");
            return;
        }
        UpdateFloorStatus("저장했습니다. 보고서에는 다음 '보고서 생성/최종 보고서 재생성' 때 반영됩니다." + await PushFloorsToServerAsync(facade, confirmed));
        RedrawFloorLabels(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
    }

    private async void FloorClearButton_Click(object sender, RoutedEventArgs e)
    {
        if (!FloorToolAvailable || _facadeContext is not { } facade || string.IsNullOrEmpty(facade.OutputDir))
            return;
        try
        {
            FloorSettingStore.SaveConfirmed(facade.OutputDir, facade.FacadeId, null);
            _floorFile!.Confirmed = null;
        }
        catch (Exception ex)
        {
            UpdateFloorStatus($"저장 실패: {ex.Message}");
            return;
        }
        LoadFloorWorkingValues(preferConfirmed: false);
        UpdateFloorStatus("확정을 해제했습니다 (층 번호 없음)." + await PushFloorsToServerAsync(facade, null));
    }

    /// <summary>When this result folder belongs to a MngData archive, writes the floor setting to the DB right away
    /// (crackvision_facades.floor_*, crackvision_cracks.floor_min/max). Returns a status suffix.</summary>
    private static async Task<string> PushFloorsToServerAsync(FacadeItemViewModel facade, FloorConfirmed? confirmed)
    {
        var baseDir = System.IO.Path.GetDirectoryName(facade.OutputDir!.TrimEnd('\\', '/'));
        var archiveId = facade.ArchiveId ?? (baseDir != null ? ArchiveLinkStore.TryLoad(baseDir)?.ArchiveId : null);
        if (archiveId is not long id)
            return " (서버 archive와 연결되지 않은 폴더라 DB는 갱신하지 않았습니다.)";
        try
        {
            var settings = CrackVisionDbSettingsStore.Load();
            if (string.IsNullOrWhiteSpace(settings.PostgresHost))
                return " (DB 접속 정보가 없어 서버는 갱신하지 않았습니다.)";
            var ok = await Task.Run(() => CrackVisionArchiveQueryService.UpdateFloorsAsync(settings, id, facade.FacadeId, confirmed));
            return ok ? $" 서버(archive #{id}) 층 정보도 갱신했습니다." : $" (서버 archive #{id}에 이 면의 결과가 아직 없어 DB는 다음 결과 저장 때 반영됩니다.)";
        }
        catch (Exception ex)
        {
            return $" (서버 갱신 실패: {ex.Message})";
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
        if (FloorEditToggle.IsChecked == true && FloorHitTest(e.GetPosition(Viewport)) is int line)
        {
            _draggingFloorLine = line;
            Viewport.CaptureMouse();
            e.Handled = true;
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
        if (_draggingFloorLine >= 0)
        {
            FloorDragTo(e.GetPosition(Viewport));
            return;
        }
        if (_draggingVertexPolygon != NotDraggingVertex)
        {
            var imagePoint = ScreenToImagePoint(e.GetPosition(Viewport));
            if (_draggingVertexPolygon == CurrentPolygonMarker)
                _currentPolygon[_draggingVertexIndex] = imagePoint;
            else
                _completedPolygons[_draggingVertexPolygon][_draggingVertexIndex] = imagePoint;
            RedrawRegionOverlay(Canvas.GetLeft(TheImage), Canvas.GetTop(TheImage));
            return;
        }

        if (!_isDragging)
            return;
        var pos = e.GetPosition(Viewport);
        double left = _panStartLeft + (pos.X - _dragStart.X);
        double top = _panStartTop + (pos.Y - _dragStart.Y);
        Canvas.SetLeft(TheImage, left);
        Canvas.SetTop(TheImage, top);
        RedrawFloorLabels(left, top);
    }

    private void Canvas_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (_draggingFloorLine >= 0)
        {
            _draggingFloorLine = -1;
            Viewport.ReleaseMouseCapture();
            return;
        }
        if (_draggingVertexPolygon != NotDraggingVertex)
        {
            _draggingVertexPolygon = NotDraggingVertex;
            _draggingVertexIndex = -1;
            Viewport.ReleaseMouseCapture();
            return;
        }
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

    private Point ImageToScreenPoint(Point imagePoint)
    {
        double left = Canvas.GetLeft(TheImage);
        double top = Canvas.GetTop(TheImage);
        return new Point(left + imagePoint.X * _scale, top + imagePoint.Y * _scale);
    }

    /// <summary>화면 클릭 지점 근처(<see cref="VertexHitRadiusPx"/> 이내)에 이미 찍힌 꼭짓점이
    /// 있는지 검사 -- 완성된 다각형들을 먼저, 그리는 중인 다각형을 나중에 본다(그리는 중인
    /// 다각형의 꼭짓점이 화면상 겹쳐 보일 때 방금 찍은 점이 우선권을 갖도록).</summary>
    private bool TryHitTestVertex(Point screenPoint, out int polygonIndex, out int vertexIndex)
    {
        for (int i = 0; i < _completedPolygons.Count; i++)
        {
            var poly = _completedPolygons[i];
            for (int v = 0; v < poly.Count; v++)
            {
                if ((ImageToScreenPoint(poly[v]) - screenPoint).Length <= VertexHitRadiusPx)
                {
                    polygonIndex = i;
                    vertexIndex = v;
                    return true;
                }
            }
        }
        for (int v = 0; v < _currentPolygon.Count; v++)
        {
            if ((ImageToScreenPoint(_currentPolygon[v]) - screenPoint).Length <= VertexHitRadiusPx)
            {
                polygonIndex = CurrentPolygonMarker;
                vertexIndex = v;
                return true;
            }
        }
        polygonIndex = NotDraggingVertex;
        vertexIndex = -1;
        return false;
    }

    private void RegionCanvas_MouseLeftButtonDown(MouseButtonEventArgs e)
    {
        var screenPoint = e.GetPosition(Viewport);

        if (e.ClickCount == 1 && TryHitTestVertex(screenPoint, out int hitPoly, out int hitVertex))
        {
            _draggingVertexPolygon = hitPoly;
            _draggingVertexIndex = hitVertex;
            Viewport.CaptureMouse();
            e.Handled = true;
            return;
        }

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

        // 2026-09-19 사용자 요청("이외 지역은 어둡게"): 완성된 다각형이 하나라도 있으면
        // 그 다각형들의 합집합 밖(=크랙 검출에서 제외될 영역)을 반투명 검정으로 덮어서
        // "이 부분은 범위 밖"이라는 걸 그리는 즉시 눈으로 바로 알 수 있게 함. 전체 이미지
        // 사각형과 각 다각형을 한 PathGeometry에 FillRule=EvenOdd로 같이 넣으면, 사각형과
        // 다각형이 겹치는 부분(=다각형 내부)만 짝수 겹침이라 안 채워지고 나머지가 채워짐 --
        // 별도 마스킹/클리핑 계산 없이 다중 다각형(구멍 여러 개)도 그대로 처리됨.
        if (_completedPolygons.Count > 0)
        {
            var dimGeometry = new PathGeometry { FillRule = FillRule.EvenOdd };
            var fullRectFigure = new PathFigure { IsClosed = true, StartPoint = ToScreen(new Point(0, 0)) };
            fullRectFigure.Segments.Add(new PolyLineSegment(new[]
            {
                ToScreen(new Point(_pixelWidth, 0)),
                ToScreen(new Point(_pixelWidth, _pixelHeight)),
                ToScreen(new Point(0, _pixelHeight)),
            }, isStroked: false));
            dimGeometry.Figures.Add(fullRectFigure);

            foreach (var poly in _completedPolygons)
            {
                if (poly.Count < 3)
                    continue;
                var holeFigure = new PathFigure { IsClosed = true, StartPoint = ToScreen(poly[0]) };
                holeFigure.Segments.Add(new PolyLineSegment(poly.Skip(1).Select(ToScreen), isStroked: false));
                dimGeometry.Figures.Add(holeFigure);
            }

            RegionDrawCanvas.Children.Add(new System.Windows.Shapes.Path
            {
                Data = dimGeometry,
                Fill = new SolidColorBrush(Color.FromArgb(0x99, 0x00, 0x00, 0x00)),
            });
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
        // new UTF8Encoding(false): plain Encoding.UTF8 writes a leading BOM, which
        // Python's json.loads(text) rejects outright (JSONDecodeError) -- confirmed
        // real, 2026-09-19: every manual_region.json this method had ever written
        // silently failed to load on the Python side (load_manual_region_mask's
        // except clause swallowed the error), so the dim overlay/crack filter never
        // actually activated despite the file existing and looking fine on disk.
        File.WriteAllText(tmp, json, new UTF8Encoding(false));
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
