using System.Diagnostics;

namespace CheckCrackViewer.Services;

/// <summary>Determines this workstation's max_concurrent value for the remote analysis worker
/// role (see https://github.com/youngilyou/AnalysisLoadBalancer README "max_concurrent 결정
/// 규칙"): GPU detected -> 5, CPU-only -> 1. Deliberately asks the SAME python environment the
/// actual pipeline runs in (<see cref="PythonEnvironment.DiscoverPythonExe"/>, the conda env
/// with torch+cuda already confirmed installed) via `torch.cuda.is_available()`, rather than
/// probing hardware (WMI/nvidia-smi) independently -- a machine can have a GPU that this
/// specific python env still can't reach (broken CUDA toolkit link), and the whole point of this
/// check is "can the pipeline actually use a GPU", not "does hardware exist".</summary>
public static class GpuDetectionService
{
    /// <summary>Runs once; the caller is expected to cache the result for the process lifetime
    /// (GPU availability isn't expected to change while the app is running, see the
    /// AnalysisLoadBalancer README's own note on this).</summary>
    public static async Task<bool> DetectCudaAvailableAsync()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = PythonEnvironment.DiscoverPythonExe(),
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add("-c");
            psi.ArgumentList.Add("import torch; print(torch.cuda.is_available())");

            using var process = new Process { StartInfo = psi };
            process.Start();
            var stdoutTask = process.StandardOutput.ReadToEndAsync();
            var stderrTask = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            var stdout = await stdoutTask;
            _ = await stderrTask;

            if (process.ExitCode != 0)
                return false;
            return stdout.Trim().Equals("True", StringComparison.OrdinalIgnoreCase);
        }
        catch
        {
            // python not found, torch not installed, etc. -- fail closed to the safe (CPU-only,
            // max_concurrent=1) assumption rather than crash the workstation's startup.
            return false;
        }
    }

    public static uint ComputeMaxConcurrent(bool gpuAvailable) => gpuAvailable ? 5u : 1u;
}
