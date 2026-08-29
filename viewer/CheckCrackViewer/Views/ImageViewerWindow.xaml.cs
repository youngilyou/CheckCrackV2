using System;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Imaging;
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

    public ImageViewerWindow(string imagePath)
    {
        InitializeComponent();
        InitWindowBounds();
        LoadImage(imagePath);
        KeyDown += (_, e) => { if (e.Key == Key.Escape) Close(); };
    }

    /// <summary>"Live" mode: opened from the growing stitching preview while a
    /// facade is actively running. Keeps showing whatever LivePreviewImagePath
    /// points at as it's replaced by newer snapshots, then switches over once to
    /// the real final mosaic once the run finishes and one becomes available —
    /// matching CLAUDE.local.md's "don't show it until it's genuinely done"
    /// requirement for the *final* image, while still growing live in between.</summary>
    public ImageViewerWindow(FacadeItemViewModel facade)
    {
        InitializeComponent();
        InitWindowBounds();
        _liveFacade = facade;
        _liveFacade.PropertyChanged += Facade_PropertyChanged;
        Closed += (_, _) => _liveFacade.PropertyChanged -= Facade_PropertyChanged;

        if (facade.LivePreviewImagePath != null)
            LoadImage(facade.LivePreviewImagePath, facade.FacadeId + " (실시간 미리보기)");
        else
            Tag = facade.FacadeId + " (실시간 미리보기 대기 중)";

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
}
