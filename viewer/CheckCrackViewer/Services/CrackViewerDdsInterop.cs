using System.Runtime.InteropServices;

namespace CheckCrackViewer.Services;

/// <summary>Raw P/Invoke surface for CheckCrackDdsBridge.dll (see
/// viewer/CheckCrackDdsBridge/src/CheckCrackDdsBridge.h and AnalysisBridge.h — struct field
/// order/types must match those C structs exactly). Nothing here does marshaling to managed
/// types beyond what P/Invoke does automatically; see <see cref="AnalysisBridgeService"/> for
/// the layer that's actually safe to call from WPF. Modeled on FacadePreviewer's own
/// DdsBridgeInterop.cs pattern -- independent codebase, not shared code (see
/// https://github.com/youngilyou/AnalysisLoadBalancer README "독립성 원칙").</summary>
internal static class CrackViewerDdsInterop
{
    private const string DllName = "CheckCrackDdsBridge.dll";

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct AnalysisAssignmentData
    {
        public long ArchiveId;
        public IntPtr Company; // const char*
        public IntPtr Building; // const char*
        public IntPtr DirectionsCsv; // const char*
        public uint ImageCount;
        public IntPtr ZipRemotePath; // const char*
        public ulong SizeBytes;
        public long AssignedAtEpochMs;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Ansi)]
    public struct AnalysisControlData
    {
        public long ArchiveId;
        public long RequestedAtEpochMs;
    }

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void AssignmentCallback(IntPtr assignmentPtr, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void RetryCallback(IntPtr retryPtr, IntPtr userData);

    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    public delegate void StopCallback(IntPtr stopPtr, IntPtr userData);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern IntPtr CrackViewerDds_Create();

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void CrackViewerDds_Destroy(IntPtr handle);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void CrackViewerDds_SetCallbacks(IntPtr handle, AssignmentCallback assignmentCb,
        RetryCallback retryCb, StopCallback stopCb, IntPtr userData);

    // workerId/topicPrefix: ASCII-safe (hostname, topic name) -- LPStr is fine.
    // initialPeerHost/localInterfaceIp: pass "" to fall back to
    // CRACKVIEWER_DDS_INITIAL_PEER/CRACKVIEWER_DDS_INTERFACE_WHITELIST env vars.
    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_Start(IntPtr handle, int domainId,
        [MarshalAs(UnmanagedType.LPStr)] string workerId, [MarshalAs(UnmanagedType.LPStr)] string topicPrefix,
        [MarshalAs(UnmanagedType.LPStr)] string initialPeerHost, int initialPeerPort,
        [MarshalAs(UnmanagedType.LPStr)] string localInterfaceIp);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    public static extern void CrackViewerDds_Stop(IntPtr handle);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendHeartbeat(IntPtr handle, uint maxConcurrent, uint runningCount, uint queuedCount);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendJobAccepted(IntPtr handle, long archiveId,
        [MarshalAs(UnmanagedType.I1)] bool startedImmediately);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendJobQueued(IntPtr handle, long archiveId, uint queuePosition);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendJobStarted(IntPtr handle, long archiveId);

    // stage/progress: potentially Korean-adjacent free text (progress strings from the pipeline
    // log) -- LPUTF8Str, same reasoning as FacadePreviewer's FacadeStorageStatus_SendRequirements
    // (see that file's own comment on why LPStr silently corrupts non-ASCII text).
    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendStatusUpdate(IntPtr handle, long archiveId,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string stage, [MarshalAs(UnmanagedType.LPUTF8Str)] string progress);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendErrorNotify(IntPtr handle, long archiveId,
        [MarshalAs(UnmanagedType.LPUTF8Str)] string stage, [MarshalAs(UnmanagedType.LPUTF8Str)] string errorMessage);

    [DllImport(DllName, CallingConvention = CallingConvention.Cdecl)]
    [return: MarshalAs(UnmanagedType.I1)]
    public static extern bool CrackViewerDds_SendResult(IntPtr handle, long archiveId,
        [MarshalAs(UnmanagedType.I1)] bool success);
}
