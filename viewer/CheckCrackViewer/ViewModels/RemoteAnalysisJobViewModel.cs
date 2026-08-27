using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.ViewModels;

/// <summary>One row in the "원격 분석 작업" window -- one FacadePreviewer-dispatched
/// AnalysisAssignment, from the moment it arrives through download/extract/analysis/result. See
/// RemoteAnalysisJobsViewModel for the orchestration that drives Status/ProgressText.</summary>
public partial class RemoteAnalysisJobViewModel : ObservableObject
{
    public long ArchiveId { get; init; }
    public string Company { get; init; } = "";
    public string Building { get; init; } = "";
    public string DirectionsDisplay { get; init; } = "";
    public uint ImageCount { get; init; }
    public DateTime ReceivedAt { get; init; } = DateTime.Now;

    [ObservableProperty] private string _status = "대기 중";
    [ObservableProperty] private string _progressText = "";
    [ObservableProperty] private string? _errorMessage;
}
