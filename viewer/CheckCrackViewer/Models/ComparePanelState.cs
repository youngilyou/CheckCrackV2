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
    // 스티칭 패널 클릭 -> 원본 사진 점프(JumpToOriginalImageAt) 시, 클릭한 지점이 이 원본
    // 사진의 어디에 해당하는지(OriginalDisplayWidth/Height 기준 표시-픽셀 좌표, ZoomFactor=1.0
    // 기준 -- 뷰가 실제 스크롤할 땐 여기에 ZoomFactor를 곱한다). null이면 "센터링할 지점 없음"
    // (예: prev/next로 넘긴 경우) -- View(ResultsCompareView.xaml.cs)가 이 값을 소비해
    // ScrollViewer를 그 지점이 뷰포트 중앙에 오도록 스크롤한 뒤 반드시 null로 되돌려서, 다음
    // prev/next 넘김에서 엉뚱하게 재사용되지 않게 한다.
    [ObservableProperty] private double? _pendingCenterDisplayX;
    [ObservableProperty] private double? _pendingCenterDisplayY;

    // 현재 표시 중인 원본 사진의 실제(전체 해상도) 픽셀 크기 -- OriginalDisplayWidth/Height는
    // LoadScaledBitmap이 MaxDisplayDim으로 축소한 표시용 크기라, CrackMarkerDisplayX/Y처럼
    // source-pixel 좌표(bbox_px_in_source)를 표시 좌표로 환산하려면 둘 다 필요하다
    // (StitchOrigWidth/Height와 동일한 이유, ResultsCompareViewModel.LoadOriginalImageAt 참고).
    [ObservableProperty] private int _originalOrigWidth;
    [ObservableProperty] private int _originalOrigHeight;

    // 2026-09-13: 균열 검토 모드에서 크랙을 선택했을 때(SelectedReviewItem), 그 크랙이 찍힌
    // 원본 사진 위 위치를 이 패널(항상 Panel1, "왼쪽 원본")에 원 마커로 표시하기 위한 좌표 --
    // OriginalDisplayWidth/Height 기준(ZoomFactor=1.0 기준) 표시-픽셀 좌표. null이면 "표시할
    // 마커 없음"(크랙 미선택, source_observations 없음, 해당 사진을 못 찾음 등).
    // ResultsCompareViewModel.ShowCrackMarkerInPanel1이 채우고, PreviousOriginal/NextOriginal/
    // ReloadPanel이 더 이상 유효하지 않게 될 때 null로 되돌린다.
    [ObservableProperty] private double? _crackMarkerDisplayX;
    [ObservableProperty] private double? _crackMarkerDisplayY;

    public bool HasCrackMarker => CrackMarkerDisplayX.HasValue && CrackMarkerDisplayY.HasValue;
    partial void OnCrackMarkerDisplayXChanged(double? value) => OnPropertyChanged(nameof(HasCrackMarker));
    partial void OnCrackMarkerDisplayYChanged(double? value) => OnPropertyChanged(nameof(HasCrackMarker));

    public bool HasOriginalImageList => OriginalImageList.Count > 0;
    public string OriginalImageLabel => HasOriginalImageList ? $"{OriginalImageIndex + 1} / {OriginalImageList.Count}" : "";

    // --- 스티칭: 마우스 줌/이동 + 더블클릭 전체화면(StitchImagePath를 ImageViewerWindow에 전달) ---
    [ObservableProperty] private BitmapImage? _stitchDisplayBitmap;
    [ObservableProperty] private double _stitchDisplayWidth;
    [ObservableProperty] private double _stitchDisplayHeight;
    [ObservableProperty] private string _stitchImagePath = "";
    // 스티칭 이미지의 실제(원본) 픽셀 크기 -- StitchDisplayWidth/Height는 MaxDisplayDim로
    // 축소된 화면 표시용 크기라, 클릭 좌표를 실제 모자이크 픽셀(=seam owner map 좌표계)로
    // 환산하려면 둘 다 필요하다 (ResultsCompareView.StitchImage_MouseLeftButtonDown 참고).
    [ObservableProperty] private int _stitchOrigWidth;
    [ObservableProperty] private int _stitchOrigHeight;
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
