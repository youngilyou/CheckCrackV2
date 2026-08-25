using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.ViewModels;

/// <summary>"작업 현황" 대시보드의 12개 카테고리, 요구된 표시 순서 그대로.
/// PlannedCapture/Capturing/CaptureDone/CoverageRetakeNeeded/DetailCaptureNeeded는
/// 이 Viewer에 실제 신호가 없다 — 촬영 자체는 별도 FacadePreviewer 프로그램의 영역이고,
/// 재촬영/정밀촬영 필요 판정은 Coverage/신뢰도 threshold 정책이 아직 없다. 절대
/// 추측값을 넣지 않고 항상 0으로 표시한다(CLAUDE.local.md: 지어내지 않는다).</summary>
public enum DashboardStatusCategory
{
    PlannedCapture,       // 촬영예정 -- 항상 0 (외부 FacadePreviewer 영역, 신호 없음)
    Capturing,            // 촬영중 -- 항상 0
    CaptureDone,          // 촬영완료 -- 항상 0

    CoverageRetakeNeeded, // 누락 재촬영 필요 -- 항상 0 (threshold 정책 미확정)
    StitchQueued,         // Stitching 대기
    Stitching,            // Stitching 중
    AiAnalyzing,          // AI 분석중
    DetailCaptureNeeded,  // 정밀촬영 필요 -- 항상 0 (threshold 정책 미확정)
    NeedsManualReview,    // 사용자 검토 필요
    GeneratingReport,     // 보고서 생성중
    Done,                 // 완료
    Failed,               // 실패
}

/// <summary>"작업 현황" 대시보드의 카드 하나. 12개 전부 MainViewModel 생성 시 한 번만
/// 만들어지고(순서 고정) 이후 재계산 때는 Count만 갱신한다 — FacadeTree가 폴링마다
/// Clear()+재구성하지 않는 것과 같은 이유로, 불필요한 매 틱 재할당/재바인딩을 피한다.</summary>
public partial class DashboardStatusCount : ObservableObject
{
    public DashboardStatusCategory Category { get; init; }
    public string Label { get; init; } = "";
    [ObservableProperty] private int _count;
}
