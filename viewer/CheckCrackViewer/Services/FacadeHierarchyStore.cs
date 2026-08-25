using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

public sealed class FacadeClassificationEntry
{
    [JsonPropertyName("key")] public string Key { get; set; } = "";
    [JsonPropertyName("facade_id")] public string FacadeId { get; set; } = "";
    [JsonPropertyName("complex_id")] public string ComplexId { get; set; } = "";
    [JsonPropertyName("complex_name")] public string ComplexName { get; set; } = "";
    [JsonPropertyName("building_id")] public string? BuildingId { get; set; }
    [JsonPropertyName("building_name")] public string? BuildingName { get; set; }
    [JsonPropertyName("side")] public string Side { get; set; } = "";
}

public sealed class FacadeHierarchyIndex
{
    [JsonPropertyName("facades")] public List<FacadeClassificationEntry> Facades { get; set; } = new();
}

/// <summary>Owns RootPath/facade_hierarchy.json: display-only 단지(Complex)/동(Building,
/// optional)/방위(Side) classification for facades in the 분석·스티칭 FACADES tree.
///
/// Deliberately does NOT touch FacadeId itself, which stays the app-wide unique
/// key it already is (see MainViewModel.GetOrCreateFacade) — this store only
/// layers grouping metadata on top for display, keyed by KeyFor(...), never by
/// bare FacadeId (two different complexes could legitimately reuse the same
/// leaf folder name like "TOP").
///
/// Lives at the project root (like facades/, logs/), not %APPDATA%, because
/// classification is per-project data tied to RootPath, unlike DbSettingsStore's
/// machine-wide DB connection settings.</summary>
public static class FacadeHierarchyStore
{
    private const string FileName = "facade_hierarchy.json";
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    /// <summary>A facade's classification key: its SourceFolderPath when known
    /// (set via "이미지 폴더 추가"), else a synthetic "facades/{facadeId}" key
    /// matching where RescanFacadeOutputs discovers CLI-produced facades that
    /// never went through the folder-add dialog. Both read and write paths must
    /// use this same helper so classification round-trips consistently.</summary>
    public static string KeyFor(string? sourceFolderPath, string facadeId) =>
        !string.IsNullOrEmpty(sourceFolderPath) ? sourceFolderPath : $"facades/{facadeId}";

    private static string IndexPath(string rootPath) => Path.Combine(rootPath, FileName);

    public static FacadeHierarchyIndex Load(string rootPath)
    {
        var path = IndexPath(rootPath);
        if (!File.Exists(path))
            return new FacadeHierarchyIndex();
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<FacadeHierarchyIndex>(stream) ?? new FacadeHierarchyIndex();
        }
        catch (JsonException)
        {
            return new FacadeHierarchyIndex(); // corrupt/partial -- treat as "nothing classified yet"
        }
        catch (IOException)
        {
            return new FacadeHierarchyIndex();
        }
    }

    private static void Save(string rootPath, FacadeHierarchyIndex index)
    {
        Directory.CreateDirectory(rootPath);
        var path = IndexPath(rootPath);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(index, SerializerOptions));
        File.Move(tempPath, path, overwrite: true); // same atomic-rename pattern as FacadeVersionStore
    }

    public static FacadeClassificationEntry? Get(string rootPath, string key)
    {
        var index = Load(rootPath);
        return index.Facades.FirstOrDefault(f => f.Key == key);
    }

    public static void Upsert(string rootPath, FacadeClassificationEntry entry)
    {
        var index = Load(rootPath);
        var existing = index.Facades.FirstOrDefault(f => f.Key == entry.Key);
        if (existing != null)
            index.Facades.Remove(existing);
        index.Facades.Add(entry);
        Save(rootPath, index);
    }
}
