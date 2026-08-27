// Native Fast-DDS client for facade_analysis_msgs, CheckCrackViewer's "worker" role -- see
// idl/facade_analysis_msgs/msg/FacadeAnalysis.idl's header comment and
// https://github.com/youngilyou/AnalysisLoadBalancer README for the full protocol.
//
// This class only speaks the CheckCrackViewer <-> AnalysisLoadBalancer/FacadePreviewer side
// (domain 31, see DDS-Router/config/ddsrouter/crack_inspection_analysis.yaml): it
// - publishes WorkerHeartbeat periodically (caller drives the timer, see SendHeartbeat),
// - subscribes AnalysisAssignment, filtering out anything not addressed to this worker_id
//   (app-level filter, matches the IDL's own documented convention -- every worker technically
//   receives every AnalysisAssignment on the wire, but only acts on its own),
// - publishes AnalysisJobAccepted/Queued/Started/StatusUpdate/ErrorNotify/Result,
// - subscribes AnalysisRetryRequest/AnalysisStopRequest, routed by archive_id (the caller is
//   expected to ignore anything for an archive_id it isn't currently tracking).
//
// One participant per process, one Start()/Stop() lifecycle -- same convention as
// FacadeStorageStatus in the previewer's own bridge (this is a completely independent,
// separately-vendored codebase, not a shared class).
#pragma once

#include <cstdint>

extern "C" {

// Mirrors facade_analysis_msgs::msg::AnalysisAssignment. Strings (including directions_csv)
// valid only for the duration of the callback.
struct AnalysisAssignmentData
{
    int64_t archive_id;
    const char* company;
    const char* building;
    const char* directions_csv;   // comma-joined, e.g. "FRONT,BACK,ROOF"
    uint32_t image_count;
    const char* zip_remote_path;
    uint64_t size_bytes;
    int64_t assigned_at_epoch_ms;
};

// Mirrors facade_analysis_msgs::msg::AnalysisRetryRequest / AnalysisStopRequest (identical
// shape) -- one struct, one callback type, reused for both.
struct AnalysisControlData
{
    int64_t archive_id;
    int64_t requested_at_epoch_ms;
};

using AnalysisAssignmentCallback = void(*)(const AnalysisAssignmentData* assignment, void* user_data);
using AnalysisRetryCallback = void(*)(const AnalysisControlData* retry, void* user_data);
using AnalysisStopCallback = void(*)(const AnalysisControlData* stop, void* user_data);

} // extern "C"

class AnalysisBridge
{
public:
    AnalysisBridge();
    ~AnalysisBridge();

    AnalysisBridge(const AnalysisBridge&) = delete;
    AnalysisBridge& operator=(const AnalysisBridge&) = delete;

    void SetCallbacks(AnalysisAssignmentCallback assignment_cb, AnalysisRetryCallback retry_cb,
            AnalysisStopCallback stop_cb, void* user_data);

    // worker_id: this workstation's own identity (e.g. hostname) -- stored and used both to
    // stamp outgoing messages and to filter incoming AnalysisAssignment by target_worker_id.
    // topic_prefix: pass nullptr/"" for the default "rt/facade_analysis/" (matches the IDL's
    // documented naming convention) -- topic names are this prefix + the IDL struct name
    // (e.g. "rt/facade_analysis/WorkerHeartbeat").
    // initial_peer_host/initial_peer_port/local_interface_ip: same discovery-override
    // convention as FacadePreviewer's DdsFrameSubscriber::Start -- pass nullptr/""/<=0 to fall
    // back to CRACKVIEWER_DDS_INITIAL_PEER/CRACKVIEWER_DDS_INTERFACE_WHITELIST.
    bool Start(int domain_id, const char* worker_id, const char* topic_prefix,
            const char* initial_peer_host = nullptr, int initial_peer_port = 0,
            const char* local_interface_ip = nullptr);
    void Stop();

    // BEST_EFFORT, fire-and-forget -- caller (C# side) is expected to call this on its own
    // timer (5s, see AnalysisLoadBalancer README). max_concurrent: GPU detected -> 5,
    // CPU-only -> 1 (decided by the caller, this class has no GPU-detection logic of its own).
    bool SendHeartbeat(uint32_t max_concurrent, uint32_t running_count, uint32_t queued_count);

    bool SendJobAccepted(int64_t archive_id, bool started_immediately);
    bool SendJobQueued(int64_t archive_id, uint32_t queue_position);
    bool SendJobStarted(int64_t archive_id);
    // stage/progress: free-form strings, matching PipelineLogEntry's existing Stage/Progress
    // shape (see FacadeAnalysis.idl's own comment on AnalysisStatusUpdate) -- e.g.
    // stage="EXTRACT_START", progress="5/20".
    bool SendStatusUpdate(int64_t archive_id, const char* stage, const char* progress);
    bool SendErrorNotify(int64_t archive_id, const char* stage, const char* error_message);
    bool SendResult(int64_t archive_id, bool success);

private:
    struct Impl;
    Impl* impl_;
};
