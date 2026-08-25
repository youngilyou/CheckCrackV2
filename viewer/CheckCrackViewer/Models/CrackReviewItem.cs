using System.Collections.Generic;
using System.Linq;
using System.Windows.Media;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.Models;

/// <summary>Pending = AI detected it, nobody has looked at it yet (default).
/// Confirmed/Rejected = a reviewer explicitly kept/excluded it. Manual = a
/// reviewer drew this one themselves; it was never an AI detection.</summary>
public enum CrackReviewStatus { Pending, Confirmed, Rejected, Manual }

/// <summary>One row in the review list + one overlay polygon on the mosaic
/// canvas -- the same dual role CrackBoxItem plays for the AI-training
/// annotation screen, extended with a review Status instead of always
/// being freshly drawn. CanvasPoints are display-space (already scaled for
/// whatever bitmap is currently shown); PolygonPx stays in the facade
/// mosaic's own pixel space so it round-trips to the review JSON and to
/// Python's measure_polygon unchanged.</summary>
public partial class CrackReviewItem : ObservableObject
{
    public string CrackId { get; init; } = "";
    // null for a just-drawn manual addition -- its real length/width only
    // exists after Python's measure_polygon runs (SaveReview -> next report
    // generation), never guessed client-side.
    public double? LengthPx { get; init; }
    public double? MaxWidthPx { get; init; }
    public double? AreaPx { get; init; }
    public double Confidence { get; init; }
    public string? Severity { get; init; }

    public double[][] PolygonPx { get; init; } = [];
    public double[]? BboxPx { get; init; }

    /// <summary>Which specific original photo(s) this crack is actually visible
    /// in, and where -- empty for manual additions (no AI provenance) or for
    /// AI detections from a facade stitched before this existed. Consumed by
    /// OriginalCrackViewerWindow (원본 보기) via
    /// ResultsCompareViewModel.SelectedReviewItem.</summary>
    public List<SourceObservationModel> SourceObservations { get; init; } = new();

    /// <summary>Sequential 1..N shown on the small canvas badge (matches the
    /// PDF report's "전체 위치도" numbering convention) -- replaces showing the
    /// full crack_id text on-canvas, which at high zoom (LayoutTransform scales
    /// the label along with the image) grew into a giant opaque box that
    /// covered the exact area a reviewer was trying to inspect. Observable
    /// because removing a manual addition mid-list re-numbers everything
    /// after it -- a plain property wouldn't refresh already-rendered badges.</summary>
    [ObservableProperty] private int _displayNumber;

    // LabelText depends on Status (뒤_C000000 vs "뒤_C000000 (제외됨)") -- without
    // this, rejecting a crack left its on-canvas tag showing the bare id with
    // no visible sign it had been reviewed at all, which is exactly the "can
    // I still tell this was a crack candidate" gap the user flagged.
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(LabelText))]
    private CrackReviewStatus _status;

    public PointCollection CanvasPoints { get; set; } = new();

    /// <summary>Clicking a crack's overlay toggles this -- when false, the
    /// polygon renders fully transparent (fill AND stroke, not just faded)
    /// so the raw mosaic pixels underneath are completely unobstructed. The
    /// user reported that even a semi-transparent highlight made a thin
    /// crack impossible to actually judge ("이 부분이 크랙인지 알 수 없다"),
    /// so "reduced opacity" wasn't enough -- this needs a real on/off. The
    /// label tag stays visible regardless, as the click target to toggle
    /// back on.</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(LabelText))]
    private bool _isHighlightVisible = true;

    public double CanvasLabelX => CanvasPoints.Count == 0 ? 0 : CanvasPoints.Min(p => p.X);
    public double CanvasLabelY => CanvasPoints.Count == 0 ? 0 : CanvasPoints.Min(p => p.Y);

    public string ConfidenceText => Status == CrackReviewStatus.Manual ? "관리자 확인" : $"{Confidence:P0}";
    public string LengthText => LengthPx.HasValue ? $"{LengthPx:F1} px" : "저장 후 계산";
    public string WidthText => MaxWidthPx.HasValue ? $"{MaxWidthPx:F2} px" : "저장 후 계산";

    /// <summary>What the on-canvas tag shows -- always includes the crack id
    /// (never just a generic "제외됨" with no way to trace which crack it was)
    /// plus a status suffix so a rejected/added mark stays self-explanatory
    /// without opening the list.</summary>
    public string LabelText
    {
        get
        {
            var text = Status switch
            {
                CrackReviewStatus.Rejected => $"{CrackId} · 제외됨",
                CrackReviewStatus.Manual => $"{CrackId} · 관리자 추가",
                _ => CrackId,
            };
            return IsHighlightVisible ? text : $"{text} · 숨김(클릭)";
        }
    }
}
