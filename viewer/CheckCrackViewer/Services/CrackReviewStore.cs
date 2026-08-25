using System;
using System.IO;
using System.Text.Json;
using CheckCrackViewer.Models;

namespace CheckCrackViewer.Services;

/// <summary>Owns {facade_id}_crack_review.json: one version's human review
/// decisions on top of the AI's {facade_id}_cracks.json. Same atomic
/// temp-file-then-rename write pattern as FacadeVersionStore/
/// FacadeHierarchyStore use for their own JSON files, so a crash mid-save
/// can never leave a half-written review file that a later read chokes on.</summary>
public static class CrackReviewStore
{
    private static readonly JsonSerializerOptions SerializerOptions = new() { WriteIndented = true };

    private static string PathFor(string outputDir, string facadeId) =>
        Path.Combine(outputDir, $"{facadeId}_crack_review.json");

    public static CrackReviewFile? Load(string outputDir, string facadeId)
    {
        var path = PathFor(outputDir, facadeId);
        if (!File.Exists(path))
            return null;
        try
        {
            using var stream = File.Open(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return JsonSerializer.Deserialize<CrackReviewFile>(stream);
        }
        catch (JsonException)
        {
            return null; // partially-written/corrupt -- treat as "no review yet"
        }
        catch (IOException)
        {
            return null;
        }
    }

    public static void Save(string outputDir, string facadeId, CrackReviewFile file)
    {
        Directory.CreateDirectory(outputDir);
        var path = PathFor(outputDir, facadeId);
        var tempPath = path + ".tmp";
        File.WriteAllText(tempPath, JsonSerializer.Serialize(file, SerializerOptions));
        File.Move(tempPath, path, overwrite: true);
    }
}
