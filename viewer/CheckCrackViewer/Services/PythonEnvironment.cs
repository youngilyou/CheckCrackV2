using System.IO;

namespace CheckCrackViewer.Services;

/// <summary>Single source of truth for which python.exe runs the pipeline scripts
/// (tools/stitch_folder.py etc.) -- shared by MainViewModel's RunFacadeCommand and
/// AiTrainingViewModel's folder-to-pipeline flow so the machine-specific conda path
/// only ever lives in one place.</summary>
public static class PythonEnvironment
{
    public static string DiscoverPythonExe()
    {
        // This viewer and its Python pipeline currently only run on this one
        // dev machine (same assumption MainViewModel.DiscoverDefaultRoot already
        // makes) -- the pipeline's real dependencies (torch+cuda, kornia,
        // pycolmap, ultralytics) live in this conda env, confirmed by hand; a
        // bare "python" on PATH here resolves to a bare env without them.
        string[] candidates =
        {
            @"C:\Users\KAC-USER\miniconda3\python.exe",
        };
        foreach (var c in candidates)
            if (File.Exists(c))
                return c;
        return "python";
    }
}
