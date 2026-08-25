using System.Collections.ObjectModel;
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
}

/// <summary>동(Building) 노드 — 선택 사항 레벨. Children은 SideGroupNode만 담는다.</summary>
public partial class BuildingNode : ObservableObject
{
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
