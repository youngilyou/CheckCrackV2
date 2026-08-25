using System;
using System.Collections.Generic;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Shapes;
using CheckCrackViewer.Models;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

/// <summary>"원본 보기" -- 스티칭 모자이크에서 구분하기 어려운 크랙을 원본(비왜곡) 사진에서
/// 직접 확인하기 위한 별도 창. ResultsCompareView.xaml.cs가 이 창의 생명주기를 소유하고,
/// ResultsCompareViewModel.SelectedReviewItem이 바뀔 때마다(캔버스 번호 배지 클릭 또는
/// 목록에서 선택) ShowCrack을 호출해 갱신한다 -- ImageViewerWindow의 "live" 생성자가
/// FacadeItemViewModel.PropertyChanged를 구독하는 것과 같은 패턴.
///
/// 줌/팬은 ImageViewerWindow.xaml.cs와 동일하게 Width/Height/Canvas.Left/Top을 매 스텝
/// 직접 설정한다(RenderTransform은 이 앱의 소프트웨어 렌더링에서 이미지 일부만 그리는
/// 문제가 있음, 그 파일의 클래스 주석 참고). 다른 점은 "처음 여는 기준"이 화면 전체 맞춤이
/// 아니라 크랙의 bbox_px_in_source를 화면 중앙에 오도록 확대/이동한다는 것 -- 사용자 요청:
/// "위치가 중심에 와야 함".
///
/// Prev/Next는 이 창에 표시된 크랙 하나의 SourceObservations 안에서만 넘어간다(facade
/// 전체 원본 목록이 아님) -- 사용자 확정: "Next/Previous는 source_observations 내부
/// 이미지만 넘기도록 하면 훨씬 자연스럽습니다".</summary>
public partial class OriginalCrackViewerWindow : Window
{
    private List<SourceObservationModel> _observations = new();
    private int _index;
    private string _crackId = "";
    private int _pixelWidth, _pixelHeight;
    private double _scale = 1.0;
    private bool _userHasZoomedOrPanned;
    private double[]? _currentBboxPxInSource; // [x0,y0,x1,y1] in the CURRENT image's own pixel space

    public OriginalCrackViewerWindow()
    {
        InitializeComponent();
        InitWindowBounds();
        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
        SetHasImage(false); // 창을 막 열었을 때(아직 ShowCrack 호출 전)는 보여줄 이미지가
                            // 없으므로 Esc 닫기 안내 말고는 전부 숨긴다(사용자 요청).
    }

    /// <summary>Shows/hides every control that only makes sense once an actual
    /// image is on screen -- both "창을 막 열었고 아직 크랙을 선택 안 함" and
    /// "크랙은 선택했지만 source_observations가 비어있음" end up with no image,
    /// so both call this the same way rather than duplicating the hide list.</summary>
    private void SetHasImage(bool hasImage)
    {
        var visible = hasImage ? Visibility.Visible : Visibility.Collapsed;
        CrackIdText.Visibility = visible;
        RecenterButton.Visibility = visible;
        FileNameText.Visibility = visible;
        ZoomText.Visibility = visible;
        HintText.Text = hasImage ? "휠: 확대/축소 · 드래그: 이동 · Esc: 닫기" : "Esc: 닫기";
        if (!hasImage)
        {
            PrevButton.Visibility = Visibility.Collapsed;
            NextButton.Visibility = Visibility.Collapsed;
            PositionText.Visibility = Visibility.Collapsed;
        }
    }

    private void InitWindowBounds()
    {
        // Same reasoning as ImageViewerWindow.InitWindowBounds -- WindowState=
        // Maximized resolves through an OS animation that fires SizeChanged at
        // a small pre-maximize size first on this (remote/software-rendered)
        // machine, so the final bounds are set directly instead.
        var wa = SystemParameters.WorkArea;
        Left = wa.Left;
        Top = wa.Top;
        Width = wa.Width;
        Height = wa.Height;
        WindowState = WindowState.Normal;
    }

    /// <summary>Called by ResultsCompareView.xaml.cs whenever the selected crack
    /// changes while this window is open. Always jumps to source_observations[0]
    /// (the best/most-owned-pixels photo) and resets zoom/pan so the new crack
    /// is centered -- a different crack means the previous pan position is
    /// meaningless anyway.</summary>
    public void ShowCrack(CrackReviewItem item, ResultsCompareViewModel vm)
    {
        _crackId = item.CrackId;
        _observations = item.SourceObservations;
        _index = 0;
        _userHasZoomedOrPanned = false;
        CrackIdText.Text = _crackId;
        LoadCurrentObservation(vm);
    }

    private void LoadCurrentObservation(ResultsCompareViewModel vm)
    {
        if (_observations.Count == 0)
        {
            SetHasImage(false);
            EmptyStateText.Text = "이 크랙에 연결된 원본 사진 정보가 없습니다 (재스티칭 필요)";
            EmptyStateText.Visibility = Visibility.Visible;
            TheImage.Source = null;
            CrackBboxOverlay.Visibility = Visibility.Collapsed;
            PositionText.Text = "";
            return;
        }

        SetHasImage(true);
        EmptyStateText.Visibility = Visibility.Collapsed;

        // 사진이 1장뿐이면 이전/다음/위치 표시 자체가 의미 없다(넘어갈 곳이 없음) --
        // 숨겨서 "1 / 1"처럼 아무 의미 없는 숫자만 떠 있지 않게 한다.
        var showNav = _observations.Count > 1;
        var navVisibility = showNav ? Visibility.Visible : Visibility.Collapsed;
        PrevButton.Visibility = navVisibility;
        NextButton.Visibility = navVisibility;
        PositionText.Visibility = navVisibility;
        if (showNav)
        {
            PrevButton.IsEnabled = _index > 0;
            NextButton.IsEnabled = _index < _observations.Count - 1;
            PositionText.Text = $"{_index + 1} / {_observations.Count}";
        }

        var obs = _observations[_index];
        var path = vm.ResolveSourceImagePath(obs.ImageId);
        FileNameText.Text = path != null ? System.IO.Path.GetFileName(path) : $"{obs.ImageId} (파일 경로 없음)";
        _currentBboxPxInSource = obs.BboxPxInSource;

        if (path == null || !File.Exists(path))
        {
            TheImage.Source = null;
            CrackBboxOverlay.Visibility = Visibility.Collapsed;
            return;
        }

        var bitmap = new BitmapImage();
        bitmap.BeginInit();
        bitmap.CacheOption = BitmapCacheOption.OnLoad;
        bitmap.UriSource = new Uri(path);
        bitmap.EndInit();
        bitmap.Freeze();
        TheImage.Source = bitmap;
        _pixelWidth = bitmap.PixelWidth;
        _pixelHeight = bitmap.PixelHeight;

        if (Viewport.ActualWidth > 0 && Viewport.ActualHeight > 0)
            CenterOnCrack();
    }

    /// <summary>Frames the crack's bbox with margin (not just its exact bbox --
    /// a bare bbox-fit zoom leaves no surrounding context to tell what part of
    /// the wall this actually is) and centers it in the viewport. This is the
    /// "위치가 중심에 와야 함" requirement -- unlike ImageViewerWindow's
    /// FitToWindow (whole image), this always frames just the crack area
    /// regardless of the source photo's full resolution.</summary>
    private void CenterOnCrack()
    {
        if (_currentBboxPxInSource is not { Length: 4 } bbox)
        {
            FitToWindow();
            return;
        }

        double x0 = bbox[0], y0 = bbox[1], x1 = bbox[2], y1 = bbox[3];
        double bw = Math.Max(1, x1 - x0);
        double bh = Math.Max(1, y1 - y0);
        double cx = (x0 + x1) / 2.0;
        double cy = (y0 + y1) / 2.0;

        // Show ~4x the crack's own bbox size as surrounding context.
        const double paddingFactor = 4.0;
        double targetW = bw * paddingFactor;
        double targetH = bh * paddingFactor;

        _scale = Math.Min(Viewport.ActualWidth / targetW, Viewport.ActualHeight / targetH);
        _scale = Math.Clamp(_scale, 0.05, 8.0);

        double left = (Viewport.ActualWidth / 2.0) - (cx * _scale);
        double top = (Viewport.ActualHeight / 2.0) - (cy * _scale);
        ApplyTransform(left, top);
    }

    private void FitToWindow()
    {
        if (_pixelWidth == 0 || _pixelHeight == 0)
            return;
        _scale = Math.Min(Viewport.ActualWidth / _pixelWidth, Viewport.ActualHeight / _pixelHeight);
        _scale = Math.Min(_scale, 1.0);
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

        if (_currentBboxPxInSource is { Length: 4 } bbox)
        {
            double x0 = bbox[0], y0 = bbox[1], x1 = bbox[2], y1 = bbox[3];
            CrackBboxOverlay.Width = Math.Max(1, (x1 - x0) * _scale);
            CrackBboxOverlay.Height = Math.Max(1, (y1 - y0) * _scale);
            Canvas.SetLeft(CrackBboxOverlay, left + x0 * _scale);
            Canvas.SetTop(CrackBboxOverlay, top + y0 * _scale);
            CrackBboxOverlay.Visibility = Visibility.Visible;
        }
        else
        {
            CrackBboxOverlay.Visibility = Visibility.Collapsed;
        }
    }

    private void Viewport_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (_userHasZoomedOrPanned || _pixelWidth == 0 || Viewport.ActualWidth == 0 || Viewport.ActualHeight == 0)
            return;
        CenterOnCrack();
    }

    private void PrevButton_Click(object sender, RoutedEventArgs e)
    {
        if (_index <= 0 || DataContext is not ResultsCompareViewModel vm)
            return;
        _index--;
        _userHasZoomedOrPanned = false;
        LoadCurrentObservation(vm);
    }

    private void NextButton_Click(object sender, RoutedEventArgs e)
    {
        if (_index >= _observations.Count - 1 || DataContext is not ResultsCompareViewModel vm)
            return;
        _index++;
        _userHasZoomedOrPanned = false;
        LoadCurrentObservation(vm);
    }

    /// <summary>"원본 크기로 복귀" -- re-centers/re-frames on the crack the way
    /// it looked when first selected, undoing whatever zoom/pan the operator
    /// did while inspecting (사용자 요청: "줌 후 원본 복귀 버튼").</summary>
    private void RecenterButton_Click(object sender, RoutedEventArgs e)
    {
        _userHasZoomedOrPanned = false;
        CenterOnCrack();
    }

    private bool _isDragging;
    private Point _dragStart;
    private double _panStartLeft, _panStartTop;

    private void Canvas_MouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (_pixelWidth == 0)
            return;
        _userHasZoomedOrPanned = true;
        var cursor = e.GetPosition(Viewport);
        double currentLeft = Canvas.GetLeft(TheImage);
        double currentTop = Canvas.GetTop(TheImage);

        double factor = e.Delta > 0 ? 1.15 : 1 / 1.15;
        double newScale = Math.Clamp(_scale * factor, 0.02, 20.0);

        var contentPoint = new Point((cursor.X - currentLeft) / _scale, (cursor.Y - currentTop) / _scale);
        double newLeft = cursor.X - contentPoint.X * newScale;
        double newTop = cursor.Y - contentPoint.Y * newScale;

        _scale = newScale;
        ApplyTransform(newLeft, newTop);
        e.Handled = true;
    }

    private void Canvas_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (_pixelWidth == 0)
            return;
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

        if (_currentBboxPxInSource is { Length: 4 } bbox)
        {
            Canvas.SetLeft(CrackBboxOverlay, left + bbox[0] * _scale);
            Canvas.SetTop(CrackBboxOverlay, top + bbox[1] * _scale);
        }
    }

    private void Canvas_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        _isDragging = false;
        Viewport.ReleaseMouseCapture();
    }
}
