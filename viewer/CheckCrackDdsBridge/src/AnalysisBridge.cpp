#include "AnalysisBridge.h"

#define WIN32_LEAN_AND_MEAN
#include <Windows.h>

#include <fastdds/dds/domain/DomainParticipant.hpp>
#include <fastdds/dds/domain/DomainParticipantFactory.hpp>
#include <fastdds/dds/publisher/DataWriter.hpp>
#include <fastdds/dds/publisher/Publisher.hpp>
#include <fastdds/dds/publisher/qos/DataWriterQos.hpp>
#include <fastdds/dds/subscriber/DataReader.hpp>
#include <fastdds/dds/subscriber/DataReaderListener.hpp>
#include <fastdds/dds/subscriber/SampleInfo.hpp>
#include <fastdds/dds/subscriber/Subscriber.hpp>
#include <fastdds/dds/subscriber/qos/DataReaderQos.hpp>
#include <fastdds/dds/topic/Topic.hpp>
#include <fastdds/dds/topic/TypeSupport.hpp>

#include "FacadeAnalysisPubSubTypes.hpp"
#include "DdsQosHelpers.h"

#include <chrono>
#include <cstdio>
#include <sstream>
#include <string>

using namespace eprosima::fastdds::dds;
using namespace facade_analysis_msgs::msg;

namespace {

int64_t NowEpochMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string JoinCsv(const std::vector<std::string>& values)
{
    std::string out;
    for (size_t i = 0; i < values.size(); ++i)
    {
        if (i > 0)
            out += ',';
        out += values[i];
    }
    return out;
}

class AssignmentListener : public DataReaderListener
{
public:
    AnalysisAssignmentCallback callback = nullptr;
    void* user_data = nullptr;
    std::string my_worker_id;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            // App-level filter: every worker technically receives every AnalysisAssignment on
            // this bridge (see FacadeAnalysis.idl's own comment on why this isn't a DDS-Router
            // content-filter route) -- only act on the one addressed to us.
            if (sample_.target_worker_id() != my_worker_id)
                continue;
            directions_csv_ = JoinCsv(sample_.directions());
            AnalysisAssignmentData out{};
            out.archive_id = sample_.archive_id();
            out.company = sample_.company().c_str();
            out.building = sample_.building().c_str();
            out.directions_csv = directions_csv_.c_str();
            out.image_count = sample_.image_count();
            out.zip_remote_path = sample_.zip_remote_path().c_str();
            out.size_bytes = sample_.size_bytes();
            out.assigned_at_epoch_ms = sample_.assigned_at_epoch_ms();
            callback(&out, user_data);
        }
    }

private:
    AnalysisAssignment sample_;
    std::string directions_csv_;
};

class RetryListener : public DataReaderListener
{
public:
    AnalysisRetryCallback callback = nullptr;
    void* user_data = nullptr;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            AnalysisControlData out{};
            out.archive_id = sample_.archive_id();
            out.requested_at_epoch_ms = sample_.requested_at_epoch_ms();
            callback(&out, user_data);
        }
    }

private:
    AnalysisRetryRequest sample_;
};

class StopListener : public DataReaderListener
{
public:
    AnalysisStopCallback callback = nullptr;
    void* user_data = nullptr;

    void on_data_available(DataReader* reader) override
    {
        SampleInfo info;
        while (RETCODE_OK == reader->take_next_sample(&sample_, &info))
        {
            if (!info.valid_data || !callback)
                continue;
            AnalysisControlData out{};
            out.archive_id = sample_.archive_id();
            out.requested_at_epoch_ms = sample_.requested_at_epoch_ms();
            callback(&out, user_data);
        }
    }

private:
    AnalysisStopRequest sample_;
};

DWORD WINAPI TeardownParticipantThread(LPVOID param)
{
    DomainParticipant* participant = (DomainParticipant*)param;
    participant->delete_contained_entities();
    DomainParticipantFactory::get_instance()->delete_participant(participant);
    return 0;
}

} // namespace

struct AnalysisBridge::Impl
{
    DomainParticipant* participant = nullptr;
    Publisher* publisher = nullptr;
    Subscriber* subscriber = nullptr;
    std::string worker_id;

    Topic* heartbeat_topic = nullptr;
    Topic* assignment_topic = nullptr;
    Topic* accepted_topic = nullptr;
    Topic* queued_topic = nullptr;
    Topic* started_topic = nullptr;
    Topic* status_topic = nullptr;
    Topic* error_topic = nullptr;
    Topic* retry_topic = nullptr;
    Topic* stop_topic = nullptr;
    Topic* result_topic = nullptr;

    DataWriter* heartbeat_writer = nullptr;
    DataWriter* accepted_writer = nullptr;
    DataWriter* queued_writer = nullptr;
    DataWriter* started_writer = nullptr;
    DataWriter* status_writer = nullptr;
    DataWriter* error_writer = nullptr;
    DataWriter* result_writer = nullptr;

    DataReader* assignment_reader = nullptr;
    DataReader* retry_reader = nullptr;
    DataReader* stop_reader = nullptr;

    TypeSupport heartbeat_type{new WorkerHeartbeatPubSubType()};
    TypeSupport assignment_type{new AnalysisAssignmentPubSubType()};
    TypeSupport accepted_type{new AnalysisJobAcceptedPubSubType()};
    TypeSupport queued_type{new AnalysisJobQueuedPubSubType()};
    TypeSupport started_type{new AnalysisJobStartedPubSubType()};
    TypeSupport status_type{new AnalysisStatusUpdatePubSubType()};
    TypeSupport error_type{new AnalysisErrorNotifyPubSubType()};
    TypeSupport retry_type{new AnalysisRetryRequestPubSubType()};
    TypeSupport stop_type{new AnalysisStopRequestPubSubType()};
    TypeSupport result_type{new AnalysisResultPubSubType()};

    AssignmentListener assignment_listener;
    RetryListener retry_listener;
    StopListener stop_listener;
};

AnalysisBridge::AnalysisBridge() : impl_(new Impl()) {}

AnalysisBridge::~AnalysisBridge()
{
    Stop();
    delete impl_;
}

void AnalysisBridge::SetCallbacks(AnalysisAssignmentCallback assignment_cb, AnalysisRetryCallback retry_cb,
        AnalysisStopCallback stop_cb, void* user_data)
{
    impl_->assignment_listener.callback = assignment_cb;
    impl_->assignment_listener.user_data = user_data;
    impl_->retry_listener.callback = retry_cb;
    impl_->retry_listener.user_data = user_data;
    impl_->stop_listener.callback = stop_cb;
    impl_->stop_listener.user_data = user_data;
}

bool AnalysisBridge::Start(int domain_id, const char* worker_id, const char* topic_prefix,
        const char* initial_peer_host, int initial_peer_port, const char* local_interface_ip)
{
    Impl* impl = impl_;
    impl->worker_id = worker_id ? worker_id : "";
    impl->assignment_listener.my_worker_id = impl->worker_id;

    std::string prefix = (topic_prefix && *topic_prefix) ? topic_prefix : "rt/facade_analysis/";

    auto factory = DomainParticipantFactory::get_instance();
    impl->participant = factory->create_participant(domain_id,
            MakeUdpOnlyQos(domain_id, initial_peer_host, initial_peer_port, local_interface_ip, "AnalysisBridge"));
    if (!impl->participant)
    {
        fprintf(stderr, "AnalysisBridge: failed to create participant (domain %d)\n", domain_id);
        return false;
    }

    impl->heartbeat_type.register_type(impl->participant);
    impl->assignment_type.register_type(impl->participant);
    impl->accepted_type.register_type(impl->participant);
    impl->queued_type.register_type(impl->participant);
    impl->started_type.register_type(impl->participant);
    impl->status_type.register_type(impl->participant);
    impl->error_type.register_type(impl->participant);
    impl->retry_type.register_type(impl->participant);
    impl->stop_type.register_type(impl->participant);
    impl->result_type.register_type(impl->participant);

    impl->publisher = impl->participant->create_publisher(PUBLISHER_QOS_DEFAULT);
    impl->subscriber = impl->participant->create_subscriber(SUBSCRIBER_QOS_DEFAULT);
    if (!impl->publisher || !impl->subscriber)
    {
        fprintf(stderr, "AnalysisBridge: failed to create publisher/subscriber\n");
        return false;
    }

    impl->heartbeat_topic = impl->participant->create_topic(prefix + "WorkerHeartbeat", impl->heartbeat_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->assignment_topic = impl->participant->create_topic(prefix + "AnalysisAssignment", impl->assignment_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->accepted_topic = impl->participant->create_topic(prefix + "AnalysisJobAccepted", impl->accepted_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->queued_topic = impl->participant->create_topic(prefix + "AnalysisJobQueued", impl->queued_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->started_topic = impl->participant->create_topic(prefix + "AnalysisJobStarted", impl->started_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->status_topic = impl->participant->create_topic(prefix + "AnalysisStatusUpdate", impl->status_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->error_topic = impl->participant->create_topic(prefix + "AnalysisErrorNotify", impl->error_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->retry_topic = impl->participant->create_topic(prefix + "AnalysisRetryRequest", impl->retry_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->stop_topic = impl->participant->create_topic(prefix + "AnalysisStopRequest", impl->stop_type.get_type_name(), TOPIC_QOS_DEFAULT);
    impl->result_topic = impl->participant->create_topic(prefix + "AnalysisResult", impl->result_type.get_type_name(), TOPIC_QOS_DEFAULT);
    if (!impl->heartbeat_topic || !impl->assignment_topic || !impl->accepted_topic || !impl->queued_topic ||
            !impl->started_topic || !impl->status_topic || !impl->error_topic || !impl->retry_topic ||
            !impl->stop_topic || !impl->result_topic)
    {
        fprintf(stderr, "AnalysisBridge: failed to create topics\n");
        return false;
    }

    // BEST_EFFORT, VOLATILE (default) -- periodic, loss of one sample is harmless (matches
    // facade_storage_msgs::FacadeStorageFeedback convention, see FacadeAnalysis.idl).
    DataWriterQos heartbeat_wqos = DATAWRITER_QOS_DEFAULT;
    heartbeat_wqos.reliability().kind = BEST_EFFORT_RELIABILITY_QOS;
    heartbeat_wqos.history().kind = KEEP_LAST_HISTORY_QOS;
    heartbeat_wqos.history().depth = 1;
    impl->heartbeat_writer = impl->publisher->create_datawriter(impl->heartbeat_topic, heartbeat_wqos);

    DataWriterQos status_wqos = DATAWRITER_QOS_DEFAULT;
    status_wqos.reliability().kind = BEST_EFFORT_RELIABILITY_QOS;
    status_wqos.history().kind = KEEP_LAST_HISTORY_QOS;
    status_wqos.history().depth = 10;
    impl->status_writer = impl->publisher->create_datawriter(impl->status_topic, status_wqos);

    // RELIABLE + TRANSIENT_LOCAL for every discrete command/result -- same defensive pattern as
    // FacadeStorageStatus's cancel/requirements/finalize writers (the operator-facing outcome
    // must not be lost even if the reader's participant hasn't finished matching yet).
    auto make_reliable_wqos = []() {
        DataWriterQos wqos = DATAWRITER_QOS_DEFAULT;
        wqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
        wqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
        wqos.history().kind = KEEP_LAST_HISTORY_QOS;
        wqos.history().depth = 20;
        return wqos;
    };
    impl->accepted_writer = impl->publisher->create_datawriter(impl->accepted_topic, make_reliable_wqos());
    impl->queued_writer = impl->publisher->create_datawriter(impl->queued_topic, make_reliable_wqos());
    impl->started_writer = impl->publisher->create_datawriter(impl->started_topic, make_reliable_wqos());
    impl->error_writer = impl->publisher->create_datawriter(impl->error_topic, make_reliable_wqos());
    impl->result_writer = impl->publisher->create_datawriter(impl->result_topic, make_reliable_wqos());

    if (!impl->heartbeat_writer || !impl->status_writer || !impl->accepted_writer || !impl->queued_writer ||
            !impl->started_writer || !impl->error_writer || !impl->result_writer)
    {
        fprintf(stderr, "AnalysisBridge: failed to create one or more writers\n");
        return false;
    }

    // RELIABLE + TRANSIENT_LOCAL readers -- matches the writer side above (AnalysisLoadBalancer/
    // FacadePreviewer are expected to use the same durability for their matching writers).
    auto make_reliable_rqos = []() {
        DataReaderQos rqos = DATAREADER_QOS_DEFAULT;
        rqos.reliability().kind = RELIABLE_RELIABILITY_QOS;
        rqos.durability().kind = TRANSIENT_LOCAL_DURABILITY_QOS;
        rqos.history().kind = KEEP_LAST_HISTORY_QOS;
        rqos.history().depth = 20;
        return rqos;
    };
    impl->assignment_reader = impl->subscriber->create_datareader(impl->assignment_topic, make_reliable_rqos(), &impl->assignment_listener);
    impl->retry_reader = impl->subscriber->create_datareader(impl->retry_topic, make_reliable_rqos(), &impl->retry_listener);
    impl->stop_reader = impl->subscriber->create_datareader(impl->stop_topic, make_reliable_rqos(), &impl->stop_listener);
    if (!impl->assignment_reader || !impl->retry_reader || !impl->stop_reader)
    {
        fprintf(stderr, "AnalysisBridge: failed to create one or more readers\n");
        return false;
    }

    printf("AnalysisBridge: worker '%s' listening on domain %d (topic prefix '%s')\n",
            impl->worker_id.c_str(), domain_id, prefix.c_str());
    return true;
}

void AnalysisBridge::Stop()
{
    if (!impl_->participant)
        return;

    // Same bounded-wait teardown as FacadeStorageStatus::Stop() -- delete_contained_entities()
    // has been observed to hang in this project family waiting on unrelated participants.
    HANDLE thread = CreateThread(NULL, 0, TeardownParticipantThread, impl_->participant, 0, NULL);
    if (thread)
    {
        const DWORD kTeardownBudgetMs = 2000;
        if (WaitForSingleObject(thread, kTeardownBudgetMs) == WAIT_TIMEOUT)
            fprintf(stderr, "AnalysisBridge: graceful teardown exceeded %ums, abandoning it\n", kTeardownBudgetMs);
        CloseHandle(thread);
    }
    impl_->participant = nullptr;
    impl_->publisher = nullptr;
    impl_->subscriber = nullptr;
    impl_->heartbeat_topic = impl_->assignment_topic = impl_->accepted_topic = impl_->queued_topic =
            impl_->started_topic = impl_->status_topic = impl_->error_topic = impl_->retry_topic =
            impl_->stop_topic = impl_->result_topic = nullptr;
    impl_->heartbeat_writer = impl_->accepted_writer = impl_->queued_writer = impl_->started_writer =
            impl_->status_writer = impl_->error_writer = impl_->result_writer = nullptr;
    impl_->assignment_reader = impl_->retry_reader = impl_->stop_reader = nullptr;
}

bool AnalysisBridge::SendHeartbeat(uint32_t max_concurrent, uint32_t running_count, uint32_t queued_count)
{
    if (!impl_->heartbeat_writer)
        return false;
    WorkerHeartbeat msg;
    msg.worker_id(impl_->worker_id);
    msg.max_concurrent(max_concurrent);
    msg.running_count(running_count);
    msg.queued_count(queued_count);
    msg.updated_at_epoch_ms(NowEpochMs());
    return impl_->heartbeat_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendJobAccepted(int64_t archive_id, bool started_immediately)
{
    if (!impl_->accepted_writer)
        return false;
    AnalysisJobAccepted msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    msg.started_immediately(started_immediately);
    return impl_->accepted_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendJobQueued(int64_t archive_id, uint32_t queue_position)
{
    if (!impl_->queued_writer)
        return false;
    AnalysisJobQueued msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    msg.queue_position(queue_position);
    return impl_->queued_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendJobStarted(int64_t archive_id)
{
    if (!impl_->started_writer)
        return false;
    AnalysisJobStarted msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    return impl_->started_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendStatusUpdate(int64_t archive_id, const char* stage, const char* progress)
{
    if (!impl_->status_writer)
        return false;
    AnalysisStatusUpdate msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    msg.stage(stage ? stage : "");
    msg.progress(progress ? progress : "");
    msg.updated_at_epoch_ms(NowEpochMs());
    return impl_->status_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendErrorNotify(int64_t archive_id, const char* stage, const char* error_message)
{
    if (!impl_->error_writer)
        return false;
    AnalysisErrorNotify msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    msg.stage(stage ? stage : "");
    msg.error_message(error_message ? error_message : "");
    msg.occurred_at_epoch_ms(NowEpochMs());
    return impl_->error_writer->write(&msg) == RETCODE_OK;
}

bool AnalysisBridge::SendResult(int64_t archive_id, bool success)
{
    if (!impl_->result_writer)
        return false;
    AnalysisResult msg;
    msg.archive_id(archive_id);
    msg.worker_id(impl_->worker_id);
    msg.success(success);
    msg.completed_at_epoch_ms(NowEpochMs());
    return impl_->result_writer->write(&msg) == RETCODE_OK;
}
