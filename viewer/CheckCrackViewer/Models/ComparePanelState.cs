using System.Collections.Generic;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.Models;

/// <summary>One of 결과보기's two independent comparison panels. Each panel can show
/// one of up to three sub-views (원본/스티칭/보고서) -- Mode picks which, and only
/// that sub-view's fields are populated at any time (ResultsCompareViewModel.ReloadPanel
/// clears the others). Kept as one class with three sets of fields rather than three
/// separate panel types since a panel's mode changes at runtime and the view binds to
/// a single DataContext per panel.</summary>
public partial class ComparePanelState : ObservableObject
{
    [ObservableProperty] private string _mode = "원본";

    // --- 원본: prev/next 사진 넘기기 + 마우스 줌/이동 (ZoomFactor 공용, 아래 참고) ---
    [ObservableProperty] private List<string> _originalImageList = new();
    [ObservableProperty] private int _originalImageIndex = -1;
    [ObservableProperty] private BitmapImage? _originalDisplayBitmap;
    [ObservableProperty] private double _originalDisplayWidth;
    [ObservableProperty] private double _originalDisplayHeight;

    public bool HasOriginalImageList => OriginalImageList.Count > 0;
    public string OriginalImageLabel => HasOriginalImageList ? $"{OriginalImageIndex + 1} / {OriginalImageList.Count}" : "";

    // --- 스티칭: 마우스 줌/이동 + 더블클릭 전체화면(StitchImagePath를 ImageViewerWindow에 전달) ---
    [ObservableProperty] private BitmapImage? _stitchDisplayBitmap;
    [ObservableProperty] private double _stitchDisplayWidth;
    [ObservableProperty] private double _stitchDisplayHeight;
    [ObservableProperty] private string _stitchImagePath = "";
    // 원본/스티칭 서브뷰는 한 패널 안에서 배타적으로만 보이므로(Mode가 둘 중 하나),
    // 줌 배율 하나를 공유해도 안전 -- 모드 전환/파사드 전환마다 ReloadPanel에서 1.0으로 리셋.
    [ObservableProperty] private double _zoomFactor = 1.0;
    // facade마다 COLMAP 폴백이 걸렸을 때만 정합(rectified) 버전이 있고, 아니면 일반
    // Homography 체인 스티칭 결과를 그대로 보여준다 -- 어느 쪽인지 화면에 표시해야
    // "스티칭 + CM" 라벨이 실제로 CM 미적용 facade에서도 뜨는 오해를 막는다.
    [ObservableProperty] private bool _stitchIsColmapRectified;

    // --- 보고서: PDF 페이지 넘기기 + 마우스 줌/이동. ReportDisplayWidth/Height는 원본/스티칭의
    // 고정 MaxDisplayDim 기반 크기와 달리, 실제 패널 뷰포트 크기에 맞춰 계산한 "화면에 맞춤"
    // 기준 크기(ZoomFactor=1일 때의 크기) -- View 쪽 FitReportPage가 계산해서 채운다.
    [ObservableProperty] private BitmapSource? _reportPageBitmap;
    [ObservableProperty] private int _reportPageIndex;
    [ObservableProperty] private int _reportPageCount;
    [ObservableProperty] private double _reportDisplayWidth;
    [ObservableProperty] private double _reportDisplayHeight;

    public string ReportPageLabel => ReportPageCount > 0 ? $"{ReportPageIndex + 1} / {ReportPageCount}" : "";

    partial void OnOriginalImageIndexChanged(int value) => OnPropertyChanged(nameof(OriginalImageLabel));
    partial void OnOriginalImageListChanged(List<string> value)
    {
        OnPropertyChanged(nameof(HasOriginalImageList));
        OnPropertyChanged(nameof(OriginalImageLabel));
    }
    partial void OnReportPageIndexChanged(int value) => OnPropertyChanged(nameof(ReportPageLabel));
    partial void OnReportPageCountChanged(int value) => OnPropertyChanged(nameof(ReportPageLabel));
}
