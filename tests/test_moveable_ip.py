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
        reserved_public_ip_id=oci_conf.reserved_public_ip_id
    )
    get_instance_id.return_value = oci_conf.primary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.primary_vnic_ids[0],
        internal_ip=oci_conf.primary_ips[0],
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        wan_nic_id=oci_conf.primary_vnic_ids[1],
        wan_ip=oci_conf.primary_ips[1],
        wan_ip_id=oci_conf.primary_private_ip_ids[1]
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

    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.secondary_private_ip_ids[1],
    )

    get_local_status.return_value = "online"

    ctx = HAScriptContext(
        prev_local_status="offline",
        prev_local_active=False,
        display_info_needed=False,
    )

    # --- ACTUAL TEST ---
    primary_main_loop_handler(config, clients, ctx, primary_net_ctx)

    # Verify public IP was moved to primary WAN interface
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Verify notification was sent
    assert any(
        "Public IP address" in str(call) and "moved" in str(call)
        for call in send_notification_to_smc.mock_calls
    )


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
        reserved_public_ip_id=oci_conf.reserved_public_ip_id,
        probe_port=12345,
        probe_ip=primary_ip
    )
    get_instance_id.return_value = oci_conf.secondary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.secondary_vnic_ids[0],
        internal_ip=oci_conf.secondary_ips[0],
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
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

    # Public IP is assigned to primary
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.primary_private_ip_ids[1],
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

    # Verify public IP was moved to secondary WAN interface
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]

    # Verify notification was sent
    assert any(
        "Public IP address" in str(call) and "moved" in str(call)
        for call in send_notification_to_smc.mock_calls
    )


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
        reserved_public_ip_id=oci_conf.reserved_public_ip_id
    )
    get_instance_id.return_value = oci_conf.secondary_instance_id

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for primary
    primary_net_ctx = api.LocalNetContext(
        internal_nic_id=primary_vnic_id,
        internal_ip=primary_ip,
        internal_ip_id=oci_conf.primary_private_ip_ids[0],
        wan_nic_id=oci_conf.primary_vnic_ids[1],
        wan_ip=oci_conf.primary_ips[1],
        wan_ip_id=oci_conf.primary_private_ip_ids[1]
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

    # Public IP is already assigned to primary
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    public_ip['assignedEntityId'] = oci_conf.primary_private_ip_ids[1]
    original_assignee = public_ip['assignedEntityId']

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
    assert public_ip['assignedEntityId'] == original_assignee

    # Verify no notification about IP move was sent
    ip_move_notifications = [
        call for call in send_notification_to_smc.mock_calls
        if "Public IP address" in str(call) and "moved" in str(call)
    ]
    assert len(ip_move_notifications) == 0


def test_resolve_public_ip_assignee(oci_conf: OCIConf):
    """Test resolving private IP to public IP"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ip_id=oci_conf.reserved_public_ip_id
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Initially assigned to primary
    public_ip, assignee = api.resolve_public_ip(config, clients)
    assert assignee == oci_conf.primary_private_ip_ids[1]

    # Move to secondary
    oci_conf.vcn_client.update_public_ip(
        oci_conf.reserved_public_ip_id,
        oci_conf.secondary_private_ip_ids[1]
    )

    public_ip, assignee = api.resolve_public_ip(config, clients)
    assert assignee == oci_conf.secondary_private_ip_ids[1]


def test_move_public_ip_basic(oci_conf: OCIConf):
    """Test basic public IP move functionality"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        reserved_public_ip_id=oci_conf.reserved_public_ip_id
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Create network context for secondary
    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.secondary_vnic_ids[0],
        internal_ip=oci_conf.secondary_ips[0],
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
    )

    # Initially assigned to primary
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == oci_conf.primary_private_ip_ids[1]

    # Move to secondary
    api.move_public_ip(config, clients, secondary_net_ctx)

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
        reserved_public_ip_id=oci_conf.reserved_public_ip_id
    )

    # Create network context for secondary
    secondary_net_ctx = api.LocalNetContext(
        internal_nic_id=oci_conf.secondary_vnic_ids[0],
        internal_ip=oci_conf.secondary_ips[0],
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
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
    api.move_public_ip(config, clients, secondary_net_ctx)

    # Verify reserved IP was moved
    public_ip = oci_conf.vcn_client.get_public_ip(
        oci_conf.reserved_public_ip_id
    )
    assert public_ip['assignedEntityId'] == \
        oci_conf.secondary_private_ip_ids[1]

    # Verify ephemeral IP was deleted
    with pytest.raises(ValueError):
        oci_conf.vcn_client.get_public_ip(ephemeral_ip_id)
