namespace CheckCrackViewer.Services;

/// <summary>Generic Label/Value pair for ComboBox `ItemsSource` bindings
/// (`DisplayMemberPath="Label"`, `SelectedValuePath="Value"`) — same shape as
/// `IdentifyViewClient.ViewModeOption`, but not tied to that one screen.
/// 2026-09-24: introduced for the 매칭 알고리즘(설정 화면)/구조물 유형(단지
/// 종합보고서 옆) combo boxes, both of which need a Korean display label over
/// an English backend value the Python pipeline expects verbatim.</summary>
public sealed record SelectOption(string Value, string Label);
