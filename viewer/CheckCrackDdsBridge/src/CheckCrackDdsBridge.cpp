#include "CheckCrackDdsBridge.h"

void* CrackViewerDds_Create()
{
    return new AnalysisBridge();
}

void CrackViewerDds_Destroy(void* handle)
{
    delete static_cast<AnalysisBridge*>(handle);
}

void CrackViewerDds_SetCallbacks(void* handle, AnalysisAssignmentCallback assignment_cb,
        AnalysisRetryCallback retry_cb, AnalysisStopCallback stop_cb, void* user_data)
{
    static_cast<AnalysisBridge*>(handle)->SetCallbacks(assignment_cb, retry_cb, stop_cb, user_data);
}

bool CrackViewerDds_Start(void* handle, int domain_id, const char* worker_id, const char* topic_prefix,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    return static_cast<AnalysisBridge*>(handle)->Start(domain_id, worker_id, topic_prefix, initial_peer_host,
            initial_peer_port, local_interface_ip);
}

void CrackViewerDds_Stop(void* handle)
{
    static_cast<AnalysisBridge*>(handle)->Stop();
}

bool CrackViewerDds_SendHeartbeat(void* handle, uint32_t max_concurrent, uint32_t running_count, uint32_t queued_count)
{
    return static_cast<AnalysisBridge*>(handle)->SendHeartbeat(max_concurrent, running_count, queued_count);
}

bool CrackViewerDds_SendJobAccepted(void* handle, int64_t archive_id, bool started_immediately)
{
    return static_cast<AnalysisBridge*>(handle)->SendJobAccepted(archive_id, started_immediately);
}

bool CrackViewerDds_SendJobQueued(void* handle, int64_t archive_id, uint32_t queue_position)
{
    return static_cast<AnalysisBridge*>(handle)->SendJobQueued(archive_id, queue_position);
}

bool CrackViewerDds_SendJobStarted(void* handle, int64_t archive_id)
{
    return static_cast<AnalysisBridge*>(handle)->SendJobStarted(archive_id);
}

bool CrackViewerDds_SendStatusUpdate(void* handle, int64_t archive_id, const char* stage, const char* progress)
{
    return static_cast<AnalysisBridge*>(handle)->SendStatusUpdate(archive_id, stage, progress);
}

bool CrackViewerDds_SendErrorNotify(void* handle, int64_t archive_id, const char* stage, const char* error_message)
{
    return static_cast<AnalysisBridge*>(handle)->SendErrorNotify(archive_id, stage, error_message);
}

bool CrackViewerDds_SendResult(void* handle, int64_t archive_id, bool success)
{
    return static_cast<AnalysisBridge*>(handle)->SendResult(archive_id, success);
}
