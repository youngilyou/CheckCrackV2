using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;
using CheckCrackViewer.Models;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

/// <summary>더블클릭 시 스티칭 이미지를 ImageViewerWindow로 전체화면 오픈 --
/// MainWindow.xaml.cs의 동일 패턴(단일 뷰어 인스턴스 유지) 재사용. 우클릭 드래그 이동은
/// AiTrainingView.xaml.cs의 ImageScrollViewer_* 핸들러와 동일 패턴이지만, 휠은 (Ctrl 없이)
/// 항상 줌으로 쓴다 -- 이동을 우클릭 드래그가 전담하므로 휠의 기본 스크롤 동작은 필요 없음.
/// 원본/스티칭 두 패널이 같은 DataTemplate을 재사용하므로 x:Name 대신 sender 기반으로
/// 어느 ScrollViewer/패널인지 매번 식별한다.</summary>
public partial class ResultsCompareView : UserControl
{
    private ImageViewerWindow? _currentViewer;
    private OriginalCrackViewerWindow? _originalCrackViewer;

    private bool _isPanning;
    private Point _panMouseStart;
    private double _panOffsetStartH;
    private double _panOffsetStartV;
    private ScrollViewer? _panningScrollViewer;

    public ResultsCompareView()
    {
        InitializeComponent();
        DataContextChanged += ResultsCompareView_DataContextChanged;
    }

    // 원본 보기 창이 열려 있는 동안, 번호 배지 클릭이나 목록 선택으로 SelectedReviewItem이
    // 바뀔 때마다 그 창을 갱신한다 -- ImageViewerWindow가 FacadeItemViewModel.PropertyChanged를
    // 구독해 실시간 미리보기를 갱신하는 것과 같은 패턴. UserControl이라 DataContext가 생성자
    // 이후에 외부에서 설정되므로 DataContextChanged로 구독 대상을 갈아 끼운다.
    private void ResultsCompareView_DataContextChanged(object sender, DependencyPropertyChangedEventArgs e)
    {
        if (e.OldValue is ResultsCompareViewModel oldVm)
            oldVm.PropertyChanged -= ViewModel_PropertyChanged;
        if (e.NewValue is ResultsCompareViewModel newVm)
            newVm.PropertyChanged += ViewModel_PropertyChanged;
    }

    private void ViewModel_PropertyChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName != nameof(ResultsCompareViewModel.SelectedReviewItem))
            return;
        if (_originalCrackViewer == null || sender is not ResultsCompareViewModel vm)
            return;
        if (vm.SelectedReviewItem != null)
            _originalCrackViewer.ShowCrack(vm.SelectedReviewItem, vm);
    }

    private void OpenOriginalViewerButton_Click(object sender, RoutedEventArgs e)
    {
        if (DataContext is not ResultsCompareViewModel vm)
            return;

        if (_originalCrackViewer == null)
        {
            _originalCrackViewer = new OriginalCrackViewerWindow
            {
                Owner = Window.GetWindow(this),
                DataContext = vm,
            };
            _originalCrackViewer.Closed += (_, _) => _originalCrackViewer = null;
            _originalCrackViewer.Show();
        }
        else
        {
            _originalCrackViewer.Activate();
        }

        if (vm.SelectedReviewItem != null)
            _originalCrackViewer.ShowCrack(vm.SelectedReviewItem, vm);
    }

    private void StitchImage_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2)
            return;
        if (sender is not FrameworkElement { DataContext: ComparePanelState panel } || string.IsNullOrEmpty(panel.StitchImagePath))
            return;

        _currentViewer?.Close();
        _currentViewer = new ImageViewerWindow(panel.StitchImagePath) { Owner = Window.GetWindow(this) };
        _currentViewer.Closed += (_, _) => _currentViewer = null;
        _currentViewer.Show();
    }

    private void PanZoomScrollViewer_PreviewMouseRightButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (sender is not ScrollViewer sv)
            return;
        _isPanning = true;
        _panningScrollViewer = sv;
        _panMouseStart = e.GetPosition(sv);
        _panOffsetStartH = sv.HorizontalOffset;
        _panOffsetStartV = sv.VerticalOffset;
        sv.CaptureMouse();
        sv.Cursor = Cursors.ScrollAll;
        e.Handled = true; // 기본 우클릭 컨텍스트 메뉴도 같이 막음
    }

    private void PanZoomScrollViewer_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_isPanning || _panningScrollViewer == null)
            return;
        var pos = e.GetPosition(_panningScrollViewer);
        _panningScrollViewer.ScrollToHorizontalOffset(_panOffsetStartH - (pos.X - _panMouseStart.X));
        _panningScrollViewer.ScrollToVerticalOffset(_panOffsetStartV - (pos.Y - _panMouseStart.Y));
    }

    private void PanZoomScrollViewer_PreviewMouseRightButtonUp(object sender, MouseButtonEventArgs e)
    {
        EndPan();
        e.Handled = true;
    }

    // LostMouseCapture(not MouseLeave)를 쓰는 이유는 AiTrainingView와 동일: 캡처된 상태면
    // 커서가 ScrollViewer 밖으로 나가도 드래그가 계속 이어져야 하고, 실제 캡처 상실(다른
    // 창이 포커스를 뺏는 경우 등)때만 드래그를 끝내야 한다.
    private void PanZoomScrollViewer_LostMouseCapture(object sender, MouseEventArgs e) => EndPan();

    private void EndPan()
    {
        if (!_isPanning)
            return;
        _isPanning = false;
        _panningScrollViewer?.ReleaseMouseCapture();
        if (_panningScrollViewer != null)
            _panningScrollViewer.Cursor = Cursors.Arrow;
        _panningScrollViewer = null;
    }

    private void PanZoomScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (sender is not FrameworkElement { DataContext: ComparePanelState panel })
            return;
        if (DataContext is not ResultsCompareViewModel vm)
            return;

        if (e.Delta > 0)
            vm.ZoomInPanelCommand.Execute(panel);
        else
            vm.ZoomOutPanelCommand.Execute(panel);
        e.Handled = true; // 스크롤 대신 항상 줌 -- 이동은 우클릭 드래그가 전담
    }

    /// <summary>"초기화" 버튼의 Command(ResetZoomPanelCommand)는 ZoomFactor만 1.0으로
    /// 되돌리고 ScrollViewer의 스크롤 위치는 건드리지 않는다 -- 확대 상태에서 이동해둔 뒤
    /// 초기화를 누르면 줌만 풀리고 화면 밖으로 스크롤된 채 남아있던 문제. 같은 버튼의 Click
    /// 이벤트에서 스크롤을 뷰포트 중앙으로 맞춘다. ZoomFactor 변경 → LayoutTransform 재측정이
    /// 끝난 뒤에 ScrollableWidth/Height가 최신값이 되므로, Loaded 우선순위로 한 프레임 미룬다.</summary>
    private void ResetZoomButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { Parent: FrameworkElement buttonRow })
            return;
        if (buttonRow.Parent is not DockPanel dock)
            return;
        var scrollViewer = LogicalTreeHelper.GetChildren(dock).OfType<ScrollViewer>().FirstOrDefault();
        if (scrollViewer == null)
            return;

        scrollViewer.Dispatcher.BeginInvoke(new System.Action(() =>
        {
            scrollViewer.ScrollToHorizontalOffset(scrollViewer.ScrollableWidth / 2);
            scrollViewer.ScrollToVerticalOffset(scrollViewer.ScrollableHeight / 2);
        }), DispatcherPriority.Loaded);
    }

    /// <summary>"전체 보기" -- 원본/스티칭/균열 검토 공통. 보고서 패널은 이미 항상
    /// "화면에 맞춤"이 기본값이라(FitReportPage) 대상이 아니다. 같은 DockPanel 안의
    /// ScrollViewer를 ResetZoomButton_Click과 동일한 방식으로 찾은 뒤, 그 뷰포트
    /// 크기에 원본 이미지가 통째로 들어오는 배율을 계산해 ZoomFactor에 바로 대입한다
    /// -- 100%보다 확대는 하지 않음(이미 화면보다 작은 이미지를 억지로 키우지 않기
    /// 위함), ZoomOutPanel/ZoomOutReview의 하한을 0.05로 낮춰둔 것과 짝을 이룬다
    /// (그렇지 않으면 전체 보기로 100% 밑까지 줄인 뒤 "－"를 눌렀을 때 오히려
    /// 확대되는 것처럼 보인다).</summary>
    private void FitZoomButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement { Parent: FrameworkElement buttonRow })
            return;
        if (buttonRow.Parent is not DockPanel dock)
            return;
        var scrollViewer = LogicalTreeHelper.GetChildren(dock).OfType<ScrollViewer>().FirstOrDefault();
        if (scrollViewer == null)
            return;

        scrollViewer.UpdateLayout();
        if (scrollViewer.ActualWidth <= 0 || scrollViewer.ActualHeight <= 0)
            return;

        if (dock.DataContext is ComparePanelState panel)
        {
            var (w, h) = panel.Mode == "스티칭"
                ? (panel.StitchDisplayWidth, panel.StitchDisplayHeight)
                : (panel.OriginalDisplayWidth, panel.OriginalDisplayHeight);
            var scale = ComputeFitScale(scrollViewer.ActualWidth, scrollViewer.ActualHeight, w, h);
            if (scale.HasValue)
                panel.ZoomFactor = scale.Value;
        }
        else if (DataContext is ResultsCompareViewModel vm)
        {
            var scale = ComputeFitScale(scrollViewer.ActualWidth, scrollViewer.ActualHeight, vm.ReviewDisplayWidth, vm.ReviewDisplayHeight);
            if (scale.HasValue)
                vm.ReviewZoomFactor = scale.Value;
        }
    }

    private static double? ComputeFitScale(double viewportWidth, double viewportHeight, double nativeWidth, double nativeHeight)
    {
        if (nativeWidth <= 0 || nativeHeight <= 0)
            return null;
        var scale = System.Math.Min(viewportWidth / nativeWidth, viewportHeight / nativeHeight);
        if (scale <= 0 || double.IsNaN(scale) || double.IsInfinity(scale))
            return null;
        return System.Math.Round(System.Math.Min(1.0, scale) * 100) / 100;
    }

    private void ReportScrollViewer_Loaded(object sender, RoutedEventArgs e)
    {
        if (sender is ScrollViewer sv)
            FitReportPage(sv);
    }

    private void ReportScrollViewer_SizeChanged(object sender, SizeChangedEventArgs e)
    {
        if (sender is ScrollViewer sv)
            FitReportPage(sv);
    }

    /// <summary>보고서 패널은 원본/스티칭과 달리 "기본값 = 화면(패널 뷰포트)에 맞춤"이어야
    /// 한다는 요청 -- 고정 MaxDisplayDim이 아니라 ScrollViewer의 실제 ActualWidth/Height를
    /// 기준으로 PDF 페이지 종횡비를 유지하며 맞춘 크기를 ReportDisplayWidth/Height에 채운다.
    /// ZoomFactor(공용, 페이지 전환/패널 리로드 시 1.0으로 리셋됨)가 이 기준 크기에 곱해져
    /// 실제 표시 크기가 되므로, 확대하면 이 "맞춤" 크기보다 커지면서 스크롤이 생긴다.</summary>
    private static void FitReportPage(ScrollViewer sv)
    {
        if (sv.DataContext is not ComparePanelState panel || panel.ReportPageBitmap == null)
            return;
        if (sv.ActualWidth <= 0 || sv.ActualHeight <= 0)
            return;

        var bitmap = panel.ReportPageBitmap;
        var scale = System.Math.Min(sv.ActualWidth / bitmap.PixelWidth, sv.ActualHeight / bitmap.PixelHeight);
        if (scale <= 0 || double.IsInfinity(scale) || double.IsNaN(scale))
            return;

        panel.ReportDisplayWidth = bitmap.PixelWidth * scale;
        panel.ReportDisplayHeight = bitmap.PixelHeight * scale;
    }

    // ================= 균열 검토 캔버스 =================
    // 줌: 검토 캔버스는 ComparePanelState가 아니라 VM에 직접 달린 ReviewZoomFactor를
    // 쓰므로, panel 기반 PanZoomScrollViewer_PreviewMouseWheel을 그대로 못 쓰고 따로 둔다
    // (우클릭 드래그 이동 핸들러들은 ComparePanelState에 의존하지 않아 그대로 재사용 가능).
    // 드로잉 제스처는 AiTrainingView.xaml.cs의 DrawCanvas_Mouse* 3종과 완전히 동일한 패턴.

    private void ReviewScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if (DataContext is not ResultsCompareViewModel vm)
            return;
        if (e.Delta > 0)
            vm.ZoomInReviewCommand.Execute(null);
        else
            vm.ZoomOutReviewCommand.Execute(null);
        e.Handled = true;
    }

    private Polyline? _reviewDragLine;
    private readonly List<Point> _reviewDragPoints = new();
    private bool _reviewIsToggleClick;

    private void ReviewDrawCanvas_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (sender is not Canvas canvas)
            return;
        var start = e.GetPosition(canvas);

        // 기존 크랙 위를 클릭한 거면 새로 그리기 시작하지 않고 그 크랙의
        // 표시/숨김만 토글한다 (사용자 요청: 클릭으로 오버레이 껐다 켰다 하면서
        // 원본 픽셀을 직접 확인). 빈 곳을 클릭했을 때만 아래 드래그-그리기로 이어진다.
        if (DataContext is ResultsCompareViewModel vm && vm.TryToggleHighlightAt(start))
        {
            _reviewIsToggleClick = true;
            return;
        }
        _reviewIsToggleClick = false;

        _reviewDragPoints.Clear();
        _reviewDragPoints.Add(start);
        _reviewDragLine = new Polyline
        {
            Stroke = (Brush)Application.Current.Resources["Accent"],
            StrokeThickness = 2,
            StrokeDashArray = { 4, 3 },
        };
        _reviewDragLine.Points.Add(start);
        canvas.Children.Add(_reviewDragLine);
        canvas.CaptureMouse();
    }

    private void ReviewDrawCanvas_MouseMove(object sender, MouseEventArgs e)
    {
        if (_reviewIsToggleClick || _reviewDragLine == null || sender is not Canvas canvas)
            return;
        var pos = e.GetPosition(canvas);
        if (_reviewDragPoints.Count > 0)
        {
            var last = _reviewDragPoints[^1];
            var dx = pos.X - last.X;
            var dy = pos.Y - last.Y;
            if ((dx * dx) + (dy * dy) < 16)
                return;
        }
        _reviewDragPoints.Add(pos);
        _reviewDragLine.Points.Add(pos);
    }

    private void ReviewDrawCanvas_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (_reviewDragLine == null || sender is not Canvas canvas)
            return;
        canvas.ReleaseMouseCapture();
        var end = e.GetPosition(canvas);
        _reviewDragPoints.Add(end);
        canvas.Children.Remove(_reviewDragLine);
        _reviewDragLine = null;

        if (DataContext is ResultsCompareViewModel vm)
            vm.AddManualCrack(_reviewDragPoints);
        _reviewDragPoints.Clear();
    }
}
