using CheckCrackViewer.Services;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.ViewModels;

/// <summary>Wraps one CrackVisionArchiveRecord (immutable DB row) with a live-updating Status
/// string for the "수동 다운로드" list -- the record itself stays a plain read-only mapping of
/// crackvision_archives, this is purely a display concern. Without this wrapper the only
/// progress feedback was a single shared status TextBlock positioned next to the 저장 button,
/// far from the row/button the operator actually clicked -- effectively invisible in practice
/// (reported 2026-08-27: "다운로드를 눌렀고 진행 상태를 보았으면 합니다... 어떤 상태인지를
/// 알수없음").</summary>
public sealed partial class RemoteArchiveRowViewModel : ObservableObject
{
    public CrackVisionArchiveRecord Record { get; }

    public long ArchiveId => Record.ArchiveId;
    public string Company => Record.Company;
    public string Building => Record.Building;
    public int ImageCount => Record.ImageCount;
    public DateTime CreatedAt => Record.CreatedAt;

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsNotBusy))]
    private bool _isBusy;

    public bool IsNotBusy => !IsBusy;

    [ObservableProperty] private string _status = "";

    public RemoteArchiveRowViewModel(CrackVisionArchiveRecord record)
    {
        Record = record;
    }
}
