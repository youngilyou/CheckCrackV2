using System.Windows;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

public partial class BuildingInfoDialog : Window
{
    public BuildingInfoDialogViewModel ViewModel { get; }

    public BuildingInfoDialog(string complexName, string buildingName, int? totalFloors, double? floorHeightM)
    {
        InitializeComponent();
        ViewModel = new BuildingInfoDialogViewModel(complexName, buildingName, totalFloors, floorHeightM);
        DataContext = ViewModel;
        ViewModel.RequestClose += confirmed =>
        {
            DialogResult = confirmed;
            Close();
        };
    }
}
