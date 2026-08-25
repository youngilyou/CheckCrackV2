using System;
using System.Threading.Tasks;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using CheckCrackViewer.Models;
using CheckCrackViewer.Services;

namespace CheckCrackViewer.ViewModels;

/// <summary>Backs LoginWindow -- shown before MainWindow (see App.xaml.cs).
/// Just CheckCrack's own username/password/로그인 button. UserStore.EnsureCreated
/// seeds a default admin/admin123 account on first run, so there's no
/// account-creation step here at all -- changing/adding accounts is the
/// "설정" page's job (MainViewModel.ChangePassword), not the login screen's.</summary>
public partial class LoginViewModel : ObservableObject
{
    [ObservableProperty] private string _username = "";
    [ObservableProperty] private string _password = "";
    [ObservableProperty] private string _statusText = "";
    [ObservableProperty] private bool _isBusy;
    [ObservableProperty] private bool _rememberLogin;

    public AppUser? LoggedInUser { get; private set; }
    public event Action? LoginSucceeded;

    public LoginViewModel()
    {
        UserStore.EnsureCreated();
        var (remember, username) = LoginPreferencesStore.Load();
        RememberLogin = remember;
        Username = username;
    }

    partial void OnUsernameChanged(string value) => LoginCommand.NotifyCanExecuteChanged();
    partial void OnPasswordChanged(string value) => LoginCommand.NotifyCanExecuteChanged();
    partial void OnIsBusyChanged(bool value) => LoginCommand.NotifyCanExecuteChanged();

    private bool CanLogin() => !IsBusy && !string.IsNullOrWhiteSpace(Username) && !string.IsNullOrEmpty(Password);

    [RelayCommand(CanExecute = nameof(CanLogin))]
    private async Task Login()
    {
        IsBusy = true;
        StatusText = "로그인 중…";
        try
        {
            var user = await UserStore.ValidateLoginAsync(Username, Password);
            if (user == null)
            {
                StatusText = "아이디 또는 비밀번호가 올바르지 않습니다.";
                return;
            }
            LoggedInUser = user;
            StatusText = "";
            LoginPreferencesStore.Save(RememberLogin, Username);
            LoginSucceeded?.Invoke();
        }
        catch (Exception ex)
        {
            StatusText = $"로그인 실패: {ex.Message}";
        }
        finally
        {
            IsBusy = false;
        }
    }
}
