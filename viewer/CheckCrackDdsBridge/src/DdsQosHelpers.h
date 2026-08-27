// Own copy of FacadePreviewer/FacadeDdsBridge/src/DdsQosHelpers.h -- NOT shared/referenced
// across repos (project-wide "no shared code/libraries between codebases" rule, see
// https://github.com/youngilyou/AnalysisLoadBalancer README "독립성 원칙"). Same UDPv4-only
// transport override, same cross-machine discovery fix, distinct env var names
// (CRACKVIEWER_DDS_*, not FACADE_DDS_*) so the two processes never collide if ever run on the
// same machine during development.
#pragma once

#include <fastdds/dds/domain/qos/DomainParticipantQos.hpp>

// initial_peer_host/local_interface_ip: pass nullptr/"" to fall back to the
// CRACKVIEWER_DDS_INITIAL_PEER/CRACKVIEWER_DDS_INTERFACE_WHITELIST env vars. initial_peer_port
// <= 0 means "use the standard participant-index-0 metatraffic-unicast port formula"
// (7400 + 250*domain_id + 10).
// log_prefix: prefixes the printf lines this prints when it applies a whitelist/initial peer.
eprosima::fastdds::dds::DomainParticipantQos MakeUdpOnlyQos(
        int domain_id,
        const char* initial_peer_host,
        int initial_peer_port,
        const char* local_interface_ip,
        const char* log_prefix);
