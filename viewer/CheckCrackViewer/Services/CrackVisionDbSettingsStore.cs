using System.IO;
using System.Text.Json;

namespace CheckCrackViewer.Services;

/// <summary>PostgreSQL(CrackVisionDB, MngData backend_core) + SFTP connection settings for the
/// remote analysis download feature. Deliberately a SEPARATE file/class from
/// DbSettingsStore/DbConnectionSettings, which is MySQL-based login-server connection info for
/// a completely different, unrelated future feature (고객 정보) -- see
/// https://github.com/youngilyou/AnalysisLoadBalancer README's own note on why these two must
/// not be conflated. Same %APPDATA%\SmartCrackViewer\*.json persistence pattern as
/// DbSettingsStore, just a different file so the two never collide.</summary>
public sealed class CrackVisionDbSettings
{
    // CrackVisionDB (PostgreSQL, MngData backend_core) -- used only by the "수동 다운로드" browse
    // feature (SELECT from crackvision_archives). The DDS-based automatic path never needs this:
    // AnalysisAssignment already carries everything needed (archive_id/company/building/
    // zip_remote_path/...).
    public string PostgresHost { get; set; } = "";
    public int PostgresPort { get; set; } = 5432;
    public string PostgresDatabase { get; set; } = "mngdata";
    public string PostgresUser { get; set; } = "mngdata";
    public string PostgresPassword { get; set; } = "";

    // SFTP -- the actual zip download transport, used by BOTH the automatic (AnalysisAssignment)
    // and manual paths. Same host as the DDS-Router/backend_core host in the common case (the
    // archive zip lives on that host's local disk, see crackvision_archive_manager.cpp).
    // Password auth only (2026-08-27 operator decision) -- no private-key option here.
    public string SftpHost { get; set; } = "";
    public int SftpPort { get; set; } = 22;
    public string SftpUser { get; set; } = "";
    public string SftpPassword { get; set; } = "";

    // This workstation's own identity for facade_analysis_msgs (WorkerHeartbeat.worker_id,
    // stamped on every outgoing message). Defaults to the machine's hostname if left blank --
    // see AnalysisBridgeService callers.
    public string WorkerId { get; set; } = "";

    // "수동 다운로드" 목록에서 zip을 받고 압축 해제할 위치 -- 이전엔 RootPath(앱 프로젝트 루트)
    // 밑에 고정이었는데, 운용자가 직접 고를 수 있어야 한다는 요청으로 분리(2026-08-27).
    public string DownloadFolder { get; set; } = @"C:\temp";
}

public static class CrackVisionDbSettingsStore
{
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "SmartCrackViewer");

    public static readonly string SettingsPath = Path.Combine(SettingsDir, "crackvision_db_settings.json");

    public static CrackVisionDbSettings Load()
    {
        if (!File.Exists(SettingsPath))
            return new CrackVisionDbSettings();
        try
        {
            return JsonSerializer.Deserialize<CrackVisionDbSettings>(File.ReadAllText(SettingsPath))
                ?? new CrackVisionDbSettings();
        }
        catch (JsonException)
        {
            return new CrackVisionDbSettings();
        }
    }

    public static void Save(CrackVisionDbSettings settings)
    {
        Directory.CreateDirectory(SettingsDir);
        File.WriteAllText(SettingsPath, JsonSerializer.Serialize(settings, new JsonSerializerOptions { WriteIndented = true }));
    }
}
