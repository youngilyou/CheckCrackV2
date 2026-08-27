namespace CheckCrackViewer.Services;

/// <summary>Enforces this workstation's total concurrent-analysis cap across BOTH execution
/// paths -- manual local runs (the "실행" button) and remote-triggered runs (AnalysisAssignment,
/// see AnalysisBridgeService) -- since both ultimately spawn the same python pipeline process.
///
/// Before this class existed, CheckCrackViewer had NO such cap at all: MainViewModel.RunFacade
/// only guarded against double-starting the SAME facade (`if (facade.IsRunning...) return`), so
/// an operator clicking "실행" on several facades ran them all in parallel unconditionally (see
/// https://github.com/youngilyou/AnalysisLoadBalancer README's own note on this gap, confirmed
/// by reading MainViewModel.cs directly on 2026-08-27). MaxConcurrent is set from
/// GpuDetectionService (GPU detected -> 5, CPU-only -> 1).</summary>
public sealed class AnalysisConcurrencyManager
{
    private readonly object _lock = new();
    private readonly Queue<TaskCompletionSource> _queue = new();

    public uint MaxConcurrent { get; set; } = 1;
    public int RunningCount { get; private set; }
    public int QueuedCount { get { lock (_lock) return _queue.Count; } }

    /// <summary>Completes immediately if a slot is free (RunningCount is incremented before
    /// returning). Otherwise the caller is queued (FIFO) and the returned Task completes once
    /// <see cref="Release"/> hands it a slot -- await this right after marking the caller's own
    /// "busy" state (e.g. facade.IsRunning = true) so the UI shows work in progress even while
    /// queued.</summary>
    public Task WaitForSlotAsync()
    {
        lock (_lock)
        {
            if (RunningCount < MaxConcurrent)
            {
                RunningCount++;
                return Task.CompletedTask;
            }
            // RunContinuationsAsynchronously: Release() below runs inside this same lock's
            // caller context (a finally block on some other job's thread) -- without this, the
            // queued job's continuation could run synchronously on that thread, which is
            // surprising and, if that continuation ever waited on this same lock, deadlock-prone.
            var tcs = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            _queue.Enqueue(tcs);
            return tcs.Task;
        }
    }

    /// <summary>Call exactly once per job that previously completed a WaitForSlotAsync() call,
    /// when that job finishes. If another job is queued, the slot is handed to it directly
    /// (RunningCount is not decremented then re-incremented -- it never drops in between).</summary>
    public void Release()
    {
        lock (_lock)
        {
            if (_queue.Count > 0)
                _queue.Dequeue().SetResult();
            else
                RunningCount--;
        }
    }
}
