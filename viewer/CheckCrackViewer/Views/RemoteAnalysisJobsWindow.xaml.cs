using System.Windows;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

public partial class RemoteAnalysisJobsWindow : Window
{
    public RemoteAnalysisJobsWindow(RemoteAnalysisJobsViewModel viewModel)
    {
        InitializeComponent();
        DataContext = viewModel;
    }
}
