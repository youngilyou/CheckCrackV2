using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using CheckCrackViewer.Services;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

public partial class LoginWindow : Window
{
    public LoginViewModel ViewModel { get; }

    public LoginWindow()
    {
        InitializeComponent();
        TitleBarTheme.Apply(this, (Color)ColorConverter.ConvertFromString("#0A0D10"), Colors.White);
        ViewModel = new LoginViewModel();
        DataContext = ViewModel;
        ViewModel.LoginSucceeded += () =>
        {
            DialogResult = true;
            Close();
        };
    }

    private void PasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is LoginViewModel vm)
            vm.Password = PasswordInput.Password;
        PasswordPlaceholder.Visibility = string.IsNullOrEmpty(PasswordInput.Password)
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    // IsDefault on the 로그인 button should already catch Enter, but PasswordBox
    // focus doesn't always propagate it reliably in WPF -- explicit fallback.
    private void PasswordInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter || DataContext is not LoginViewModel vm)
            return;
        if (vm.LoginCommand.CanExecute(null))
            vm.LoginCommand.Execute(null);
    }
}
