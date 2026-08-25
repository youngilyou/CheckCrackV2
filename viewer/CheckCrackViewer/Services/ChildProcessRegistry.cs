using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;

namespace CheckCrackViewer.Services;

/// <summary>Tracks every long-running python subprocess this app has started
/// (stitch_for_ai_training.py / stitch_folder.py / train_from_viewer.py) so they
/// can all be force-killed when the app shuts down.
///
/// Real bug this fixes: `Process.Dispose()` (via `using var process = ...`) only
/// releases the .NET wrapper handle -- it does NOT kill the underlying OS
/// process. If the WPF window closes while a RunFacade/PrepareImage/StartTraining
/// call is still awaiting WaitForExitAsync(), the awaited Task is simply abandoned
/// (the whole app process is exiting), but the CHILD python process keeps running
/// orphaned in the background -- silently eating GPU/CPU indefinitely and starving
/// whatever pipeline run gets started next time the app opens. Confirmed live:
/// an orphaned stitch_for_ai_training.py from an earlier session left running,
/// still actively computing 11 minutes later, starved a freshly-started run of
/// GPU/CPU badly enough to look completely stuck.</summary>
public static class ChildProcessRegistry
{
    private static readonly List<Process> Active = new();
    private static readonly object Lock = new();

    public static void Register(Process process)
    {
        lock (Lock)
            Active.Add(process);
    }

    public static void Unregister(Process process)
    {
        lock (Lock)
            Active.Remove(process);
    }

    /// <summary>Called from App.OnExit -- kills every still-running tracked
    /// process (and its own child tree, e.g. any ultralytics worker) so nothing
    /// survives this app closing.</summary>
    public static void KillAll()
    {
        List<Process> snapshot;
        lock (Lock)
            snapshot = Active.ToList();

        foreach (var process in snapshot)
        {
            try
            {
                if (!process.HasExited)
                    process.Kill(entireProcessTree: true);
            }
            catch
            {
                // best-effort -- process may have exited between the HasExited
                // check and Kill(), or already be gone; nothing more to do.
            }
        }
    }
}
