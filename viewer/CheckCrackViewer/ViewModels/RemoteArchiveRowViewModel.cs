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

    // 2026-08-28: 계약 연동 + 분석 결과 write-back 컬럼(전부 nullable, 아직 채워지지 않은 archive는
    // 빈 문자열로 표시). ContractId는 목록에 그대로 노출(고객 ID 컬럼), customer_name도 같이 태그는
    // 되지만 이 목록엔 아직 별도 컬럼 없음(필요하면 추가). ZipPath(원본)는 이미 있던 다운로드
    // 버튼이 그대로 쓰고, 아래 둘은 새로 추가된 "스티칭 결과 다운로드"/"보고서 다운로드" 버튼의
    // IsEnabled/CommandParameter로 씀.
    public string ContractId => Record.ContractId ?? "";
    public string? StitchingZipPath => Record.StitchingZipPath;
    public string? ReportPath => Record.ReportPath;
    public bool HasStitchingResult => !string.IsNullOrEmpty(Record.StitchingZipPath);
    public bool HasReport => !string.IsNullOrEmpty(Record.ReportPath);

    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(IsNotBusy))]
    [NotifyPropertyChangedFor(nameof(CanDownloadStitchingResult))]
    [NotifyPropertyChangedFor(nameof(CanDownloadReport))]
    private bool _isBusy;

    public bool IsNotBusy => !IsBusy;
    public bool CanDownloadStitchingResult => HasStitchingResult && IsNotBusy;
    public bool CanDownloadReport => HasReport && IsNotBusy;

    // 2026-08-27: 다운로드/등록 액션의 일시적 진행 메시지("다운로드 중...", "압축 해제 중...") --
    // Record.AnalysisStatus(DB에 영구 저장되는 검사완료/진행중/에러/재진행 필요)와는 다른 개념.
    // 화면엔 한 칸만 있어서, 액션이 진행 중이면 이 값을 우선 보여주고 아니면 AnalysisStatus로
    // 대체(DisplayStatus)한다.
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(DisplayStatus))]
    private string _status = "";

    public string DisplayStatus => string.IsNullOrEmpty(Status) ? (Record.AnalysisStatus ?? "") : Status;

    public RemoteArchiveRowViewModel(CrackVisionArchiveRecord record)
    {
        Record = record;
    }
}
