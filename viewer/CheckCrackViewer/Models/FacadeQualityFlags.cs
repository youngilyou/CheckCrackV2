using System.Text.Json.Serialization;

namespace CheckCrackViewer.Models;

/// <summary>{facade_id}_quality_flags.json -- operator-facing follow-up flags for a facade,
/// separate from the AI's own {facade_id}_quality_report*.json (CLAUDE.local.md: this Viewer
/// never guesses a numeric threshold for "needs re-shoot"/"needs detail shoot" on its own).
///
/// 2026-08-29 사용자 결정: 누락 재촬영 필요(NeedsRetake)는 순수 운영자 판단(스티칭 결과를
/// 직접 보고 결정) -- 자동 판정 없음. 정밀촬영 필요(NeedsDetailCapture)는 앱이 먼저 의심되면
/// 자동으로 체크해두고, 운영자가 최종 확인/해제한다 -- 자동 판정 기준은
/// FacadeQualityFlagsStore.SuggestNeedsDetailCapture 참고.</summary>
public sealed class FacadeQualityFlagsFile
{
    [JsonPropertyName("needs_retake")] public bool NeedsRetake { get; set; }
    [JsonPropertyName("needs_detail_capture")] public bool NeedsDetailCapture { get; set; }

    /// <summary>true면 NeedsDetailCapture가 아직 앱의 자동 의심 판정 그대로이고 운영자가
    /// 한 번도 손대지 않은 상태 -- 다음 스캔에서 자동 판정 로직이 다시 계산해서 이 파일을
    /// 덮어써도 됨(품질 지표가 재계산되며 값이 바뀔 수 있음, 예: 재실행 후). 운영자가
    /// 체크박스를 한 번이라도 건드리면 false로 고정되고, 그 뒤로는 어떤 자동 재계산도
    /// 이 값을 다시 덮어쓰지 않는다 -- "최종 판단은 운영자"라는 요구사항을 지키기 위함.</summary>
    [JsonPropertyName("detail_capture_auto_suggested")] public bool DetailCaptureAutoSuggested { get; set; }
}
