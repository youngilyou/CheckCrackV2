using System.Diagnostics;

namespace CheckCrackViewer.Services;

/// <summary>Determines this workstation's max_concurrent value for the remote analysis worker
/// role (see https://github.com/youngilyou/AnalysisLoadBalancer README "max_concurrent 결정
/// 규칙"): always 1, regardless of GPU. 2026-08-29: previously GPU detected -> 5, CPU-only -> 1
/// -- reverted to a flat 1 per an operator-confirmed real measurement ON THE CURRENT GPU (RTX
/// 4080): a single GPU-accelerated facade run already uses 95%+ of the GPU on its own, so a
/// second concurrent run on the same GPU isn't actually feasible (they'd contend for the same
/// near-saturated resource, not run in true parallel). This is a hardware-specific number, not a
/// permanent architectural limit -- the operator has confirmed a GPU swap is planned, and the
/// concurrency this returns should be revisited (measured the same way: run 1 facade, check GPU
/// utilization, see how much headroom is actually left for a 2nd) once the replacement GPU is in.
/// DetectCudaAvailableAsync() below is kept (still useful to know/show whether GPU acceleration
/// is available at all) even though its result no longer changes this value.
/// Deliberately asks the SAME python environment the
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

    public static uint ComputeMaxConcurrent(bool gpuAvailable) => 1u;
}
