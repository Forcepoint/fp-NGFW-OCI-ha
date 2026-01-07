"""
Tests for secondary mainloop in OCI environment.

This module tests the secondary engine's main loop logic including:
- Taking over when primary is offline
- Taking over when probe fails
- Taking over when route is in blackhole state
- Respecting moveable IP functionality
"""
import logging

import pytest
from unittest.mock import Mock, patch

from conftest import OCIConf

from ha_script.oci import api
from ha_script.config import HAScriptConfig
from ha_script.context import HAScriptContext
from ha_script.mainloop import secondary_main_loop_handler


@pytest.mark.parametrize(
    "takeover_reason", ["probe_fails", "prim_offline", "route_blackhole"]
)
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.oci.api.update_route_table")
@patch("ha_script.oci.api.get_route_table_info")
@patch("ha_script.oci.api.create_local_net_context")
def test_secondary_takeover(
    create_local_net_context,
    get_route_table_info,
    update_route_table,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    oci_conf: OCIConf,
    caplog,
    takeover_reason,
):
    """Test secondary takeover for different reasons: probe fails, primary
    offline, or route blackhole"""
    caplog.set_level(logging.INFO)

    secondary_ip = oci_conf.secondary_ips[0]
    secondary_vnic_id = oci_conf.secondary_vnic_ids[0]
    primary_ip = oci_conf.primary_ips[0]
    route_table_id = oci_conf.protected_route_table_id

    config = HAScriptConfig(
        route_table_id=route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        probe_port=12345,
        probe_ip=primary_ip
    )

    # For now the primary has the traffic
    route_state = "blackhole" if takeover_reason == "route_blackhole" else "ACTIVE"
    get_route_table_info.return_value = [
        api.RouteInfo(
            route_state,
            "0.0.0.0/0",
            oci_conf.primary_private_ip_ids[0],
            primary_ip,
            oci_conf.primary_vnic_ids[0],
            route_table_id
        )
    ]
    get_local_status.return_value = "online"
    get_primary_status.return_value = "offline" if takeover_reason == "prim_offline" else "online"
    tcp_probe.return_value = False if takeover_reason == "probe_fails" else True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    tcp_probe.assert_called_once_with(config, [primary_ip], config.probe_port,
                                      ctx)

    update_route_table.assert_called_once_with(
        config, clients, route_table_id, "0.0.0.0/0", local_net_ctx
    )

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{route_table_id}' changed route to '0.0.0.0/0' "
        f"via secondary '{secondary_ip}'.",
        alert=True)


@pytest.mark.parametrize("takeover_reason", ["probe_fails", "prim_offline"])
@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.oci.api.get_route_table_info", wraps=api.get_route_table_info)
@patch("ha_script.oci.api.create_local_net_context")
def test_secondary_takeover_with_oci_mock(
    create_local_net_context,
    get_route_table_info: Mock,
    get_local_status: Mock,
    get_primary_status: Mock,
    tcp_probe: Mock,
    send_notification_to_smc: Mock,
    oci_conf: OCIConf,
    caplog,
    takeover_reason,
):
    """Same test using OCI mock. Verifies that only routes via NGFW are modified"""
    caplog.set_level(logging.INFO)

    primary_vnic_id = oci_conf.primary_vnic_ids[0]
    secondary_vnic_id = oci_conf.secondary_vnic_ids[0]
    secondary_ip = oci_conf.secondary_ips[0]

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        probe_port=12345,
    )

    # Make sure default route goes initially via the primary
    route_table = oci_conf.vcn_client.get_route_table(oci_conf.protected_route_table_id)
    default_route = next(r for r in route_table['routeRules'] if r['destination'] == '0.0.0.0/0')
    assert default_route['networkEntityId'] == oci_conf.primary_private_ip_ids[0]

    # Make sure 'other_route' goes via other VNIC
    other_route = next(r for r in route_table['routeRules'] if r['destination'] == '192.168.0.0/24')
    assert other_route['networkEntityId'] == oci_conf.other_private_ip_id

    get_local_status.return_value = "online"
    tcp_probe.return_value = True

    if takeover_reason == "prim_offline":
        get_primary_status.return_value = "offline"
    elif takeover_reason == "probe_fails":
        tcp_probe.return_value = False

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure the default route now points to secondary
    route_table = oci_conf.vcn_client.get_route_table(oci_conf.protected_route_table_id)
    default_route = next(r for r in route_table['routeRules'] if r['destination'] == '0.0.0.0/0')
    assert default_route['networkEntityId'] == oci_conf.secondary_private_ip_ids[0]

    # Make sure 'other_route' still goes via other VNIC
    other_route = next(r for r in route_table['routeRules'] if r['destination'] == '192.168.0.0/24')
    assert other_route['networkEntityId'] == oci_conf.other_private_ip_id

    # Make sure the SMC is notified
    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{oci_conf.protected_route_table_id}' changed route to '0.0.0.0/0' "
        f"via secondary '{secondary_ip}'.",
        alert=True)


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.oci.api.get_route_table_info")
@patch("ha_script.oci.api.create_local_net_context")
def test_secondary_no_takeover_when_primary_online(
    create_local_net_context,
    get_route_table_info,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc: Mock,
    oci_conf: OCIConf,
    caplog,
):
    """Test that secondary does not take over when primary is healthy"""
    caplog.set_level(logging.INFO)

    secondary_ip = oci_conf.secondary_ips[0]
    secondary_vnic_id = oci_conf.secondary_vnic_ids[0]
    primary_ip = oci_conf.primary_ips[0]

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        probe_port=12345,
        probe_ip=primary_ip
    )

    # Primary has the traffic and is healthy
    get_route_table_info.return_value = [
        api.RouteInfo(
            "ACTIVE",
            "0.0.0.0/0",
            oci_conf.primary_private_ip_ids[0],
            primary_ip,
            oci_conf.primary_vnic_ids[0],
            oci_conf.protected_route_table_id
        )
    ]
    get_local_status.return_value = "online"
    get_primary_status.return_value = "online"
    tcp_probe.return_value = True  # Primary responds to probe

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock local network context for secondary
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1]
    )
    create_local_net_context.return_value = local_net_ctx

    # --- ACTUAL TEST ---
    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Make sure no takeover happened
    assert len(send_notification_to_smc.mock_calls) == 0

    # Context should reflect that secondary is still not active
    assert not ctx.prev_local_active


@patch("ha_script.mainloop.send_notification_to_smc")
@patch("ha_script.mainloop.tcp_probe")
@patch("ha_script.mainloop.get_primary_status")
@patch("ha_script.mainloop.get_local_status")
@patch("ha_script.oci.api.get_route_table_info", wraps=api.get_route_table_info)
@patch("ha_script.oci.api.create_local_net_context")
def test_secondary_takeover_on_blackhole_route_with_oci_mock(
    create_local_net_context,
    get_route_table_info,
    get_local_status,
    get_primary_status,
    tcp_probe,
    send_notification_to_smc,
    oci_conf: OCIConf,
    caplog,
):
    caplog.set_level(logging.INFO)

    secondary_ip = oci_conf.secondary_ips[0]
    secondary_vnic_id = oci_conf.secondary_vnic_ids[0]

    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
    )

    # Replace default route with a blackhole (empty networkEntityId)
    oci_conf.state.route_tables[0]['routeRules'] = [
        {'destination': '0.0.0.0/0', 'destinationType': 'CIDR_BLOCK'},
    ]

    get_local_status.return_value = "online"
    get_primary_status.return_value = "online"
    tcp_probe.return_value = True

    ctx = HAScriptContext(
        prev_local_status="online",
        prev_primary_status="online",
        prev_local_active=False,
        display_info_needed=False,
    )
    local_net_ctx = api.LocalNetContext(
        internal_nic_id=secondary_vnic_id,
        internal_ip=secondary_ip,
        internal_ip_id=oci_conf.secondary_private_ip_ids[0],
        wan_nic_id=oci_conf.secondary_vnic_ids[1],
        wan_ip=oci_conf.secondary_ips[1],
        wan_ip_id=oci_conf.secondary_private_ip_ids[1],
    )
    create_local_net_context.return_value = local_net_ctx
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    secondary_main_loop_handler(config, clients, ctx, local_net_ctx)

    # Route must now point to secondary
    route_table = oci_conf.vcn_client.get_route_table(
        oci_conf.protected_route_table_id
    )
    default_route = next(
        r for r in route_table['routeRules'] if r['destination'] == '0.0.0.0/0'
    )
    assert default_route['networkEntityId'] == \
        oci_conf.secondary_private_ip_ids[0]

    send_notification_to_smc.assert_called_once_with(
        config,
        f"Route table '{oci_conf.protected_route_table_id}' changed route "
        f"to '0.0.0.0/0' via secondary '{secondary_ip}'.",
        alert=True,
    )
