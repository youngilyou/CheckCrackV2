#include "DdsQosHelpers.h"

#include <fastdds/rtps/transport/UDPv4TransportDescriptor.hpp>
#include <fastdds/utils/IPLocator.hpp>

#include <cstdio>
#include <cstdlib>
#include <memory>
#include <sstream>
#include <string>

using namespace eprosima::fastdds::dds;

// Own copy of FacadePreviewer's MakeUdpOnlyQos (see DdsQosHelpers.h for why this isn't shared
// code). SHM transport hangs indefinitely inside create_participant() on the previewer's dev
// machine -- disabling it here too as a precaution even though this bridge hasn't specifically
// hit that yet (same FastDDS SDK vendoring, same class of risk).
// CRACKVIEWER_DDS_INTERFACE_WHITELIST (comma-separated IPs) restricts the transport to real LAN
// adapters. CRACKVIEWER_DDS_INITIAL_PEER (single IP) makes this participant also send its own
// SPDP announcement via unicast directly to the peer, sidestepping multicast entirely --
// CheckCrackViewer workstations are expected to reach the DDS-Router host over a WAN/routed
// network the same way FacadePreviewer does, not local multicast.
DomainParticipantQos MakeUdpOnlyQos(
        int domain_id,
        const char* initial_peer_host,
        int initial_peer_port,
        const char* local_interface_ip,
        const char* log_prefix)
{
    DomainParticipantQos qos = PARTICIPANT_QOS_DEFAULT;
    qos.transport().use_builtin_transports = false;
    auto udp = std::make_shared<eprosima::fastdds::rtps::UDPv4TransportDescriptor>();

    udp->sendBufferSize = 4 * 1024 * 1024;
    udp->receiveBufferSize = 4 * 1024 * 1024;

    std::string whitelist_str = (local_interface_ip && *local_interface_ip)
            ? local_interface_ip
            : (std::getenv("CRACKVIEWER_DDS_INTERFACE_WHITELIST") ? std::getenv("CRACKVIEWER_DDS_INTERFACE_WHITELIST") : "");
    if (!whitelist_str.empty())
    {
        std::stringstream ss(whitelist_str);
        std::string ip;
        while (std::getline(ss, ip, ','))
        {
            if (!ip.empty())
            {
                udp->interfaceWhiteList.push_back(ip);
                printf("%s: restricting UDPv4 transport to interface '%s'\n", log_prefix, ip.c_str());
            }
        }
    }

    qos.transport().user_transports.push_back(udp);

    std::string peer_str = (initial_peer_host && *initial_peer_host)
            ? initial_peer_host
            : (std::getenv("CRACKVIEWER_DDS_INITIAL_PEER") ? std::getenv("CRACKVIEWER_DDS_INITIAL_PEER") : "");
    if (!peer_str.empty())
    {
        eprosima::fastdds::rtps::Locator_t peer_locator;
        peer_locator.kind = LOCATOR_KIND_UDPv4;
        peer_locator.port = initial_peer_port > 0
                ? static_cast<uint16_t>(initial_peer_port)
                : static_cast<uint16_t>(7400 + 250 * domain_id + 10);
        eprosima::fastdds::rtps::IPLocator::setIPv4(peer_locator, peer_str);
        qos.wire_protocol().builtin.initialPeersList.push_back(peer_locator);
        printf("%s: adding initial discovery peer '%s:%u'\n", log_prefix, peer_str.c_str(), peer_locator.port);
    }

    return qos;
}
