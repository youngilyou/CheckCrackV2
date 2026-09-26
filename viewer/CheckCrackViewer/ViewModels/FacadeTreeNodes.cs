using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using CheckCrackViewer.Services;
using CommunityToolkit.Mvvm.ComponentModel;

namespace CheckCrackViewer.ViewModels;

/// <summary>단지(Complex) 노드 — 분석·스티칭 화면 FACADES 트리의 최상위 레벨.
/// Children은 BuildingNode와 SideGroupNode를 섞어서 담을 수 있는 폴리모픽
/// 컬렉션이다: 한 단지 안에서 일부 facade는 동이 분류돼 있고 일부는 없는
/// 뒤섞인 상태를 그대로 표현하기 위함(동은 선택 사항이라는 요구사항).
/// WPF의 암시적(DataType만 지정한) DataTemplate 매칭이 실제 렌더링 시
/// BuildingNode/SideGroupNode를 구분해 그린다.</summary>
public partial class ComplexNode : ObservableObject
{
    public string ComplexId { get; init; } = "";
    public string ComplexName { get; init; } = "";
    [ObservableProperty] private bool _isExpanded = true;
    public ObservableCollection<object> Children { get; } = new();

    /// <summary>"APARTMENT"/"DAM"/"FACTORY" -- 단지 종합보고서 버튼 옆 콤보박스
    /// (2026-09-24). This node is transient (RebuildFacadeTree recreates it
    /// every refresh, see BuildingNode's own doc comment above), so
    /// MainViewModel sets both the initial value (from ComplexSettingsStore,
    /// keyed by ComplexId) AND <see cref="OnCommit"/> right after
    /// constructing each node -- the property setter below fires that
    /// callback so a combo-box selection change persists immediately without
    /// needing a separate "저장" button.</summary>
    [ObservableProperty] private string _structureType = "APARTMENT";

    /// <summary>Set once by MainViewModel.RebuildFacadeTree right after
    /// construction; invoked from OnStructureTypeChanged so a UI selection
    /// writes straight through to ComplexSettingsStore. Never invoked during
    /// the initial load itself (that assigns the field directly via the
    /// backing property before this callback is wired, so loading a saved
    /// value never re-triggers a redundant save).</summary>
    public Action<ComplexNode>? OnStructureTypeCommitted { get; set; }

    public static IReadOnlyList<SelectOption> StructureTypeOptions { get; } = new[]
    {
        new SelectOption("APARTMENT", "아파트"),
        new SelectOption("DAM", "댐"),
        new SelectOption("FACTORY", "공장"),
    };

    partial void OnStructureTypeChanged(string value) => OnStructureTypeCommitted?.Invoke(this);
}

/// <summary>동(Building) 노드 — 선택 사항 레벨. Children은 SideGroupNode만 담는다.
/// ComplexId/ComplexName은 부모 노드 포인터가 없어서(RebuildFacadeTree가 매번 새로
/// 만드는 트랜지언트 노드) 자신을 소유한 단지를 스스로 알 수 있게 별도로 들고 있음
/// -- 우클릭 "추가"(2026-08-27)가 새 facade를 정확히 이 단지 밑에 붙이려면 필요.</summary>
public partial class BuildingNode : ObservableObject
{
    public string ComplexId { get; init; } = "";
    public string ComplexName { get; init; } = "";
    public string BuildingId { get; init; } = "";
    public string BuildingName { get; init; } = "";
    [ObservableProperty] private bool _isExpanded = true;
    public ObservableCollection<object> Children { get; } = new();
}

/// <summary>방위(Side) 그룹 — 트리의 최하위 그룹핑 레벨, 실제 Facade들을 담는 leaf 컨테이너.
/// CLAUDE.local.md #4 "N/E/S/W는 UI/관리용 방향 그룹일 뿐 실제 Stitching ID가 아니다"에 따라
/// 여러 Facade가 같은 Side 아래 묶일 수 있다 — 처리 단위는 여전히 Facade 개별이다.
///
/// Facades는 object로 폴리모픽: 분석·스티칭 화면(MainViewModel)은 여기에
/// FacadeItemViewModel을, 결과보기 화면(ResultsCompareViewModel)은 자체
/// FacadeSnapshot을 담아 같은 트리 구조/스타일을 재사용한다 — 두 화면의 leaf
/// 카드 내용(실행 버튼 유무 등)은 각자 UserControl의 DataTemplate이 따로
/// 그린다.</summary>
public partial class SideGroupNode : ObservableObject
{
    public string Side { get; init; } = "";
    [ObservableProperty] private bool _isExpanded = true;
    public ObservableCollection<object> Facades { get; } = new();
}
