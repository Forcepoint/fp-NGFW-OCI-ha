"""
Conftest for OCI-based tests.

This module provides mock OCI infrastructure for testing without
requiring an actual OCI connection. It mocks the ComputeClient and
VirtualNetworkClient classes from oci_utils.py.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

import pytest


class MockComputeClient:
    """Mock OCI Compute Client"""

    def __init__(self, state: 'OCIState'):
        self.state = state

    def get_instance(self, instance_id: str) -> Dict:
        """Get instance details"""
        for instance in self.state.instances:
            if instance['id'] == instance_id:
                return instance.copy()
        raise ValueError(f"Instance {instance_id} not found")

    def update_instance(self, instance_id: str, update_details: Dict) -> Dict:
        """Update instance details (mainly tags)"""
        for instance in self.state.instances:
            if instance['id'] == instance_id:
                if 'freeformTags' in update_details:
                    instance['freeformTags'].update(
                        update_details['freeformTags']
                    )
                return instance.copy()
        raise ValueError(f"Instance {instance_id} not found")

    def list_vnic_attachments(
        self,
        compartment_id: str,
        instance_id: Optional[str] = None,
        vnic_id: Optional[str] = None
    ) -> List[Dict]:
        """List VNIC attachments"""
        result = []
        for attachment in self.state.vnic_attachments:
            if attachment['compartmentId'] != compartment_id:
                continue
            if instance_id and attachment['instanceId'] != instance_id:
                continue
            if vnic_id and attachment['vnicId'] != vnic_id:
                continue
            result.append(attachment.copy())
        return result


class MockVirtualNetworkClient:
    """Mock OCI Virtual Network Client"""

    def __init__(self, state: 'OCIState'):
        self.state = state

    def get_vnic(self, vnic_id: str) -> Dict:
        """Get VNIC details"""
        for vnic in self.state.vnics:
            if vnic['id'] == vnic_id:
                return vnic.copy()
        raise ValueError(f"VNIC {vnic_id} not found")

    def get_private_ip(self, private_ip_id: str) -> Dict:
        """Get private IP details"""
        for private_ip in self.state.private_ips:
            if private_ip['id'] == private_ip_id:
                return private_ip.copy()
        raise ValueError(f"Private IP {private_ip_id} not found")

    def list_private_ips(self, vnic_id: str) -> List[Dict]:
        """List private IPs assigned to a VNIC"""
        result = []
        for private_ip in self.state.private_ips:
            if private_ip['vnicId'] == vnic_id:
                result.append(private_ip.copy())
        return result

    def get_route_table(self, route_table_id: str) -> Dict:
        """Get route table details"""
        for route_table in self.state.route_tables:
            if route_table['id'] == route_table_id:
                return route_table.copy()
        raise ValueError(f"Route table {route_table_id} not found")

    def update_route_table(self, route_table_id: str, details: Dict) -> Dict:
        """Update route table"""
        for route_table in self.state.route_tables:
            if route_table['id'] == route_table_id:
                if 'routeRules' in details:
                    route_table['routeRules'] = details['routeRules']
                return route_table.copy()
        raise ValueError(f"Route table {route_table_id} not found")

    def get_public_ip(self, public_ip_id: str) -> Dict:
        """Get public IP details"""
        for public_ip in self.state.public_ips:
            if public_ip['id'] == public_ip_id:
                return public_ip.copy()
        raise ValueError(f"Public IP {public_ip_id} not found")

    def get_public_ip_by_private_ip_id(self, private_ip_id: str) -> Dict:
        """Get public IP from private IP address"""
        for public_ip in self.state.public_ips:
            if public_ip.get('assignedEntityId') == private_ip_id:
                return public_ip.copy()
        raise ValueError(
            f"No public IP assigned to private IP {private_ip_id}"
        )

    def update_public_ip(self, public_ip_id: str, private_ip_id: str) -> Dict:
        """Update public IP assignment"""
        for public_ip in self.state.public_ips:
            if public_ip['id'] == public_ip_id:
                public_ip['assignedEntityId'] = private_ip_id
                return public_ip.copy()
        raise ValueError(f"Public IP {public_ip_id} not found")

    def delete_public_ip(self, public_ip_id: str) -> None:
        """Delete public IP"""
        for i, public_ip in enumerate(self.state.public_ips):
            if public_ip['id'] == public_ip_id:
                del self.state.public_ips[i]
                return
        raise ValueError(f"Public IP {public_ip_id} not found")


class OCIState:
    """Holds the mocked OCI state"""

    def __init__(self):
        self.instances: List[Dict] = []
        self.vnics: List[Dict] = []
        self.private_ips: List[Dict] = []
        self.vnic_attachments: List[Dict] = []
        self.route_tables: List[Dict] = []
        self.public_ips: List[Dict] = []
        self.compartment_id: str = "ocid1.compartment.oc1..aaaaaaaa"
        self.vcn_id: str = "ocid1.vcn.oc1.iad.aaaaaaaa"


@dataclass
class OCIConf:
    """OCI configuration for tests"""
    compute_client: MockComputeClient
    vcn_client: MockVirtualNetworkClient
    state: OCIState
    primary_instance_id: str
    secondary_instance_id: str
    protected_route_table_id: str
    primary_vnic_ids: List[str]
    secondary_vnic_ids: List[str]
    primary_private_ip_ids: List[str]
    secondary_private_ip_ids: List[str]
    primary_ips: List[str]
    secondary_ips: List[str]
    other_vnic_id: str
    other_private_ip_id: str
    reserved_public_ip_id: str


@pytest.fixture
def oci_conf() -> OCIConf:
    """Create a mock OCI environment for testing"""
    state = OCIState()

    compute_client = MockComputeClient(state)
    vcn_client = MockVirtualNetworkClient(state)

    # Create instances
    primary_instance_id = "ocid1.instance.oc1.iad.primary"
    secondary_instance_id = "ocid1.instance.oc1.iad.secondary"

    state.instances = [
        {
            'id': primary_instance_id,
            'compartmentId': state.compartment_id,
            'displayName': 'primary-ngfw',
            'lifecycleState': 'RUNNING',
            'freeformTags': {}
        },
        {
            'id': secondary_instance_id,
            'compartmentId': state.compartment_id,
            'displayName': 'secondary-ngfw',
            'lifecycleState': 'RUNNING',
            'freeformTags': {}
        }
    ]

    # Create VNICs for primary instance (2 VNICs: internal and WAN)
    primary_vnic_internal_id = "ocid1.vnic.oc1.iad.primary_internal"
    primary_vnic_wan_id = "ocid1.vnic.oc1.iad.primary_wan"

    # Create VNICs for secondary instance (2 VNICs: internal and WAN)
    secondary_vnic_internal_id = "ocid1.vnic.oc1.iad.secondary_internal"
    secondary_vnic_wan_id = "ocid1.vnic.oc1.iad.secondary_wan"

    # Create an extra VNIC not connected to NGFW
    other_vnic_id = "ocid1.vnic.oc1.iad.other"

    state.vnics = [
        {
            'id': primary_vnic_internal_id,
            'compartmentId': state.compartment_id,
            'privateIp': '10.0.11.10',
            'subnetId': 'ocid1.subnet.oc1.iad.subnet11',
            'lifecycleState': 'AVAILABLE'
        },
        {
            'id': primary_vnic_wan_id,
            'compartmentId': state.compartment_id,
            'privateIp': '10.0.12.10',
            'subnetId': 'ocid1.subnet.oc1.iad.subnet12',
            'lifecycleState': 'AVAILABLE'
        },
        {
            'id': secondary_vnic_internal_id,
            'compartmentId': state.compartment_id,
            'privateIp': '10.0.21.10',
            'subnetId': 'ocid1.subnet.oc1.iad.subnet21',
            'lifecycleState': 'AVAILABLE'
        },
        {
            'id': secondary_vnic_wan_id,
            'compartmentId': state.compartment_id,
            'privateIp': '10.0.22.10',
            'subnetId': 'ocid1.subnet.oc1.iad.subnet22',
            'lifecycleState': 'AVAILABLE'
        },
        {
            'id': other_vnic_id,
            'compartmentId': state.compartment_id,
            'privateIp': '10.0.1.50',
            'subnetId': 'ocid1.subnet.oc1.iad.subnet1',
            'lifecycleState': 'AVAILABLE'
        }
    ]

    # Create private IPs
    primary_private_ip_internal_id = "ocid1.privateip.oc1.iad.primary_internal"
    primary_private_ip_wan_id = "ocid1.privateip.oc1.iad.primary_wan"
    secondary_private_ip_internal_id = "ocid1.privateip.oc1.iad.secondary_internal"
    secondary_private_ip_wan_id = "ocid1.privateip.oc1.iad.secondary_wan"
    other_private_ip_id = "ocid1.privateip.oc1.iad.other"

    state.private_ips = [
        {
            'id': primary_private_ip_internal_id,
            'vnicId': primary_vnic_internal_id,
            'ipAddress': '10.0.11.10',
            'isPrimary': True
        },
        {
            'id': primary_private_ip_wan_id,
            'vnicId': primary_vnic_wan_id,
            'ipAddress': '10.0.12.10',
            'isPrimary': True
        },
        {
            'id': secondary_private_ip_internal_id,
            'vnicId': secondary_vnic_internal_id,
            'ipAddress': '10.0.21.10',
            'isPrimary': True
        },
        {
            'id': secondary_private_ip_wan_id,
            'vnicId': secondary_vnic_wan_id,
            'ipAddress': '10.0.22.10',
            'isPrimary': True
        },
        {
            'id': other_private_ip_id,
            'vnicId': other_vnic_id,
            'ipAddress': '10.0.1.50',
            'isPrimary': True
        }
    ]

    # Create VNIC attachments
    state.vnic_attachments = [
        {
            'id': 'ocid1.vnicattachment.primary.0',
            'compartmentId': state.compartment_id,
            'instanceId': primary_instance_id,
            'vnicId': primary_vnic_internal_id,
            'nicIndex': 0,
            'lifecycleState': 'ATTACHED'
        },
        {
            'id': 'ocid1.vnicattachment.primary.1',
            'compartmentId': state.compartment_id,
            'instanceId': primary_instance_id,
            'vnicId': primary_vnic_wan_id,
            'nicIndex': 1,
            'lifecycleState': 'ATTACHED'
        },
        {
            'id': 'ocid1.vnicattachment.secondary.0',
            'compartmentId': state.compartment_id,
            'instanceId': secondary_instance_id,
            'vnicId': secondary_vnic_internal_id,
            'nicIndex': 0,
            'lifecycleState': 'ATTACHED'
        },
        {
            'id': 'ocid1.vnicattachment.secondary.1',
            'compartmentId': state.compartment_id,
            'instanceId': secondary_instance_id,
            'vnicId': secondary_vnic_wan_id,
            'nicIndex': 1,
            'lifecycleState': 'ATTACHED'
        }
    ]

    # Create route table with routes
    protected_route_table_id = "ocid1.routetable.oc1.iad.protected"

    state.route_tables = [
        {
            'id': protected_route_table_id,
            'compartmentId': state.compartment_id,
            'vcnId': state.vcn_id,
            'displayName': 'Protected Route Table',
            'routeRules': [
                # Local route (automatically managed by OCI)
                {
                    'destination': '10.0.0.0/16',
                    'destinationType': 'CIDR_BLOCK',
                    'networkEntityId': state.vcn_id,
                },
                # Default route via primary NGFW
                {
                    'destination': '0.0.0.0/0',
                    'destinationType': 'CIDR_BLOCK',
                    'networkEntityId': primary_private_ip_internal_id,
                },
                # Another route via other interface (should not be modified)
                {
                    'destination': '192.168.0.0/24',
                    'destinationType': 'CIDR_BLOCK',
                    'networkEntityId': other_private_ip_id,
                }
            ]
        }
    ]

    # Create reserved public IP
    reserved_public_ip_id = "ocid1.publicip.oc1.iad.reserved"
    state.public_ips = [
        {
            'id': reserved_public_ip_id,
            'compartmentId': state.compartment_id,
            'ipAddress': '203.0.113.10',
            'lifetime': 'RESERVED',
            'assignedEntityId': primary_private_ip_wan_id
        }
    ]

    return OCIConf(
        compute_client=compute_client,
        vcn_client=vcn_client,
        state=state,
        primary_instance_id=primary_instance_id,
        secondary_instance_id=secondary_instance_id,
        protected_route_table_id=protected_route_table_id,
        primary_vnic_ids=[primary_vnic_internal_id, primary_vnic_wan_id],
        secondary_vnic_ids=[secondary_vnic_internal_id, secondary_vnic_wan_id],
        primary_private_ip_ids=[
            primary_private_ip_internal_id,
            primary_private_ip_wan_id
        ],
        secondary_private_ip_ids=[
            secondary_private_ip_internal_id,
            secondary_private_ip_wan_id
        ],
        primary_ips=['10.0.11.10', '10.0.12.10'],
        secondary_ips=['10.0.21.10', '10.0.22.10'],
        other_vnic_id=other_vnic_id,
        other_private_ip_id=other_private_ip_id,
        reserved_public_ip_id=reserved_public_ip_id
    )


@pytest.fixture(autouse=True)
def mock_get_compartment_id(oci_conf: OCIConf, monkeypatch: pytest.MonkeyPatch):
    """Automatically mock get_compartment_id for all tests."""
    monkeypatch.setattr(
       'ha_script.oci.metadata.get_compartment_id',
       lambda: oci_conf.state.compartment_id
    )


@pytest.fixture(autouse=True)
def mock_send_event_to_smc(monkeypatch: pytest.MonkeyPatch):
    """Automatically mock SMC event sending on all tests."""
    monkeypatch.setattr(
       'ha_script.smc_events.send_event_to_smc',
       lambda *args, **_: print(*args)
    )
