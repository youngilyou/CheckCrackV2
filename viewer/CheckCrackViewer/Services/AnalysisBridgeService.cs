using System.Runtime.InteropServices;

namespace CheckCrackViewer.Services;

/// <summary>One dispatched analysis job (see facade_analysis_msgs::msg::AnalysisAssignment).
/// Already filtered to this workstation's own worker_id by the native side (see
/// AnalysisBridge.cpp's AssignmentListener) -- every AnalysisAssignment this event fires for is
/// meant for this process.</summary>
public sealed record AnalysisAssignment(long ArchiveId, string Company, string Building,
    IReadOnlyList<string> Directions, uint ImageCount, string ZipRemotePath, ulong SizeBytes, long AssignedAtEpochMs);

/// <summary>Shared shape for AnalysisRetryRequest/AnalysisStopRequest (see FacadeAnalysis.idl --
/// both structs are identical: archive_id + requested_at_epoch_ms).</summary>
public sealed record AnalysisControlRequest(long ArchiveId, long RequestedAtEpochMs);

/// <summary>Managed wrapper around CheckCrackDdsBridge.dll's facade_analysis_msgs "worker" client
/// (see viewer/CheckCrackDdsBridge/src/AnalysisBridge.h). One instance per process -- this
/// workstation's single worker_id. Modeled on FacadePreviewer's own FacadeStorageStatusService.cs
/// pattern; independent codebase, no shared code (see AnalysisLoadBalancer README "독립성 원칙").
///
/// IMPORTANT: like FacadePreviewer's DDS services, <see cref="AssignmentReceived"/>/<see
/// cref="RetryReceived"/>/<see cref="StopReceived"/> fire on CheckCrackDdsBridge's background DDS
/// listener thread, not the WPF UI thread -- subscribers must marshal via
/// Application.Current.Dispatcher themselves.</summary>
public sealed class AnalysisBridgeService : IDisposable
{
    private readonly IntPtr _handle;
    private readonly CrackViewerDdsInterop.AssignmentCallback _assignmentCallback;
    private readonly CrackViewerDdsInterop.RetryCallback _retryCallback;
    private readonly CrackViewerDdsInterop.StopCallback _stopCallback;
    private bool _disposed;

    public event Action<AnalysisAssignment>? AssignmentReceived;
    public event Action<AnalysisControlRequest>? RetryReceived;
    public event Action<AnalysisControlRequest>? StopReceived;

    public AnalysisBridgeService()
    {
        _handle = CrackViewerDdsInterop.CrackViewerDds_Create();
        if (_handle == IntPtr.Zero)
            throw new InvalidOperationException("CrackViewerDds_Create returned null.");

        _assignmentCallback = OnAssignmentNative;
        _retryCallback = OnRetryNative;
        _stopCallback = OnStopNative;
        CrackViewerDdsInterop.CrackViewerDds_SetCallbacks(_handle, _assignmentCallback, _retryCallback, _stopCallback, IntPtr.Zero);
    }

    /// <param name="workerId">This workstation's own identity (e.g. hostname) -- stamped on every
    /// outgoing message and used to filter incoming AnalysisAssignment.</param>
    /// <param name="topicPrefix">Pass "" for the default "rt/facade_analysis/".</param>
    /// <param name="initialPeerHost">Pass "" to fall back to CRACKVIEWER_DDS_INITIAL_PEER env var.</param>
    /// <param name="localInterfaceIp">Pass "" to fall back to CRACKVIEWER_DDS_INTERFACE_WHITELIST env var.</param>
    public bool Start(int domainId, string workerId, string topicPrefix = "", string initialPeerHost = "",
        int initialPeerPort = 0, string localInterfaceIp = "")
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_Start(_handle, domainId, workerId, topicPrefix, initialPeerHost,
            initialPeerPort, localInterfaceIp);
    }

    public bool SendHeartbeat(uint maxConcurrent, uint runningCount, uint queuedCount)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendHeartbeat(_handle, maxConcurrent, runningCount, queuedCount);
    }

    public bool SendJobAccepted(long archiveId, bool startedImmediately)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendJobAccepted(_handle, archiveId, startedImmediately);
    }

    public bool SendJobQueued(long archiveId, uint queuePosition)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendJobQueued(_handle, archiveId, queuePosition);
    }

    public bool SendJobStarted(long archiveId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendJobStarted(_handle, archiveId);
    }

    /// <param name="stage">Free-form stage code (e.g. "EXTRACT_START") -- extends CheckCrackV2's
    /// existing pipeline.log stage vocabulary, see PipelineLogEntry.Stage.</param>
    /// <param name="progress">Free-form progress string (e.g. "5/20"), same shape as
    /// PipelineLogEntry.Progress.</param>
    public bool SendStatusUpdate(long archiveId, string stage, string progress)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendStatusUpdate(_handle, archiveId, stage, progress);
    }

    public bool SendErrorNotify(long archiveId, string stage, string errorMessage)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendErrorNotify(_handle, archiveId, stage, errorMessage);
    }

    public bool SendResult(long archiveId, bool success)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        return CrackViewerDdsInterop.CrackViewerDds_SendResult(_handle, archiveId, success);
    }

    // PtrToStringUTF8, not PtrToStringAnsi -- Company/Building/ZipRemotePath can be non-ASCII
    // (Korean), same reasoning as FacadePreviewer's FacadeStorageStatusService.OnFeedbackNative.
    private void OnAssignmentNative(IntPtr assignmentPtr, IntPtr userData)
    {
        if (AssignmentReceived == null || assignmentPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<CrackViewerDdsInterop.AnalysisAssignmentData>(assignmentPtr);
        var directionsCsv = Marshal.PtrToStringUTF8(native.DirectionsCsv) ?? "";
        var directions = directionsCsv.Length == 0
            ? Array.Empty<string>()
            : directionsCsv.Split(',', StringSplitOptions.RemoveEmptyEntries);
        AssignmentReceived.Invoke(new AnalysisAssignment(
            native.ArchiveId,
            Marshal.PtrToStringUTF8(native.Company) ?? "",
            Marshal.PtrToStringUTF8(native.Building) ?? "",
            directions,
            native.ImageCount,
            Marshal.PtrToStringUTF8(native.ZipRemotePath) ?? "",
            native.SizeBytes,
            native.AssignedAtEpochMs));
    }

    private void OnRetryNative(IntPtr retryPtr, IntPtr userData)
    {
        if (RetryReceived == null || retryPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<CrackViewerDdsInterop.AnalysisControlData>(retryPtr);
        RetryReceived.Invoke(new AnalysisControlRequest(native.ArchiveId, native.RequestedAtEpochMs));
    }

    private void OnStopNative(IntPtr stopPtr, IntPtr userData)
    {
        if (StopReceived == null || stopPtr == IntPtr.Zero)
            return;
        var native = Marshal.PtrToStructure<CrackViewerDdsInterop.AnalysisControlData>(stopPtr);
        StopReceived.Invoke(new AnalysisControlRequest(native.ArchiveId, native.RequestedAtEpochMs));
    }

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        CrackViewerDdsInterop.CrackViewerDds_Stop(_handle);
        CrackViewerDdsInterop.CrackViewerDds_Destroy(_handle);
        GC.SuppressFinalize(this);
    }
}
