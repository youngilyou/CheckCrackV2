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

    /// <summary>2026-08-29: 방위(면)가 여러 개인 archive는 facade마다 결과가 따로 있음
    /// (facade_analysis_results, 위 flat StitchingZipPath/ReportPath는 그중 가장 최근 것뿐).
    /// 화면은 단일 방향(대다수 케이스)일 땐 기존 버튼 그대로, 다중 방향일 때만 facade별
    /// 미니 다운로드 목록을 추가로 보여준다(MainViewModel.DownloadFacadeStitchingResult/
    /// DownloadFacadeReport, SettingsView.xaml).</summary>
    public IReadOnlyList<FacadeAnalysisResultEntry> FacadeResults => Record.FacadeResults;
    public bool HasMultipleFacadeResults => Record.FacadeResults.Count > 1;

    // 2026-08-29: 단일 방향 버튼("스티칭 결과"/"보고서")이 보여야 하는 두 조건 -- (1) 다중 방향이
    // 아니어야 함(다중이면 위 FacadeResults 미니 목록으로 대체) AND (2) 결과가 실제로 존재해야 함
    // (검토/분석이 아직 안 끝난 archive에 버튼만 비활성화된 채로 계속 보이던 버그 수정 -- 운영자
    // 요청: "반드시 검토 완료된 것만 버튼이 표시되어야 함"). 둘 다 만족할 때만 버튼 자체가 보임.
    public bool ShowSingleStitchingResult => HasStitchingResult && !HasMultipleFacadeResults;
    public bool ShowSingleReport => HasReport && !HasMultipleFacadeResults;

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

    // 2026-08-29: 실제로 겪은 버그 -- 방금 저장만 되고 아직 분석이 안 된 archive는
    // Record.AnalysisStatus가 DB에서 NULL로 와서 이 칸이 완전히 빈 문자열로 보였음(운영자
    // 입장에선 "상태를 아예 안 보여주는 버그"처럼 보임). "아직 시작 안 됨"이라는 것 자체가
    // 유의미한 상태이므로, 진행 중 메시지도 없고 DB에 저장된 분석 상태도 없으면 "미진행"으로
    // 명시적으로 표시한다.
    public string DisplayStatus => string.IsNullOrEmpty(Status) ? (Record.AnalysisStatus ?? "미진행") : Status;

    public RemoteArchiveRowViewModel(CrackVisionArchiveRecord record)
    {
        Record = record;
    }
}

/// <summary>Bundles a row with one of its (possibly several) FacadeAnalysisResultEntry items --
/// the per-facade mini download buttons need both: the entry for the remote path, the row for
/// IsBusy/Status feedback (same as the existing single-result download buttons use). Built via
/// RowAndFacadeToRequestConverter (MultiBinding) in SettingsView.xaml.</summary>
public sealed record FacadeDownloadRequest(RemoteArchiveRowViewModel Row, FacadeAnalysisResultEntry Entry);
