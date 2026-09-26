using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CheckCrackViewer.Services;

public sealed class ComplexSettingsEntry
{
    [JsonPropertyName("complex_id")] public string ComplexId { get; set; } = "";
    /// <summary>"APARTMENT" (default) / "DAM" / "FACTORY" -- forwarded to
    /// tools/stitch_folder.py's --structure-type, recorded per-facade-run as
    /// {facade_id}_structure_type.json (see src/common/structure_profiles.py).
    /// Only APARTMENT is a calibrated algorithm profile today -- see that
    /// module's docstring for why DAM/FACTORY intentionally behave
    /// identically to APARTMENT for now (2026-09-24 user decision).</summary>
    [JsonPropertyName("structure_type")] public string StructureType { get; set; } = "APARTMENT";
}

public sealed class ComplexSettingsIndex
{
    [JsonPropertyName("complexes")] public List<ComplexSettingsEntry> Complexes { get; set; } = new();
}

/// <summary>Owns RootPath/complex_settings.json: per-단지(Complex) settings that
/// don't belong on any one facade -- today just StructureType. `ComplexNode`
/// in the FACADES tree is transient (RebuildFacadeTree recreates it from
/// Facades every refresh, see FacadeTreeNodes.cs's own docs on BuildingNode),
/// so this is the durable side of that combo box, keyed by ComplexId --
/// same "own small JSON file at RootPath" convention as FacadeHierarchyStore/
/// PipelineSettingsStore.</summary>
public static class ComplexSettingsStore
{
    private const string FileName = "complex_settings.json";
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    private static string IndexPath(string rootPath) => Path.Combine(rootPath, FileName);

    public static ComplexSettingsIndex Load(string rootPath)
    {
        var path = IndexPath(rootPath);
        if (!File.Exists(path))
            return new ComplexSettingsIndex();
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<ComplexSettingsIndex>(stream) ?? new ComplexSettingsIndex();
        }
        catch (JsonException)
        {
            return new ComplexSettingsIndex();
        }
        catch (IOException)
        {
            return new ComplexSettingsIndex();
        }
    }

    private static void Save(string rootPath, ComplexSettingsIndex index)
    {
        Directory.CreateDirectory(rootPath);
        var path = IndexPath(rootPath);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(index, SerializerOptions));
        File.Move(tempPath, path, overwrite: true);
    }

    public static string GetStructureType(string rootPath, string complexId)
    {
        var index = Load(rootPath);
        return index.Complexes.FirstOrDefault(c => c.ComplexId == complexId)?.StructureType ?? "APARTMENT";
    }

    public static void SetStructureType(string rootPath, string complexId, string structureType)
    {
        var index = Load(rootPath);
        var existing = index.Complexes.FirstOrDefault(c => c.ComplexId == complexId);
        if (existing != null)
            existing.StructureType = structureType;
        else
            index.Complexes.Add(new ComplexSettingsEntry { ComplexId = complexId, StructureType = structureType });
        Save(rootPath, index);
    }
}
