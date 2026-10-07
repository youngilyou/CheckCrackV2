using System.Windows;
using System.Windows.Media;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.Views;

/// <summary>2026-10-07: 로그인 창 Ctrl+R(숨김)로만 여는 비밀번호 재설정 -- 화면 어디에도 이 기능을 표시하지 않는다.</summary>
public partial class ResetPasswordWindow : Window
{
    public ResetPasswordWindow(string? username = null)
    {
        InitializeComponent();
        TargetInput.Text = username ?? "";
        Loaded += (_, _) => (string.IsNullOrEmpty(TargetInput.Text) ? (UIElement)TargetInput : NewPasswordInput).Focus();
    }

    private void Reset_Click(object sender, RoutedEventArgs e)
    {
        ResultText.Foreground = Brushes.IndianRed;
        if (string.IsNullOrWhiteSpace(TargetInput.Text))
        {
            ResultText.Text = "아이디를 입력하세요.";
            return;
        }
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
            if (!UserStore.ResetPassword(TargetInput.Text, NewPasswordInput.Password))
            {
                ResultText.Text = "해당 아이디가 없습니다.";
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
