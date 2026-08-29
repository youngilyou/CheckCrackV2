using System.Windows;
using System.Windows.Controls;
using CheckCrackViewer.ViewModels;

namespace CheckCrackViewer.Views;

/// <summary>설정 화면 (계정/연결 · 시스템 상태). MainWindow의 다른 코드와 섞이지 않도록
/// 이 View 전용 코드는 여기에만 둔다 -- DataContext는 MainWindow에서 그대로 상속받은
/// MainViewModel(전용 자식 ViewModel은 없음, 설정 관련 속성/커맨드가 원래 MainViewModel에
/// 있었기 때문).</summary>
public partial class SettingsView : UserControl
{
    public SettingsView()
    {
        InitializeComponent();
        // PasswordBox.Password can't be data-bound, so ChangePasswordCommand/
        // ChangeUsernameCommand clearing CurrentPassword/NewPassword/
        // ConfirmNewPassword on the ViewModel doesn't clear what's still typed
        // in these three boxes -- mirror it here once a change actually succeeds.
        // DataContext isn't set until this control is parented into MainWindow's
        // tree, so wire this up on Loaded rather than in the constructor body.
        Loaded += (_, _) =>
        {
            if (DataContext is not MainViewModel vm)
                return;
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

            // 2026-08-29: CrackVisionDB Postgres/SFTP 비밀번호는 (계정 비밀번호 변경 필드들과
            // 달리) 저장된 설정에서 로드되어 화면에 채워져 있어야 하는 값 -- PasswordBox.Password는
            // 바인딩을 지원하지 않으므로 로드 시점에 한 번 직접 밀어넣는다. LoadCrackVisionSettings()가
            // 이미 생성자에서 실행 완료된 뒤 이 Loaded가 붙으므로 이 시점의 VM 값이 곧 저장된 값이다.
            CrackVisionPostgresPasswordInput.Password = vm.CrackVisionPostgresPassword;
            CrackVisionSftpPasswordInput.Password = vm.CrackVisionSftpPassword;
        };
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

    private void CrackVisionPostgresPasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
            vm.CrackVisionPostgresPassword = CrackVisionPostgresPasswordInput.Password;
    }

    private void CrackVisionSftpPasswordInput_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (DataContext is MainViewModel vm)
            vm.CrackVisionSftpPassword = CrackVisionSftpPasswordInput.Password;
    }
}
