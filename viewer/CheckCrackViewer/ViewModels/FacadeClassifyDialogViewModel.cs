using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Linq;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace CheckCrackViewer.ViewModels;

/// <summary>One folder about to become (or already is) a facade — its 방위 is the
/// only per-row editable field; 단지/동 are shared across the whole batch.</summary>
public partial class ClassifyRow : ObservableObject
{
    public string FolderPath { get; init; } = "";
    public string FacadeId { get; init; } = "";
    [ObservableProperty] private string _side = "";
}

/// <summary>The confirmed result for one row, read by the caller after ShowDialog()
/// returns true.</summary>
public sealed record FacadeClassifyResult(
    string FolderPath, string FacadeId,
    string ComplexId, string ComplexName,
    string? BuildingId, string? BuildingName,
    string Side);

/// <summary>Backs FacadeClassifyDialog — used both for batch "이미지 폴더 추가"
/// (여러 폴더, 방위만 행별로 다름) and single-facade 재분류 (한 행). 사용자가 직접
/// 확인/수정 후 등록해야만 분류가 확정된다 — 폴더명에서 자동 제안은 MainViewModel이
/// 이 ViewModel을 생성할 때 미리 채워 넣는 것이고, 이 클래스 자체는 추측하지 않는다
/// (CLAUDE.local.md #7: 방향명은 별도 metadata, Viewer가 임의로 확정하지 않음).</summary>
public partial class FacadeClassifyDialogViewModel : ObservableObject
{
    [ObservableProperty] private string _complexName;
    [ObservableProperty] private string _buildingName;

    public ObservableCollection<ClassifyRow> Rows { get; } = new();

    public event Action<bool>? RequestClose;

    public FacadeClassifyDialogViewModel(
        IReadOnlyList<(string FolderPath, string FacadeId, string ProposedSide)> candidates,
        string proposedComplexName,
        string proposedBuildingName = "")
    {
        _complexName = proposedComplexName;
        _buildingName = proposedBuildingName;
        foreach (var c in candidates)
            Rows.Add(new ClassifyRow { FolderPath = c.FolderPath, FacadeId = c.FacadeId, Side = c.ProposedSide });
    }

    public IReadOnlyList<FacadeClassifyResult> Result { get; private set; } = Array.Empty<FacadeClassifyResult>();

    [RelayCommand]
    private void Confirm()
    {
        var complexName = ComplexName.Trim();
        if (complexName.Length == 0)
            return; // 단지명은 필수 -- 비어있으면 등록 버튼이 아무것도 안 함 (에러 대화상자 대신 조용히 무시, 폼이 단순해서 충분)

        var buildingName = BuildingName.Trim();
        string? buildingId = buildingName.Length > 0 ? buildingName : null;

        Result = Rows
            .Select(r => new FacadeClassifyResult(
                r.FolderPath, r.FacadeId,
                ComplexId: complexName, ComplexName: complexName,
                BuildingId: buildingId, BuildingName: buildingId != null ? buildingName : null,
                Side: string.IsNullOrWhiteSpace(r.Side) ? r.FacadeId : r.Side.Trim()))
            .ToList();

        RequestClose?.Invoke(true);
    }

    [RelayCommand]
    private void Cancel() => RequestClose?.Invoke(false);
}
