using System;
using System.IO;
using System.Text.Json;

namespace CheckCrackViewer.Services;

/// <summary>%APPDATA%\SmartCrackViewer\login_prefs.json -- remembers only the
/// last-used 아이디 when "로그인 정보 저장" is checked, never the password
/// (unlike DbSettingsStore's optional SavePassword for the MySQL connection
/// page, login credentials aren't worth that risk just for convenience).</summary>
public static class LoginPreferencesStore
{
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "SmartCrackViewer");

    private static readonly string SettingsPath = Path.Combine(SettingsDir, "login_prefs.json");

    public static (bool Remember, string Username) Load()
    {
        if (!File.Exists(SettingsPath))
            return (false, "");
        try
        {
            var prefs = JsonSerializer.Deserialize<LoginPrefs>(File.ReadAllText(SettingsPath));
            return prefs is { RememberLogin: true } ? (true, prefs.Username) : (false, "");
        }
        catch (JsonException)
        {
            return (false, "");
        }
    }

    public static void Save(bool remember, string username)
    {
        Directory.CreateDirectory(SettingsDir);
        var prefs = new LoginPrefs { RememberLogin = remember, Username = remember ? username : "" };
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(prefs));
    }

    private sealed class LoginPrefs
    {
        public bool RememberLogin { get; set; }
        public string Username { get; set; } = "";
    }
}
