"""
Tests for OCI utilities module.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
from conftest import OCIConf

from ha_script.config import HAScriptConfig
from ha_script.oci.api import (
    get_config_tag_value,
    get_config_tags,
    get_instance_ip_addresses,
    get_route_table_info,
    set_config_tag,
    update_route_table,
    create_local_net_context,
    get_oci_clients,
)


def test_set_config_tag_success(oci_conf: OCIConf) -> None:
    """Test setting a config tag on an OCI instance"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    assert set_config_tag(config, clients, "status", "online",
                          oci_conf.primary_instance_id)

    # Verify tag was set
    tags = get_config_tags(clients, oci_conf.primary_instance_id)
    assert tags["status"] == "online"


def test_set_config_tag_fails(oci_conf: OCIConf, caplog) -> None:
    """Test handling of failures when setting config tags"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id
    )

    # Mock a client that raises an exception
    mock_compute_client = MagicMock()
    mock_compute_client.get_instance.side_effect = Exception("API Error")
    clients = (mock_compute_client, oci_conf.vcn_client)

    assert not set_config_tag(config, clients, "status", "online",
                              oci_conf.primary_instance_id)
    assert len(caplog.records) >= 1


def test_get_config_tags_success(oci_conf: OCIConf) -> None:
    """Test retrieving config tags from an OCI instance"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Set multiple tags
    set_config_tag(config, clients, "tag1", "value1",
                   instance_id=oci_conf.primary_instance_id)
    set_config_tag(config, clients, "tag2", "value2",
                   instance_id=oci_conf.primary_instance_id)

    tags = get_config_tags(clients, oci_conf.primary_instance_id)
    assert tags == {"tag1": "value1", "tag2": "value2"}
    assert get_config_tag_value(clients, "tag2",
                                oci_conf.primary_instance_id) == "value2"


def test_create_local_net_context_success(oci_conf: OCIConf) -> None:
    """Test creating local network context from OCI metadata"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        internal_nic_idx=0,
        wan_nic_idx=1
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Mock get_vnics to return the VNICs
    with patch('ha_script.oci.metadata.get_vnics') as mock_get_vnics:
        mock_get_vnics.return_value = [
            {
                'vnicId': oci_conf.primary_vnic_ids[0],
                'privateIp': oci_conf.primary_ips[0]
            },
            {
                'vnicId': oci_conf.primary_vnic_ids[1],
                'privateIp': oci_conf.primary_ips[1]
            }
        ]

        ctx = create_local_net_context(config, clients)

        assert ctx.internal_nic_id == oci_conf.primary_vnic_ids[0]
        assert ctx.internal_ip == oci_conf.primary_ips[0]
        assert ctx.internal_ip_id == oci_conf.primary_private_ip_ids[0]
        assert ctx.wan_nic_id == oci_conf.primary_vnic_ids[1]
        assert ctx.wan_ip == oci_conf.primary_ips[1]
        assert ctx.wan_ip_id == oci_conf.primary_private_ip_ids[1]


def test_get_route_table_info_success(oci_conf: OCIConf) -> None:
    """Test retrieving route info."""
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    route_table_info = list(
        get_route_table_info(
            clients,
            oci_conf.protected_route_table_id,
            [oci_conf.primary_instance_id, oci_conf.secondary_instance_id],
        )
    )

    # Should return only the default route (0.0.0.0/0) via primary NGFW
    # The 192.168.0.0/24 route via "other" VNIC should not be included
    assert len(route_table_info) == 1
    assert route_table_info[0].route_dest == "0.0.0.0/0"
    assert route_table_info[0].target_ip == oci_conf.primary_ips[0]
    assert route_table_info[0].target_ip_id == \
        oci_conf.primary_private_ip_ids[0]
    assert route_table_info[0].vnic_id == oci_conf.primary_vnic_ids[0]
    assert route_table_info[0].route_table_id == \
        oci_conf.protected_route_table_id
    assert route_table_info[0].route_state == "ACTIVE"


def test_update_route_table_info_success(oci_conf: OCIConf) -> None:
    """Test route table update for a given destination"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        internal_nic_idx=0,
        wan_nic_idx=1
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Create local network context for secondary
    with patch('ha_script.oci.metadata.get_vnics') as mock_get_vnics:
        mock_get_vnics.return_value = [
            {
                'vnicId': oci_conf.secondary_vnic_ids[0],
                'privateIp': oci_conf.secondary_ips[0]
            },
            {
                'vnicId': oci_conf.secondary_vnic_ids[1],
                'privateIp': oci_conf.secondary_ips[1]
            }
        ]

        secondary_ctx = create_local_net_context(config, clients)

    # Update route to point to secondary
    assert update_route_table(
        config, clients, oci_conf.protected_route_table_id, "0.0.0.0/0",
        secondary_ctx
    )

    # Verify the route was updated
    route_table_info = list(
        get_route_table_info(
            clients,
            oci_conf.protected_route_table_id,
            [oci_conf.primary_instance_id, oci_conf.secondary_instance_id],
        )
    )

    assert len(route_table_info) == 1
    assert route_table_info[0].route_dest == "0.0.0.0/0"
    assert route_table_info[0].target_ip == oci_conf.secondary_ips[0]
    assert route_table_info[0].target_ip_id == \
        oci_conf.secondary_private_ip_ids[0]

    # Verify the 192.168.0.0/24 route via "other" VNIC has not been changed
    route_table = oci_conf.vcn_client.get_route_table(
        oci_conf.protected_route_table_id
    )
    routes = route_table['routeRules']
    other_route = next(
        r for r in routes if r['destination'] == '192.168.0.0/24'
    )
    assert other_route['networkEntityId'] == oci_conf.other_private_ip_id


def test_get_instance_ip_addresses_success(oci_conf: OCIConf) -> None:
    """Test retrieving all IP addresses from an OCI instance"""
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    ip_list = get_instance_ip_addresses(clients, oci_conf.primary_instance_id)

    assert len(ip_list) == 2
    assert oci_conf.primary_ips[0] in ip_list
    assert oci_conf.primary_ips[1] in ip_list


def test_dry_run_mode(oci_conf: OCIConf) -> None:
    """Test that dry-run mode prevents actual changes"""
    config = HAScriptConfig(
        route_table_id=oci_conf.protected_route_table_id,
        primary_instance_id=oci_conf.primary_instance_id,
        secondary_instance_id=oci_conf.secondary_instance_id,
        internal_nic_idx=0,
        wan_nic_idx=1,
        dry_run=True
    )

    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    # Create local network context for secondary
    with patch('ha_script.oci.metadata.get_vnics') as mock_get_vnics:
        mock_get_vnics.return_value = [
            {
                'vnicId': oci_conf.secondary_vnic_ids[0],
                'privateIp': oci_conf.secondary_ips[0]
            },
            {
                'vnicId': oci_conf.secondary_vnic_ids[1],
                'privateIp': oci_conf.secondary_ips[1]
            }
        ]

        secondary_ctx = create_local_net_context(config, clients)

    # Get original route target
    route_table = oci_conf.vcn_client.get_route_table(
        oci_conf.protected_route_table_id
    )
    original_default_route = next(
        r for r in route_table['routeRules'] if r['destination'] == '0.0.0.0/0'
    )
    original_target = original_default_route['networkEntityId']

    # Try to update route in dry-run mode
    assert update_route_table(
        config, clients, oci_conf.protected_route_table_id, "0.0.0.0/0",
        secondary_ctx
    )

    # Verify the route was NOT actually updated
    route_table = oci_conf.vcn_client.get_route_table(
        oci_conf.protected_route_table_id
    )
    default_route = next(
        r for r in route_table['routeRules'] if r['destination'] == '0.0.0.0/0'
    )
    assert default_route['networkEntityId'] == original_target


def test_get_route_table_info_blackhole(oci_conf: OCIConf) -> None:
    clients = (oci_conf.compute_client, oci_conf.vcn_client)

    oci_conf.state.route_tables[0]['routeRules'] = [
        {
            'destination': '0.0.0.0/0',
            'destinationType': 'CIDR_BLOCK',
            # networkEntityId intentionally absent
        }
    ]

    routes = list(
        get_route_table_info(
            clients,
            oci_conf.protected_route_table_id,
            [oci_conf.primary_instance_id, oci_conf.secondary_instance_id],
        )
    )

    assert len(routes) == 1
    assert routes[0].route_state == "blackhole"
    assert routes[0].route_dest == "0.0.0.0/0"
    assert routes[0].target_ip == ""
    assert routes[0].target_ip_id == ""
    assert routes[0].vnic_id == ""
    assert routes[0].route_table_id == oci_conf.protected_route_table_id


def test_get_oci_clients_propagates_exception_with_cause(caplog):
    original_error = RuntimeError("auth service unreachable")

    with patch("ha_script.oci.auth.RequestSigner", side_effect=original_error):
        with caplog.at_level(logging.CRITICAL, logger="ha_script.oci.api"):
            with pytest.raises(RuntimeError) as exc_info:
                get_oci_clients()

    assert exc_info.value is original_error

    critical_records = [
        r for r in caplog.records if r.levelno == logging.CRITICAL
    ]
    assert len(critical_records) == 1
    assert "auth service unreachable" in critical_records[0].message
