using System;
using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>{facade_id}_source_images.json keeps each photo's ABSOLUTE path from the machine that stitched.
/// On another computer those paths do not exist, so the original-photo panel and the review's 원본 보기 would
/// show nothing. This finds the same file NAME next to the run's output folder instead (mirror of
/// src/common/paths.py::resolve_source_image -- keep the two in sync). Only the name is matched; a photo
/// is never replaced by a different one.</summary>
public static class SourceImagePathResolver
{
    /// <summary>The stored path if it exists; else $CHECKCRACK_IMAGES_DIR\name; else name (or images\name) in the
    /// output folder's parents (normal layout: photos\output\Vnnn\, i.e. photos sit 2 levels up); else null.</summary>
    public static string? Resolve(string filePath, string outputDir)
    {
        if (!string.IsNullOrEmpty(filePath) && File.Exists(filePath))
            return filePath;

        var name = filePath.Replace('\\', '/');
        name = name[(name.LastIndexOf('/') + 1)..];
        if (string.IsNullOrEmpty(name))
            return null;

        var envDir = Environment.GetEnvironmentVariable("CHECKCRACK_IMAGES_DIR");
        if (!string.IsNullOrWhiteSpace(envDir))
        {
            var c = Path.Combine(envDir, name);
            if (File.Exists(c))
                return c;
        }

        var dir = new DirectoryInfo(outputDir).Parent;
        for (var i = 0; i < 3 && dir != null; i++, dir = dir.Parent)
        {
            var direct = Path.Combine(dir.FullName, name);
            if (File.Exists(direct))
                return direct;
            var inImages = Path.Combine(dir.FullName, "images", name);
            if (File.Exists(inImages))
                return inImages;
        }
        return null;
    }
}
