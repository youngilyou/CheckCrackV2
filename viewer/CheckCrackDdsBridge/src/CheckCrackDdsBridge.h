// Exported C API for CheckCrackViewer's C# WPF app to P/Invoke. Wraps a single AnalysisBridge
// instance -- one bridge handle per process (one CheckCrackViewer workstation = one worker_id).
#pragma once

#include "AnalysisBridge.h"

extern "C" {

#ifdef CRACKVIEWER_DDS_BRIDGE_EXPORTS
#define CRACKVIEWER_API __declspec(dllexport)
#else
#define CRACKVIEWER_API __declspec(dllimport)
#endif

// Opaque handle -- callers never touch AnalysisBridge directly.
CRACKVIEWER_API void* CrackViewerDds_Create();
CRACKVIEWER_API void CrackViewerDds_Destroy(void* handle);

CRACKVIEWER_API void CrackViewerDds_SetCallbacks(void* handle, AnalysisAssignmentCallback assignment_cb,
        AnalysisRetryCallback retry_cb, AnalysisStopCallback stop_cb, void* user_data);

// worker_id/topic_prefix/initial_peer_host/initial_peer_port/local_interface_ip: see
// AnalysisBridge::Start's own doc comment.
CRACKVIEWER_API bool CrackViewerDds_Start(void* handle, int domain_id, const char* worker_id,
        const char* topic_prefix, const char* initial_peer_host, int initial_peer_port,
        const char* local_interface_ip);
CRACKVIEWER_API void CrackViewerDds_Stop(void* handle);

CRACKVIEWER_API bool CrackViewerDds_SendHeartbeat(void* handle, uint32_t max_concurrent,
        uint32_t running_count, uint32_t queued_count);
CRACKVIEWER_API bool CrackViewerDds_SendJobAccepted(void* handle, int64_t archive_id, bool started_immediately);
CRACKVIEWER_API bool CrackViewerDds_SendJobQueued(void* handle, int64_t archive_id, uint32_t queue_position);
CRACKVIEWER_API bool CrackViewerDds_SendJobStarted(void* handle, int64_t archive_id);
CRACKVIEWER_API bool CrackViewerDds_SendStatusUpdate(void* handle, int64_t archive_id, const char* stage,
        const char* progress);
CRACKVIEWER_API bool CrackViewerDds_SendErrorNotify(void* handle, int64_t archive_id, const char* stage,
        const char* error_message);
CRACKVIEWER_API bool CrackViewerDds_SendResult(void* handle, int64_t archive_id, bool success);

}
