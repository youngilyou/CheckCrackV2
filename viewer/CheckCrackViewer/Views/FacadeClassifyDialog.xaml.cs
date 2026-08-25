using System.Collections.Generic;
using System.Windows;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

public partial class FacadeClassifyDialog : Window
{
    public FacadeClassifyDialogViewModel ViewModel { get; }

    public FacadeClassifyDialog(
        IReadOnlyList<(string FolderPath, string FacadeId, string ProposedSide)> candidates,
        string proposedComplexName,
        string proposedBuildingName = "")
    {
        InitializeComponent();
        ViewModel = new FacadeClassifyDialogViewModel(candidates, proposedComplexName, proposedBuildingName);
        DataContext = ViewModel;
        ViewModel.RequestClose += confirmed =>
        {
            DialogResult = confirmed;
            Close();
        };
    }
}
