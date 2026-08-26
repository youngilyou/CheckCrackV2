using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading.Tasks;

namespace CheckCrackViewer.Services;

/// <summary>One option in the "보기 모드" selector (원본/원본AI 화면, AI 학습 화면).</summary>
public sealed record ViewModeOption(string Value, string Label);

/// <summary>Runs tools/identify_view.py -- a human-identification-aid view (2026-08-26
/// UI request): when crack color blends into the wall, let the reviewer flip the
/// displayed photo to a shadow-corrected/binarized/skeletonized/edge view to help
/// decide "is this a crack or not". This is display-only; it never produces a
/// crack polygon, and the final marked crack region always stays on the ORIGINAL
/// image's pixel coordinates -- callers only ever swap which bitmap is shown,
/// never the overlay coordinate space.</summary>
public static class IdentifyViewClient
{
    public static IReadOnlyList<ViewModeOption> Options { get; } = new List<ViewModeOption>
    {
        new("original", "원본"),
        new("shadow", "그림자 보정"),
        new("binarize", "이진화"),
        new("skeleton", "스켈레톤"),
        new("edges", "윤곽선"),
    };

    /// <summary>Cache location for a given (image, mode) pair -- keyed by mode so
    /// switching back and forth between views never re-runs Python once generated.</summary>
    public static string CachePath(string imagePath, string mode)
    {
        var dir = Path.GetDirectoryName(imagePath) ?? ".";
        var stem = Path.GetFileNameWithoutExtension(imagePath);
        return Path.Combine(dir, "output", "identify_view", mode, $"{stem}.png");
    }

    /// <summary>Returns the (existing-or-freshly-generated) file path to display for
    /// this image+mode, or an error message if generation failed. "original" always
    /// resolves to imagePath itself, no subprocess involved.</summary>
    public static async Task<(string? Path, string? Error)> GetOrBuildAsync(string rootPath, string imagePath, string mode)
    {
        if (mode == "original")
            return (imagePath, null);

        var cachePath = CachePath(imagePath, mode);
        if (File.Exists(cachePath))
            return (cachePath, null);

        try
        {
            var scriptPath = Path.Combine(rootPath, "tools", "identify_view.py");
            var psi = new ProcessStartInfo
            {
                FileName = PythonEnvironment.DiscoverPythonExe(),
                WorkingDirectory = rootPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add(scriptPath);
            psi.ArgumentList.Add(imagePath);
            psi.ArgumentList.Add("--mode");
            psi.ArgumentList.Add(mode);
            psi.ArgumentList.Add("--out");
            psi.ArgumentList.Add(cachePath);

            using var process = new Process { StartInfo = psi };
            process.Start();
            ChildProcessRegistry.Register(process);
            try
            {
                var stderrTask = process.StandardError.ReadToEndAsync();
                var stdoutTask = process.StandardOutput.ReadToEndAsync();
                await process.WaitForExitAsync();

                if (process.ExitCode != 0)
                {
                    var stderr = await stderrTask;
                    var firstLine = stderr.Split('\n').FirstOrDefault(l => !string.IsNullOrWhiteSpace(l)) ?? "(no stderr output)";
                    return (null, firstLine.Trim());
                }
            }
            finally
            {
                ChildProcessRegistry.Unregister(process);
            }
            return (cachePath, null);
        }
        catch (System.Exception ex)
        {
            return (null, ex.Message);
        }
    }
}
