using System.Collections.Specialized;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using CheckCrackViewer.Services;
using CheckCrackViewer.ViewModels;
using CheckCrackViewer.Views;

namespace CheckCrackViewer;

/// <summary>
/// Interaction logic for MainWindow.xaml
/// </summary>
public partial class MainWindow : Window
{
    // Every double-click used to create a brand-new ImageViewerWindow without
    // closing the last one. Since each window computes its own bounds from
    // the same SystemParameters.WorkArea, two open viewers land at the exact
    // same Left/Top/Width/Height and stack directly on top of each other —
    // on a software-rendered remote session that showed up as "part of the
    // image missing", because the old window's stale render was still
    // sitting there underneath/around the new one. Tracking the single
    // currently-open viewer and closing it before opening the next one
    // guarantees only one is ever on screen.
    private ImageViewerWindow? _currentViewer;

    public MainWindow()
    {
        InitializeComponent();
        TitleBarTheme.Apply(this, (Color)ColorConverter.ConvertFromString("#0A0D10"), Colors.White);
        if (DataContext is MainViewModel vm)
        {
            vm.LogEntries.CollectionChanged += OnLogEntriesChanged;
            // PasswordBox.Password can't be data-bound, so ChangePasswordCommand/
            // ChangeUsernameCommand clearing CurrentPassword/NewPassword/
            // ConfirmNewPassword on the ViewModel doesn't clear what's still
            // typed in these three boxes -- mirror it here once a change
            // actually succeeds.
            vm.PropertyChanged += (_, e) =>
            {
                if (e.PropertyName != nameof(MainViewModel.AccountStatus))
                    return;
                if (vm.AccountStatus == "비밀번호가 변경되었습니다.")
                {
                    CurrentPasswordInput.Clear();
                    NewPasswordInput.Clear();
                    ConfirmNewPasswordInput.Clear();
                }
                else if (vm.AccountStatus == "계정명이 변경되었습니다.")
                {
                    CurrentPasswordInput.Clear();
                }
            };
        }
    }

    private void OnLogEntriesChanged(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action != NotifyCollectionChangedAction.Add || LogListBox.Items.Count == 0)
            return;
        if (DataContext is MainViewModel { AutoScrollLog: false })
            return;
        LogListBox.ScrollIntoView(LogListBox.Items[^1]);
    }

    private void Image_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2)
            return;
        if (sender is not Image { Tag: string path } || string.IsNullOrEmpty(path))
            return;

        _currentViewer?.Close();
        _currentViewer = new ImageViewerWindow(path) { Owner = this };
        _currentViewer.Closed += (_, _) => _currentViewer = null;
        _currentViewer.Show();
    }

    private void LivePreview_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2)
            return;
        if (sender is not Image { DataContext: FacadeItemViewModel facade })
            return;

        _currentViewer?.Close();
        _currentViewer = new ImageViewerWindow(facade) { Owner = this };
        _currentViewer.Closed += (_, _) => _currentViewer = null;
        _currentViewer.Show();
    }

    private void CurrentPasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
            vm.CurrentPassword = CurrentPasswordInput.Password;
    }

    private void NewPasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
            vm.NewPassword = NewPasswordInput.Password;
    }

    private void ConfirmNewPasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
            vm.ConfirmNewPassword = ConfirmNewPasswordInput.Password;
    }
}
