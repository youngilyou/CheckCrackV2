using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Collections.Generic;
using System.Windows.Shapes;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

/// <summary>Handles the crack-region drawing gesture on DrawCanvas. Deliberately
/// code-behind, not MVVM, for the same reason MainWindow.xaml.cs's
/// Image_MouseLeftButtonDown/LivePreview_MouseLeftButtonDown are -- raw pointer
/// drag feedback is transient UI state, not application data; only the
/// finished polygon (via AiTrainingViewModel.AddPolygon) is real data.</summary>
public partial class AiTrainingView : UserControl
{
    private Polyline? _dragLine;
    private readonly List<Point> _dragPoints = new();

    private bool _isPanning;
    private Point _panMouseStart;
    private double _panOffsetStartH;
    private double _panOffsetStartV;

    public AiTrainingView()
    {
        InitializeComponent();
    }

    /// <summary>Right-drag pans the mosaic. Deliberately the ONLY navigation
    /// gesture on this canvas; Ctrl+wheel handles zoom separately so ordinary
    /// wheel scrolling keeps its expected behavior.</summary>
    private void ImageScrollViewer_PreviewMouseRightButtonDown(object sender, MouseButtonEventArgs e)
    {
        _isPanning = true;
        _panMouseStart = e.GetPosition(ImageScrollViewer);
        _panOffsetStartH = ImageScrollViewer.HorizontalOffset;
        _panOffsetStartV = ImageScrollViewer.VerticalOffset;
        ImageScrollViewer.CaptureMouse();
        ImageScrollViewer.Cursor = Cursors.ScrollAll;
        e.Handled = true; // also suppresses the default right-click context menu
    }

    private void ImageScrollViewer_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_isPanning)
            return;
        var pos = e.GetPosition(ImageScrollViewer);
        ImageScrollViewer.ScrollToHorizontalOffset(_panOffsetStartH - (pos.X - _panMouseStart.X));
        ImageScrollViewer.ScrollToVerticalOffset(_panOffsetStartV - (pos.Y - _panMouseStart.Y));
    }

    private void ImageScrollViewer_PreviewMouseRightButtonUp(object sender, MouseButtonEventArgs e)
    {
        EndPan();
        e.Handled = true;
    }

    // LostMouseCapture (not MouseLeave) -- with the mouse captured, drag moves
    // keep routing here even once the cursor visually leaves the ScrollViewer's
    // bounds, so panning shouldn't stop on that; only an actual capture loss
    // (e.g. another window stealing focus mid-drag) should end it early.
    private void ImageScrollViewer_LostMouseCapture(object sender, MouseEventArgs e) => EndPan();

    private void ImageScrollViewer_PreviewMouseWheel(object sender, MouseWheelEventArgs e)
    {
        if ((Keyboard.Modifiers & ModifierKeys.Control) != ModifierKeys.Control)
            return;
        if (DataContext is AiTrainingViewModel vm)
        {
            vm.ApplyZoomDelta(e.Delta);
            e.Handled = true;
        }
    }

    private void EndPan()
    {
        if (!_isPanning)
            return;
        _isPanning = false;
        ImageScrollViewer.ReleaseMouseCapture();
        ImageScrollViewer.Cursor = Cursors.Arrow;
    }

    private void DrawCanvas_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        var start = e.GetPosition(DrawCanvas);
        _dragPoints.Clear();
        _dragPoints.Add(start);
        _dragLine = new Polyline
        {
            Stroke = (Brush)Application.Current.Resources["Accent"],
            StrokeThickness = 2,
            StrokeDashArray = { 4, 3 },
        };
        _dragLine.Points.Add(start);
        DrawCanvas.Children.Add(_dragLine);
        DrawCanvas.CaptureMouse();
    }

    private void DrawCanvas_MouseMove(object sender, MouseEventArgs e)
    {
        if (_dragLine == null)
            return;
        var pos = e.GetPosition(DrawCanvas);
        if (_dragPoints.Count > 0)
        {
            var last = _dragPoints[^1];
            var dx = pos.X - last.X;
            var dy = pos.Y - last.Y;
            if ((dx * dx) + (dy * dy) < 16)
                return;
        }
        _dragPoints.Add(pos);
        _dragLine.Points.Add(pos);
    }

    private void DrawCanvas_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (_dragLine == null)
            return;
        DrawCanvas.ReleaseMouseCapture();
        var end = e.GetPosition(DrawCanvas);
        _dragPoints.Add(end);
        DrawCanvas.Children.Remove(_dragLine);
        _dragLine = null;

        if (DataContext is AiTrainingViewModel vm)
            vm.AddPolygon(_dragPoints);
        _dragPoints.Clear();
    }
}
