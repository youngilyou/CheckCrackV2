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
        PreviewKeyDown += LoginWindow_PreviewKeyDown;
    }

    // 2026-10-07 (사용자 결정): 비밀번호 재설정은 Ctrl+R로만 연다 -- 화면 어디에도 표시하지 않는다(아는 사람만 사용).
    // 입력 칸에 포커스가 있어도 동작하도록 창 단위 PreviewKeyDown에서 받는다.
    private void LoginWindow_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.R && Keyboard.Modifiers == ModifierKeys.Control)
        {
            e.Handled = true;
            new ResetPasswordWindow(ViewModel.Username) { Owner = this }.ShowDialog();
        }
    }

    // 2026-10-07: 아이디/비밀번호 찾기 -- 이 PC의 로컬 계정(SQLite) 기준. 회사 계정(SmartOneFlow) 연동은 차후.
    private void FindId_Click(object sender, MouseButtonEventArgs e) =>
        new FindIdWindow { Owner = this }.ShowDialog();

    // 비밀번호는 되돌릴 수 없는 형태(BCrypt)로만 저장돼 찾아서 보여줄 수 없고, 임시 비밀번호도 발급하지 않는다(사용자 결정).
    private void ResetPassword_Click(object sender, MouseButtonEventArgs e) =>
        MessageBox.Show(this,
            "비밀번호는 암호화되어 저장되므로 찾아서 보여드릴 수 없습니다.\n관리자에게 문의하세요.",
            "비밀번호 찾기", MessageBoxButton.OK, MessageBoxImage.Information);

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
