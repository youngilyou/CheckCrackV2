using System;
using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>Single source of truth for which python.exe runs the pipeline scripts
/// (tools/stitch_folder.py, tools/detect_cracks_folder.py, tools/click_locator.py ...) -- shared by
/// MainViewModel's RunFacadeCommand, AiTrainingViewModel's folder-to-pipeline flow and
/// ClickLocatorClient so the interpreter choice only ever lives in one place.
///
/// The pipeline's real dependencies (torch+cuda, kornia, pycolmap with CUDA dense stereo,
/// ultralytics) live in one specific environment; a bare "python" on PATH often resolves to an env
/// without them. Order (same as scripts\_python.bat):
///   1. CHECKCRACK_PYTHON environment variable (explicit override -- use this on a new machine
///      whose env is not miniconda3 in the user profile)
///   2. miniconda3 / anaconda3 in the usual per-user and machine-wide locations
///   3. "python" on PATH</summary>
public static class PythonEnvironment
{
    public static string DiscoverPythonExe()
    {
        var overridePath = Environment.GetEnvironmentVariable("CHECKCRACK_PYTHON");
        if (!string.IsNullOrWhiteSpace(overridePath) && File.Exists(overridePath))
            return overridePath;

        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string[] candidates =
        {
            Path.Combine(profile, "miniconda3", "python.exe"),
            Path.Combine(profile, "anaconda3", "python.exe"),
            Path.Combine(localAppData, "miniconda3", "python.exe"),
            @"C:\ProgramData\miniconda3\python.exe",
        };
        foreach (var c in candidates)
            if (File.Exists(c))
                return c;
        return "python";
    }
}
