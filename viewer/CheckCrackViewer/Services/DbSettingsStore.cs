using System;
using System.IO;
using System.Text.Json;

namespace CheckCrackViewer.Services;

public sealed class DbConnectionSettings
{
    public string Host { get; set; } = "";
    public int Port { get; set; } = 3306;
    public string Database { get; set; } = "";
    public string User { get; set; } = "";
    public string Password { get; set; } = "";
    public bool UseSsl { get; set; }
    public int TimeoutSeconds { get; set; } = 5;
    public bool SavePassword { get; set; }
}

/// <summary>Shared %APPDATA%\SmartCrackViewer\db_settings.json load/save --
/// pulled out of MainViewModel so LoginWindow (which is shown before
/// MainViewModel is ever constructed, see App.xaml.cs) can read the same
/// connection info to know which MySQL server to authenticate against.
/// MainViewModel keeps its own observable properties/UI for the "설정" page
/// but delegates the actual file I/O here instead of duplicating it.</summary>
public static class DbSettingsStore
{
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "SmartCrackViewer");

    public static readonly string SettingsPath = Path.Combine(SettingsDir, "db_settings.json");

    public static DbConnectionSettings? Load()
    {
        if (!File.Exists(SettingsPath))
            return null;
        return JsonSerializer.Deserialize<DbConnectionSettings>(File.ReadAllText(SettingsPath));
    }

    public static void Save(DbConnectionSettings settings)
    {
        Directory.CreateDirectory(SettingsDir);
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(settings, new JsonSerializerOptions { WriteIndented = true }));
    }
}
