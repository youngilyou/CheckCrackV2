using CommunityToolkit.Mvvm.ComponentModel;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Media;

namespace CheckCrackViewer.Models;

public sealed record CrackPolygonPoint(int X, int Y);

/// <summary>One user-drawn crack region. CanvasPoints are display-space pixels for
/// rendering; OrigPoints are the source image's own pixel coordinates and are
/// exported directly as YOLO-seg polygons.</summary>
public partial class CrackBoxItem : ObservableObject
{
    [ObservableProperty] private int _index;

    public PointCollection CanvasPoints { get; init; } = new();
    public IReadOnlyList<CrackPolygonPoint> OrigPoints { get; init; } = [];
    public double OrigAreaPx { get; init; }
    public double OrigPerimeterPx { get; init; }

    public double CanvasLabelX => CanvasPoints.Count == 0 ? 0 : CanvasPoints.Min(p => p.X);
    public double CanvasLabelY => CanvasPoints.Count == 0 ? 0 : CanvasPoints.Min(p => p.Y);

    public int OrigX0 => OrigPoints.Count == 0 ? 0 : OrigPoints.Min(p => p.X);
    public int OrigY0 => OrigPoints.Count == 0 ? 0 : OrigPoints.Min(p => p.Y);
    public int OrigX1 => OrigPoints.Count == 0 ? 0 : OrigPoints.Max(p => p.X);
    public int OrigY1 => OrigPoints.Count == 0 ? 0 : OrigPoints.Max(p => p.Y);
    public int OrigWidthPx => Math.Max(0, OrigX1 - OrigX0 + 1);
    public int OrigHeightPx => Math.Max(0, OrigY1 - OrigY0 + 1);

    public string CoordsLabel => $"{OrigPoints.Count}점 · {OrigX0},{OrigY0} → {OrigX1},{OrigY1}";
}
