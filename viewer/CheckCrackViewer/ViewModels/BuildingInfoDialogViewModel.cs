using System;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CheckCrackViewer.ViewModels;

/// <summary>Backs BuildingInfoDialog -- 총 층수/층고 수동 입력 (memory/floor_labeling_needs_bim.md:
/// 정확한 층수 표시는 BIM/설계 도면이 있어야 가능하지만, 지금은 없으므로 사용자가 직접 입력하는
/// 임시 방안. 나중에 DB가 연결되면 고객이 입력한 값을 그 DB에서 읽어오는 쪽으로 바뀔 예정이라,
/// 이 다이얼로그는 "값을 어떻게 저장/조회하는지"는 전혀 모르고 오직 입력 폼 역할만 한다
/// (BuildingMetadataStore가 저장을 담당, 나중에 백엔드를 바꿔도 이 다이얼로그는 안 바뀜).</summary>
public partial class BuildingInfoDialogViewModel : ObservableObject
{
    public string ComplexName { get; }
    public string BuildingName { get; }

    [ObservableProperty] private string _totalFloorsText;
    [ObservableProperty] private string _floorHeightText;

    public event Action<bool>? RequestClose;

    public BuildingInfoDialogViewModel(string complexName, string buildingName, int? totalFloors, double? floorHeightM)
    {
        ComplexName = complexName;
        BuildingName = buildingName;
        _totalFloorsText = totalFloors?.ToString() ?? "";
        _floorHeightText = floorHeightM?.ToString("0.##") ?? "";
    }

    public int? ResultTotalFloors { get; private set; }
    public double? ResultFloorHeightM { get; private set; }

    [RelayCommand]
    private void Confirm()
    {
        // 둘 다 비워두는 것도 허용(= "아직 모름", 층수 표시 기능이 그냥 안 켜짐) -- 값이
        // 있으면 정수/실수로 정상 파싱될 때만 저장, 이상한 입력이면 조용히 무시(폼이
        // 단순해서 에러 대화상자 없이도 충분 -- FacadeClassifyDialogViewModel과 동일한 원칙).
        ResultTotalFloors = int.TryParse(TotalFloorsText.Trim(), out var floors) && floors > 0 ? floors : null;
        ResultFloorHeightM = double.TryParse(FloorHeightText.Trim(), out var height) && height > 0 ? height : null;
        RequestClose?.Invoke(true);
    }

    [RelayCommand]
    private void Cancel() => RequestClose?.Invoke(false);
}
