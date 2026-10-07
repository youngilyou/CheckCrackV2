using System.Windows;
using System.Windows.Input;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.Views;

public partial class FindIdWindow : Window
{
    public FindIdWindow()
    {
        InitializeComponent();
        Loaded += (_, _) => NameInput.Focus();
    }

    private void Find_Click(object sender, RoutedEventArgs e)
    {
        var name = NameInput.Text.Trim();
        if (name.Length == 0)
        {
            ResultText.Text = "이름을 입력하세요.";
            return;
        }
        try
        {
            var found = UserStore.FindUsernamesByDisplayName(name);
            ResultText.Text = found.Count == 0
                ? "이 이름으로 등록된 계정이 없습니다. 관리자에게 문의하세요."
                : "등록된 아이디:\n" + string.Join("\n", found.Select(f => $"  {f.MaskedUsername}"))
                  + "\n\n아이디 일부는 보안을 위해 가려집니다. 기억나지 않으면 관리자에게 문의하세요.";
        }
        catch (Exception ex)
        {
            ResultText.Text = $"조회 실패: {ex.Message}";
        }
    }

    private void NameInput_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.Enter)
            Find_Click(sender, e);
    }

    private void Close_Click(object sender, RoutedEventArgs e) => Close();
}
