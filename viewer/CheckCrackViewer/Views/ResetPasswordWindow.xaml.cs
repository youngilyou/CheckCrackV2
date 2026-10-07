using System.Windows;
using System.Windows.Media;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.Views;

public partial class ResetPasswordWindow : Window
{
    public ResetPasswordWindow(string? username = null)
    {
        InitializeComponent();
        TargetInput.Text = username ?? "";
        // 계정이 하나뿐이면 확인해 줄 다른 관리자가 없다 -- 재설정 불가를 먼저 알린다.
        if (UserStore.CountUsers() <= 1)
        {
            GuideText.Text = "이 PC에는 계정이 하나뿐이라 다른 관리자 확인으로 재설정할 수 없습니다. "
                + "시스템 관리자에게 문의하세요. (평소에 관리자 계정을 하나 더 만들어 두면 여기서 재설정할 수 있습니다.)";
            ResetButton.IsEnabled = false;
        }
        Loaded += (_, _) => (string.IsNullOrEmpty(TargetInput.Text) ? (UIElement)TargetInput : NewPasswordInput).Focus();
    }

    private void Reset_Click(object sender, RoutedEventArgs e)
    {
        ResultText.Foreground = Brushes.IndianRed;
        if (NewPasswordInput.Password.Length < 4)
        {
            ResultText.Text = "새 비밀번호는 4자 이상이어야 합니다.";
            return;
        }
        if (NewPasswordInput.Password != ConfirmInput.Password)
        {
            ResultText.Text = "새 비밀번호가 일치하지 않습니다.";
            return;
        }
        try
        {
            var (ok, error) = UserStore.ResetPasswordByAdmin(TargetInput.Text, AdminInput.Text,
                AdminPasswordInput.Password, NewPasswordInput.Password);
            if (!ok)
            {
                ResultText.Text = error;
                return;
            }
            ResultText.Foreground = Brushes.SeaGreen;
            ResultText.Text = "비밀번호를 재설정했습니다. 새 비밀번호로 로그인하세요.";
            ResetButton.IsEnabled = false;
        }
        catch (Exception ex)
        {
            ResultText.Text = $"재설정 실패: {ex.Message}";
        }
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
