"""
Tests for moveable IP functionality in OCI environment.
"""

import logging
import pytest
from unittest.mock import Mock, patch

import requests

from conftest import OCIConf, MockVirtualNetworkClient
from ha_script.oci import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import (
    primary_main_loop_handler,
    secondary_main_loop_handler
)


@patch("ha_script.oci.metadata.get_instance_id")
@patch("ha_script.oci.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_primary_moves_ip_when_becoming_active(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_instance_id: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that primary moves the public IP to itself when becoming active
    with a moveable IP"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_instance_id.return_value = oci_conf.primary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for primary (with both WAN private IPs)
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.primary_vnic_ids[0],
        internal_ip=oci_conf.primary_ips[0],
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        public_ip_targets=[
            (oci_conf.reserved_public_ip_id,
             oci_conf.primary_private_ip_ids[1], "203.0.113.10"),
            (oci_conf.reserved_public_ip_id_2,
             oci_conf.primary_private_ip_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

    # Secondary has the traffic initially
    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            'networkEntityId': oci_conf.primary_private_ip_ids[0],
        },
    ]

    # Both public IPs assigned to secondary (each on its own private IP)
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.secondary_private_ip_ids[1],
    )
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id_2,
        oci_conf.secondary_private_ip_wan_2_id,
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First public IP moved to its explicit target
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Second public IP moved to its explicit target
    public_ip_2 = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id_2
    )
    assert public_ip_2['assignedEntityId'] == \
        oci_conf.primary_private_ip_wan_2_id

    # Verify notifications were sent for both IPs
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 2


@patch("ha_script.oci.metadata.get_instance_id")
@patch("ha_script.oci.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_secondary_moves_ip_on_takeover(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_instance_id: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that secondary moves the public IP when taking over with a moveable
    IP"""
    caplog.set_level(logging.INFO)

    primary_ip = oci_conf.primary_ips[0]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
        probe_port=12345,
        probe_ip=primary_ip
    )
    get_instance_id.return_value = oci_conf.secondary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.secondary_vnic_ids[0],
        internal_ip=oci_conf.secondary_ips[0],
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        public_ip_targets=[
            (oci_conf.reserved_public_ip_id,
             oci_conf.secondary_private_ip_ids[1], "203.0.113.10"),
            (oci_conf.reserved_public_ip_id_2,
             oci_conf.secondary_private_ip_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = secondary_net_ctx

    # Primary has the traffic but is offline
    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            'networkEntityId': oci_conf.primary_private_ip_ids[0],
        },
    ]

    # Both public IPs are assigned to primary (each on its own private IP)
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.primary_private_ip_ids[1],
    )
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id_2,
        oci_conf.primary_private_ip_wan_2_id,
    )

    get_local_status.return_value = "online"
    get_primary_status.return_value = "offline"  # Primary is offline
    tcp_probe.return_value = True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, secondary_net_ctx)

    # First public IP moved to its explicit target
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]

    # Second public IP moved to its explicit target
    public_ip_2 = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id_2
    )
    assert public_ip_2['assignedEntityId'] == \
        oci_conf.secondary_private_ip_wan_2_id

    # Verify notifications were sent for both IPs
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 2


@patch("ha_script.oci.metadata.get_instance_id")
@patch("ha_script.oci.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_no_ip_move_when_already_assigned(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_instance_id: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that IP is not moved if it's already assigned to the correct
    instance"""
    caplog.set_level(logging.INFO)

    primary_ip = oci_conf.primary_ips[0]
    primary_vnic_id = oci_conf.primary_vnic_ids[0]

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )
    get_instance_id.return_value = oci_conf.secondary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        public_ip_targets=[
            (oci_conf.reserved_public_ip_id,
             oci_conf.primary_private_ip_ids[1], "203.0.113.10"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

    # Primary has the traffic and public IP is already assigned to primary
    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            'networkEntityId': oci_conf.primary_private_ip_ids[0],
        },
    ]

    # Public IP is already assigned to primary's target
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.primary_private_ip_ids[1],
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=True,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Verify public IP was NOT moved (still assigned to primary)
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Verify no notification about IP move was sent
    ip_move_notifications = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_notifications) == 0


def test_move_public_ip_basic(oci_conf: OCIConf):
    """Test basic public IP move functionality"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Initially assigned to primary
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Move to secondary
    api.move_public_ip(config, clients,
                       oci_conf.reserved_public_ip_id,
                       oci_conf.secondary_private_ip_ids[1])

    # Verify it was moved
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]


def test_move_public_ip_with_ephemeral_conflict(oci_conf: OCIConf, caplog):
    """Test moving public IP when target already has an ephemeral public IP"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"}
    )

    # Add an ephemeral public IP to secondary
    ephemeral_ip_id = "ocid1.publicip.oc1.iad.ephemeral"
    oci_conf.state.public_ips.append({
        'id': ephemeral_ip_id,
        'compartmentId': oci_conf.state.compartment_id,
        'ipAddress': '203.0.113.20',
        'lifetime': 'EPHEMERAL',
        'assignedEntityId': oci_conf.secondary_private_ip_ids[1]
    })

    # Mock the public IP update API to raise 409 on first attempt (conflict)
    class MockConflictVCNClient(MockVirtualNetworkClient):
        called = False

        def update_public_ip(self, public_ip_id, private_ip_id):
            if not self.called:
                self.called = True
                response = requests.Response()
                response.status_code = 409
                raise requests.exceptions.HTTPError(response=response)
            return super().update_public_ip(public_ip_id, private_ip_id)

    clients = (
        oci_conf.compute_client,
        MockConflictVCNClient(oci_conf.vcn_client.state)
    )

    # Move to secondary (should handle ephemeral IP gracefully)
    api.move_public_ip(config, clients,
                       oci_conf.reserved_public_ip_id,
                       oci_conf.secondary_private_ip_ids[1])

    # Verify reserved IP was moved
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]

    # Verify ephemeral IP was deleted
    with pytest.raises(ValueError):
        oci_conf.vcn_client.get_public_ip(ephemeral_ip_id)


@patch("ha_script.oci.metadata.get_instance_id")
@patch("ha_script.oci.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
def test_partial_ip_move(
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_instance_id: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that only non-local IPs are moved when one is already local"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_instance_id.return_value = oci_conf.primary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.primary_vnic_ids[0],
        internal_ip=oci_conf.primary_ips[0],
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        public_ip_targets=[
            (oci_conf.reserved_public_ip_id,
             oci_conf.primary_private_ip_ids[1], "203.0.113.10"),
            (oci_conf.reserved_public_ip_id_2,
             oci_conf.primary_private_ip_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            'networkEntityId': oci_conf.primary_private_ip_ids[0],
        },
    ]

    # First IP already on its target, second on secondary (needs move)
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.primary_private_ip_ids[1],
    )
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id_2,
        oci_conf.secondary_private_ip_wan_2_id,
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First IP should still be on primary (no move needed)
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Second IP should now be moved to its explicit target on primary
    public_ip_2 = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id_2
    )
    assert public_ip_2['assignedEntityId'] == \
        oci_conf.primary_private_ip_wan_2_id

    # Only one notification (for the second IP that was moved)
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 1
    assert "203.0.113.11" in str(ip_move_calls[0])


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_resolves_legacy_public_ip(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that legacy key 'id' with OCID resolves correctly"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"id": oci_conf.reserved_public_ip_id},
        wan_nic_idx=1,
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    assert pub_id == oci_conf.reserved_public_ip_id
    assert target_id == oci_conf.primary_private_ip_ids[1]
    assert ip_addr == "203.0.113.10"


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_resolves_ip_triplet_primary(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that is_primary=True picks primary private IP from triplet"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"},
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    assert pub_id == oci_conf.reserved_public_ip_id  # resolved from 203.0.113.10
    assert target_id == oci_conf.primary_private_ip_ids[1]  # 10.0.12.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_resolves_ip_triplet_whitespace(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that whitespace around triplet parts is stripped before lookup"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10, 10.0.12.10, 10.0.22.10"},
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    assert pub_id == oci_conf.reserved_public_ip_id  # resolved from 203.0.113.10
    assert target_id == oci_conf.primary_private_ip_ids[1]  # 10.0.12.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_resolves_ip_triplet_secondary(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that is_primary=False picks secondary private IP from triplet"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.secondary_vnic_ids[0],
            'privateIp': oci_conf.secondary_ips[0],
        },
        {
            'vnicId': oci_conf.secondary_vnic_ids[1],
            'privateIp': oci_conf.secondary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.0.12.10,10.0.22.10"},
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=False)

    assert len(ctx.public_ip_targets) == 1
    pub_id, target_id, ip_addr = ctx.public_ip_targets[0]
    assert pub_id == oci_conf.reserved_public_ip_id  # resolved from 203.0.113.10
    assert target_id == oci_conf.secondary_private_ip_ids[1]  # 10.0.22.10
    assert ip_addr == "203.0.113.10"


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_resolves_multiple_entries(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that multiple triplets are all resolved"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 2
    # Both should resolve to the correct public IP OCIDs and primary private IPs
    pub_ids = {t[0] for t in ctx.public_ip_targets}
    assert oci_conf.reserved_public_ip_id in pub_ids
    assert oci_conf.reserved_public_ip_id_2 in pub_ids


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_rejects_unknown_private_ip(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that a private IP not on any VNIC raises an error"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "203.0.113.10,10.99.99.99,10.0.22.10"},
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    with pytest.raises(api.HAScriptError, match="not found on any VNIC"):
        api.create_local_net_context(config, clients, is_primary=True)


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_public_ip_not_found(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that a non-existent public IP address raises error during context
    creation"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={"vpn": "198.51.100.99,10.0.12.10,10.0.22.10"},
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    with pytest.raises(api.HAScriptError, match="198.51.100.99"):
        api.create_local_net_context(config, clients, is_primary=True)


@patch("ha_script.oci.metadata.get_vnics")
def test_create_context_mixed_legacy_and_triplet(
    mock_get_vnics: Mock,
    oci_conf: OCIConf,
):
    """Test that both legacy OCID and triplet entries resolve correctly
    together"""
    mock_get_vnics.return_value = [
        {
            'vnicId': oci_conf.primary_vnic_ids[0],
            'privateIp': oci_conf.primary_ips[0],
        },
        {
            'vnicId': oci_conf.primary_vnic_ids[1],
            'privateIp': oci_conf.primary_ips[1],
        },
    ]
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "id": oci_conf.reserved_public_ip_id,
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        },
        wan_nic_idx=1,
    )
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ctx = api.create_local_net_context(config, clients, is_primary=True)

    assert len(ctx.public_ip_targets) == 2
    # Legacy entry resolved by OCID
    legacy = [t for t in ctx.public_ip_targets
              if t[0] == oci_conf.reserved_public_ip_id]
    assert len(legacy) == 1
    assert legacy[0][2] == "203.0.113.10"
    # Triplet entry resolved by IP address
    triplet = [t for t in ctx.public_ip_targets
               if t[0] == oci_conf.reserved_public_ip_id_2]
    assert len(triplet) == 1
    assert triplet[0][2] == "203.0.113.11"


@patch("ha_script.oci.metadata.get_instance_id")
@patch("ha_script.oci.api.create_local_net_context")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.oci.api.send_error_to_smc")
def test_primary_continues_after_one_ip_move_fails(
    send_error_to_smc: Mock,
    send_notification_to_smc: Mock,
    tcp_probe: Mock,
    get_primary_status: Mock,
    get_local_status: Mock,
    create_local_net_context: Mock,
    get_instance_id: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that if the first IP move fails, the second is still attempted"""
    caplog.set_level(logging.INFO)

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ips={
            "vpn": "203.0.113.10,10.0.12.10,10.0.22.10",
            "web": "203.0.113.11,10.0.12.11,10.0.22.11",
        }
    )
    get_instance_id.return_value = oci_conf.primary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.primary_vnic_ids[0],
        internal_ip=oci_conf.primary_ips[0],
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        public_ip_targets=[
            (oci_conf.reserved_public_ip_id,
             oci_conf.primary_private_ip_ids[1], "203.0.113.10"),
            (oci_conf.reserved_public_ip_id_2,
             oci_conf.primary_private_ip_wan_2_id, "203.0.113.11"),
        ]
    )
    create_local_net_context.return_value = primary_net_ctx

    # Route already points to primary
    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            'networkEntityId': oci_conf.primary_private_ip_ids[0],
        },
    ]

    # Both IPs assigned to secondary (both need move)
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.secondary_private_ip_ids[1],
    )
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id_2,
        oci_conf.secondary_private_ip_wan_2_id,
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_local_active=True,
        display_info_needed=False,
    )

    # Make the VCN client's update_public_ip fail on the first public IP,
    # succeed on the second — so move_public_ip returns False then True.
    original_update = oci_conf.vcn_client.update_public_ip
    call_count = {"n": 0}

    def failing_update(public_ip_id, private_ip_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("OCI API timeout")
        return original_update(public_ip_id, private_ip_id)

    oci_conf.vcn_client.update_public_ip = failing_update

    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # First IP should NOT have moved (failed)
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]

    # Second IP SHOULD have moved (succeeded despite first failure)
    public_ip_2 = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id_2
    )
    assert public_ip_2['assignedEntityId'] == \
        oci_conf.primary_private_ip_wan_2_id

    # Error was reported for the first IP
    assert send_error_to_smc.call_count == 1
    assert "203.0.113.10" in str(send_error_to_smc.call_args)

    # Success notification only for the second IP
    ip_move_calls = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_calls) == 1
    assert "203.0.113.11" in str(ip_move_calls[0])
