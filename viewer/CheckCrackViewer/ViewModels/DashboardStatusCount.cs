using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.ViewModels;

/// <summary>"작업 현황" 대시보드의 카테고리, 요구된 표시 순서 그대로.
/// PlannedCapture/Capturing/CaptureDone은 이 Viewer에 실제 신호가 없다 -- 촬영 자체는
/// 별도 FacadePreviewer 프로그램의 영역이라 항상 0으로 표시하고(카드 자체는 주석
/// 처리, MainViewModel.BuildDashboardCounts 참고), 절대 추측값을 넣지 않는다
/// (CLAUDE.local.md: 지어내지 않는다).
/// 2026-08-29: CoverageRetakeNeeded/DetailCaptureNeeded는 예전엔 "threshold 정책이
/// 아직 없어서 항상 0"이었으나, 운영자 결정으로 자동 판정(기준 확정되는 대로)과
/// 운영자 직접 체크(결과보기/분석·스티칭 화면의 체크박스, facade 경로 키 공용 저장소)
/// 를 함께 쓰는 것으로 방향을 바꿈 -- 더 이상 "항상 0"이 아님.
/// GeneratingReport는 삭제됨(운영자 요청) -- Done이 이미 HasReport 기준이라 "보고서만
/// 완료"를 별도로 셀 필요가 없었음.</summary>
public enum DashboardStatusCategory
{
    PlannedCapture,       // 촬영예정 -- 항상 0 (외부 FacadePreviewer 영역, 신호 없음)
    Capturing,            // 촬영중 -- 항상 0
    CaptureDone,          // 촬영완료 -- 항상 0

    CoverageRetakeNeeded, // 누락 재촬영 필요 -- 자동 판정 + 운영자 직접 체크 (2026-08-29)
    StitchQueued,         // Stitching 대기
    Stitching,            // Stitching 중
    AiAnalyzing,          // AI 분석중
    DetailCaptureNeeded,  // 정밀촬영 필요 -- 자동 판정 + 운영자 직접 체크 (2026-08-29)
    NeedsManualReview,    // 사용자 검토 필요
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
