using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

public sealed class PipelineSettings
{
    /// <summary>"loftr" (default) / "hloc" / "sift" — forwarded to
    /// tools/stitch_folder.py's --matcher-backend, which threads it into
    /// src/sfm/colmap_runner.run_colmap. See that function's docstring for
    /// the full resolution order (this value wins over the YAML config's
    /// own colmap.matcher_backend/use_loftr_matching when set here).</summary>
    [JsonPropertyName("matcher_backend")] public string MatcherBackend { get; set; } = "loftr";
}

/// <summary>Owns RootPath/pipeline_settings.json: a project-wide (not
/// per-complex/per-facade) pipeline behavior toggle set from the Settings
/// 화면. 2026-09-24, explicit user request: "LoFTR 적용, hloc(SuperPoint) 적용
/// 설정 창에 선택 콤보 박스 추가 하삼 -- 라이선스 문제 고려하지 않음". Same
/// project-root-scoped JSON file convention as FacadeHierarchyStore (not
/// %APPDATA%, unlike DbSettingsStore -- this changes actual processing
/// behavior tied to THIS project's pipeline runs, not a machine-wide DB
/// credential).</summary>
public static class PipelineSettingsStore
{
    private const string FileName = "pipeline_settings.json";
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    private static string SettingsPath(string rootPath) => Path.Combine(rootPath, FileName);

    public static PipelineSettings Load(string rootPath)
    {
        var path = SettingsPath(rootPath);
        if (!File.Exists(path))
            return new PipelineSettings();
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<PipelineSettings>(stream) ?? new PipelineSettings();
        }
        catch (JsonException)
        {
            return new PipelineSettings();
        }
        catch (IOException)
        {
            return new PipelineSettings();
        }
    }

    public static void Save(string rootPath, PipelineSettings settings)
    {
        Directory.CreateDirectory(rootPath);
        var path = SettingsPath(rootPath);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(settings, SerializerOptions));
        File.Move(tempPath, path, overwrite: true); // same atomic-rename pattern as FacadeHierarchyStore
    }
}
