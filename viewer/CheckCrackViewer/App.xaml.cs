using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using CheckCrackViewer.Services;
using CheckCrackViewer.ViewModels;
using CheckCrackViewer.Views;

namespace CheckCrackViewer;

/// <summary>
/// Interaction logic for App.xaml
/// </summary>
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // Remote sessions (TeamViewer/RDP/some VM consoles) frequently can't
        // relay WPF's GPU-accelerated (DirectX) surface — the window renders
        // fine on the physical/local display but shows solid black to a
        // remote viewer. Forcing software rendering trades a little GPU
        // performance for a window that actually renders over remote
        // desktop, which matters far more for a monitoring tool like this.
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;

        // Stays OnExplicitShutdown for the app's entire lifetime now (not just
        // the login step) -- 로그아웃 closes MainWindow and loops back to a new
        // LoginWindow, and the default OnLastWindowClose would tear the whole
        // process down the instant MainWindow closes, before the next
        // LoginWindow ever gets a chance to show. Only ExitApplication (종료)
        // and a cancelled login call Shutdown() explicitly now.
        ShutdownMode = ShutdownMode.OnExplicitShutdown;

        ShowLoginThenMain();
    }

    /// <summary>Login → MainWindow, looping back here on 로그아웃 instead of
    /// exiting the process. ShowDialog() blocks until the respective window
    /// closes, so this stays synchronous -- MainWindow is only ever reached
    /// once a real login succeeded (LoginWindow sets DialogResult=true itself,
    /// see LoginWindow.xaml.cs's LoginSucceeded handler).</summary>
    private void ShowLoginThenMain()
    {
        var login = new LoginWindow();
        var loggedIn = login.ShowDialog();
        if (loggedIn != true)
        {
            Shutdown();
            return;
        }

        var main = new MainWindow();
        // Distinguishes "closed via 로그아웃" (loop back to a new LoginWindow)
        // from every other way MainWindow can close -- the titlebar X button,
        // Alt+F4, 종료's Application.Current.Shutdown() -- which must still end
        // the process. Without this flag, OnExplicitShutdown (needed so a
        // 로그아웃-triggered close doesn't tear the whole app down before the
        // next LoginWindow shows) left the X button just closing the window
        // and leaving the process running invisibly with no windows at all.
        var loggingOut = false;
        if (main.DataContext is MainViewModel vm)
        {
            vm.LoggedInUsername = login.ViewModel.LoggedInUser?.Username ?? "";
            vm.LogoutRequested += () =>
            {
                loggingOut = true;
                main.Close();
                ShowLoginThenMain();
            };
        }
        main.Closed += (_, _) =>
        {
            if (!loggingOut)
                Shutdown();
        };
        // LoginWindow was shown first (via ShowDialog), which would otherwise
        // become Application.MainWindow by default -- reassign explicitly so
        // it points at the real main window once login is done.
        MainWindow = main;
        main.Show();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // See ChildProcessRegistry's own doc comment -- without this, closing
        // the app while a stitching/training subprocess is still running left
        // it orphaned and still eating GPU/CPU indefinitely.
        ChildProcessRegistry.KillAll();
        base.OnExit(e);
    }
}
