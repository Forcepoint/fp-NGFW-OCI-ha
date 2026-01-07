import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional
from collections.abc import Iterator

import requests
import requests.adapters
import requests.exceptions

import ha_script.oci as oci
import ha_script.oci.auth as auth
import ha_script.oci.metadata as metadata
from ha_script.config import HAScriptConfig
from ha_script.exceptions import HAScriptError
from ha_script.smc_events import send_error_to_smc


LOGGER = logging.getLogger(__name__)

# Type alias for OCI client tuple
OCIClients = tuple['ComputeClient', 'VirtualNetworkClient']

OCI_API_VERSION = "20160918"


@dataclass
class LocalNetContext:
    # Internal network interface ID as seen from cloud. Resolved on startup.
    internal_nic_id: str

    # Internal network interface ID as seen from cloud. Resolved on startup.
    wan_nic_id: str

    # Internal network private IP address. Resolved on startup.
    internal_ip: str

    # Internal network private IP address OCID. Resolved on startup.
    internal_ip_id: str

    # WAN network private IP address. Resolved on startup.
    wan_ip: Optional[str] = None

    # WAN network private IP address OCID. Resolved on startup.
    wan_ip_id: Optional[str] = None


@dataclass
class RouteInfo:
    # OCI route rule state (typically routes don't have explicit state in OCI)
    route_state: str

    # Route destination CIDR (e.g.  "0.0.0.0/0")
    route_dest: str

    # OCI private IP OCID (target of the route)
    target_ip_id: str

    # The actual private IP address (for comparison purposes)
    target_ip: str

    # Associated VNIC OCID
    vnic_id: str

    # OCI route table OCID
    route_table_id: str


class OCIClient:
    """Base OCI HTTP API client."""

    def __init__(self, service: str, request_signer: auth.RequestSigner):
        self.region = metadata.get_instance_region()
        self.service = service
        self.request_signer = request_signer
        self.host = f"{service}.{self.region}.oraclecloud.com"
        self._session = oci.session_with_retry()

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: Optional[dict[str, str]] = None
    ) -> requests.Response:
        """Make an authenticated request to OCI API."""
        url = urllib.parse.urlunparse((
            "https",
            self.host,
            path,
            '',
            urllib.parse.urlencode(params) if params else '',
            ''
        ))
        response = self._session.request(
            method=method,
            url=url,
            auth=self.request_signer,
            data=json.dumps(body) if body else None,
            timeout=30
        )
        if not response.ok:
            LOGGER.error(
                "OCI API request failed: %s %s - Status: %d - Response: %s",
                method, url, response.status_code, response.text
            )
        response.raise_for_status()
        return response

    def get(self, path: str, params: Optional[dict[str, str]] = None) -> Any:
        response = self._request("GET", path, params=params)
        return response.json()

    def post(self, path: str, body: Any) -> Any:
        response = self._request("POST", path, body=body)
        return response.json()

    def put(self, path: str, body: Any) -> Any:
        response = self._request("PUT", path, body=body)
        return response.json()

    def delete(self, path: str) -> Any:
        response = self._request("DELETE", path)
        if response.content:
            return response.json()
        return {}


class ComputeClient(OCIClient):
    """OCI Compute service HTTP API client."""

    def __init__(self, request_signer: auth.RequestSigner) -> None:
        super().__init__("iaas", request_signer)

    def get_instance(self, instance_id: str) -> Any:
        return self.get(f"/{OCI_API_VERSION}/instances/{instance_id}")

    def update_instance(self, instance_id: str,
                        update_details: dict[str, str]) -> Any:
        return self.put(
            f"/{OCI_API_VERSION}/instances/{instance_id}",
            update_details
        )

    def list_vnic_attachments(
        self,
        compartment_id: str,
        instance_id: Optional[str] = None,
        vnic_id: Optional[str] = None
    ) -> Any:
        params = {"compartmentId": compartment_id}
        if instance_id:
            params["instanceId"] = instance_id
        if vnic_id:
            params["vnicId"] = vnic_id
        return self.get(f"/{OCI_API_VERSION}/vnicAttachments", params=params)


class VirtualNetworkClient(OCIClient):
    """OCI Virtual Network service HTTP API client."""

    def __init__(self, request_signer: auth.RequestSigner) -> None:
        super().__init__("iaas", request_signer)

    def get_vnic(self, vnic_id: str) -> Any:
        return self.get(f"/{OCI_API_VERSION}/vnics/{vnic_id}")

    def get_private_ip(self, private_ip_id: str) -> Any:
        return self.get(f"/{OCI_API_VERSION}/privateIps/{private_ip_id}")

    def list_private_ips(self, vnic_id: str) -> Any:
        return self.get(
            f"/{OCI_API_VERSION}/privateIps",
            params={"vnicId": vnic_id},
        )

    def get_route_table(self, route_table_id: str) -> Any:
        return self.get(f"/{OCI_API_VERSION}/routeTables/{route_table_id}")

    def update_route_table(self, route_table_id: str,
                           details: dict[str, Any]) -> Any:
        return self.put(
            f"/{OCI_API_VERSION}/routeTables/{route_table_id}",
            details,
        )

    def get_public_ip(self, public_ip_id: str) -> Any:
        return self.get(f"/{OCI_API_VERSION}/publicIps/{public_ip_id}")

    def get_public_ip_by_private_ip_id(self, private_ip_id: str) -> Any:
        return self.post(
            f"/{OCI_API_VERSION}/publicIps/actions/getByPrivateIpId",
            {"privateIpId": private_ip_id},
        )

    def update_public_ip(self, public_ip_id: str,
                         private_ip_id: Optional[str]) -> Any:
        return self.put(
            f"/{OCI_API_VERSION}/publicIps/{public_ip_id}",
            {"privateIpId": private_ip_id},
        )

    def delete_public_ip(self, public_ip_id: str) -> None:
        self.delete(f"/{OCI_API_VERSION}/publicIps/{public_ip_id}")


def get_oci_clients() -> OCIClients:
    """Initialize and return OCI compute and virtual network clients.

    Uses instance principal authentication for instances running in OCI.

    :return: Oracle cloud clients
    """
    try:
        request_signer = auth.RequestSigner()
        compute_client = ComputeClient(request_signer)
        vcn_client = VirtualNetworkClient(request_signer)

        return compute_client, vcn_client
    except Exception as e:
        LOGGER.critical("Failed to initialize OCI clients: %s", str(e))
        raise e from None


def get_config_tags(
    clients: OCIClients,
    instance_id: Optional[str] = None
) -> dict[str, Any]:
    """Create a dictionary config from OCI instance freeform tags.

    Configuration properties are taken from the freeform tags of the given OCI
    instance (by default, local instance). Only tags starting with 'FP_HA_' are
    considered.

    For example, if tag "FP_HA_route_table_id" has value
    "ocid1.routetable.oc1.. .", the dictionary will contain the following
    value:

    {"route_table_id": "ocid1.routetable.oc1... "}

    :param clients: OCI clients
    :param instance_id: OCI instance OCID
    :return: dictionary of config properties
    """
    compute_client = clients[0]

    if not instance_id:
        instance_id = metadata.get_instance_id()

    try:
        instance = compute_client.get_instance(instance_id)
        filtered_tags = {}

        # Check freeform tags
        freeform_tags = instance.get("freeformTags", {})
        if freeform_tags:
            for key, value in freeform_tags.items():
                if key.startswith("FP_HA_"):
                    tag_key = key.replace("FP_HA_", "")
                    filtered_tags[tag_key] = value

        return filtered_tags
    except Exception as e:
        LOGGER.error("Failed to get instance tags: %s", str(e))
        return {}


def get_config_tag_value(
    clients: OCIClients,
    tag: str,
    instance_id: Optional[str] = None
) -> Optional[Any]:
    """Get value of a config property from OCI instance freeform tags.

    :param clients: OCI clients
    :param tag: config property name
    :param instance_id: OCI instance OCID
    :return: config property value or None, if property is not found
    """
    tags = get_config_tags(clients, instance_id)
    if tag in tags:
        return tags[tag]
    LOGGER.debug(
        "OCI instance tag not found, instance_id: %s, tag:  %s",
        instance_id,
        tag
    )
    return None


def set_config_tag(
    config: HAScriptConfig,
    clients: OCIClients,
    tag: str,
    value: str,
    instance_id: Optional[str] = None
) -> bool:
    """Add a freeform tag to the OCI instance.

    :param config: configuration from the main program
    :param clients: OCI clients
    :param tag: tag name
    :param value: value to set
    :param instance_id: OCI instance OCID
    :return: True if the tag was added, False otherwise

    The `tag` parameter will be prefixed with `FP_HA_` when set on instance.
    """
    compute_client = clients[0]

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not modify instance tag, key: FP_HA_%s, value: %s",
            tag,
            value
        )
        return True

    try:
        if not instance_id:
            instance_id = metadata.get_instance_id()

        # Get current instance to retrieve existing tags
        instance = compute_client.get_instance(instance_id)

        # Update freeform tags
        freeform_tags = instance.get("freeformTags", {}).copy()
        freeform_tags[f"FP_HA_{tag}"] = value

        # Update the instance with new tags
        update_details = {
            "freeformTags": freeform_tags
        }
        compute_client.update_instance(instance_id, update_details)

        return True
    except Exception as e:
        send_error_to_smc(config, f"Failed to set OCI instance tag: {e}")
        return False


def create_local_net_context(config: HAScriptConfig,
                             clients: OCIClients) -> LocalNetContext:
    """Create a context out of the instance networking

    :param config: configuration from the main program
    :param clients: OCI clients
    :return: Instance of LocalNetContext
    :raises HAScriptError: if the OCI instance does not have a VNIC with the
                           given device index
    """
    compute_client, vcn_client = clients
    vnics = metadata.get_vnics()
    try:
        vnic = vnics[config.internal_nic_idx]
    except IndexError:
        raise HAScriptError(
            f"Out of bounds internal_nic_idx '{config.internal_nic_idx}.' "
            f"Make sure this instance has the expected vnics attached."
        )

    internal_nic_id = vnic["vnicId"]
    internal_ip = vnic.get("privateIp")
    if not internal_ip:
        raise HAScriptError(
            f"Failed to find VNIC '{internal_nic_id}' private IP"
        )

    for private_ip in vcn_client.list_private_ips(internal_nic_id):
        if private_ip["ipAddress"] == internal_ip:
            internal_ip_id = private_ip["id"]
            break
    else:
        raise HAScriptError(
            f"Failed to find VNIC '{internal_nic_id}' private IP"
        )

    try:
        vnic = vnics[config.wan_nic_idx]
    except IndexError:
        raise HAScriptError(
            f"Out of bounds wan_nic_idx '{config.wan_nic_idx}. "
            f"Make sure this instance has the expected vnics attached."
        )
    wan_nic_id = vnic["vnicId"]
    wan_ip = vnic.get("privateIp")
    if not wan_ip:
        raise HAScriptError(f"Failed to find VNIC '{wan_nic_id}' private IP")

    for private_ip in vcn_client.list_private_ips(wan_nic_id):
        if private_ip["ipAddress"] == wan_ip:
            wan_ip_id = private_ip["id"]
            break
    else:
        raise HAScriptError(f"Failed to find VNIC '{wan_nic_id}' private IP")

    ctx = LocalNetContext(
        internal_nic_id=internal_nic_id,
        internal_ip=internal_ip,
        internal_ip_id=internal_ip_id,
        wan_nic_id=wan_nic_id,
        wan_ip=wan_ip,
        wan_ip_id=wan_ip_id,
    )
    LOGGER.info("created local network context: %s", ctx)
    return ctx


def get_route_table_info(
    clients: OCIClients,
    route_table_ids: str,
    ngfw_instance_ids: list[str]
) -> Iterator[RouteInfo]:
    """Iterates over all routes via NGFWs from the specified route tables.

    :param clients: OCI clients
    :param route_table_ids: comma-separated list of route table OCIDs
    :param ngfw_instance_ids: list of NGFW instance OCIDs

    :return: yields RouteInfo per rule found
    """
    compute_client, vcn_client = clients
    compartment = metadata.get_compartment_id()

    for route_table_id in route_table_ids.split(","):
        route_table_id = route_table_id.strip()
        route_table = vcn_client.get_route_table(route_table_id)

        for rule in route_table.get("routeRules", []):
            network_entity_id = rule.get("networkEntityId")
            if not network_entity_id:
                # A missing networkEntityId indicates a blackhole route.
                # Yield it so the secondary mainloop can detect this and
                # trigger a takeover.
                LOGGER.warning(
                    "route table rule with empty target (blackhole): %s",
                    rule.get("destination", "<unknown>"),
                )
                yield RouteInfo(
                    route_state="blackhole",
                    route_dest=rule.get("destination", ""),
                    target_ip_id="",
                    target_ip="",
                    vnic_id="",
                    route_table_id=route_table_id,
                )
                continue

            # Only process routes with private IP targets
            if not network_entity_id.startswith("ocid1.privateip"):
                LOGGER.warning("route table rule with non-ip4 target")
                continue

            private_ip = vcn_client.get_private_ip(network_entity_id)
            vnic_attachments = compute_client.list_vnic_attachments(
                compartment_id=compartment,
                vnic_id=private_ip["vnicId"],
            )
            if not vnic_attachments:
                LOGGER.warning("route table rule not attached to an instance")
                continue
            if len(vnic_attachments) > 1:
                LOGGER.warning("route table rule with multiple attachments")

            # Check if this route points to an NGFW instance
            if vnic_attachments[0]["instanceId"] in ngfw_instance_ids:
                yield RouteInfo(
                    route_state="ACTIVE",
                    route_dest=rule["destination"],
                    target_ip_id=network_entity_id,
                    target_ip=private_ip["ipAddress"],
                    vnic_id=private_ip["vnicId"],
                    route_table_id=route_table_id,
                )


def update_route_table(
    config: HAScriptConfig,
    clients: OCIClients,
    route_table_id: str,
    dest: str,
    local_net_ctx: LocalNetContext
) -> bool:
    """Update the OCI route table.

    Update the route table to use the given private IP (associated with a VNIC)
    for the specified destination.

    :param config: configuration from the main program
    :param clients: OCI clients
    :param route_table_id: route table OCID
    :param dest: destination CIDR for the route, e.g. "0.0.0.0/0"
    :param local_net_ctx: Local network context
    :return: True if the update is successful, False otherwise.
    """
    vcn_client = clients[1]

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not modify route, dest: %s, internal_ip_id: %s",
            dest, local_net_ctx.internal_ip_id,
        )
        return True

    try:
        route_table = vcn_client.get_route_table(route_table_id)
    except Exception as e:
        send_error_to_smc(config, f"Unable to read routes from API:  {e}")
        return False

    rules = []
    rule_found = False

    for rule in route_table.get("routeRules", []):
        if rule["destination"] == dest:
            rule = {
                "destination": dest,
                "destinationType": rule.get(
                    "destinationType",
                    "CIDR_BLOCK"
                ),
                "networkEntityId": local_net_ctx.internal_ip_id,
            }
            rule_found = True
            LOGGER.info(
                "Modifying route, dest: %s, internal_ip_id: %s",
                dest,
                local_net_ctx.internal_ip,
            )
        rules.append(rule)

    if not rule_found:
        LOGGER.warning("Route rule not found for destination: %s", dest)
        return False

    try:
        vcn_client.update_route_table(route_table_id, {"routeRules": rules})
    except Exception as e:
        send_error_to_smc(config, f"Failed to update routes:  {e}")
        return False

    LOGGER.info("Modifying route done.")
    return True


def resolve_public_ip(
    config: HAScriptConfig,
    clients: OCIClients
) -> tuple[Optional[str], Optional[str]]:
    """Get a public IP and its assigned private IP

    :param config: configuration from the main program
    :param clients: OCI clients
    :return: tuple of public IP address and assigned entity id
    """
    public_ip = clients[1].get_public_ip(config.reserved_public_ip_id)
    return public_ip.get("ipAddress"), public_ip.get("assignedEntityId")


def move_public_ip(
    config: HAScriptConfig,
    clients: OCIClients,
    local_net_ctx: LocalNetContext
) -> bool:
    """Move reserved public IP defined in config to the local instance

    :param config: configuration from the main program
    :param clients: OCI clients
    :param local_net_ctx: Local network context
    :return: True if the move is successful, False otherwise.
    """
    vcn_client = clients[1]
    public_ip_id = config.reserved_public_ip_id
    public_ip = vcn_client.get_public_ip(public_ip_id)

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not move public ip, dest: %s, internal_ip_id: %s",
            public_ip['ipAddress'],
            local_net_ctx.wan_ip_id,
        )
        return True

    if not local_net_ctx.wan_ip_id:
        raise ValueError("move_public_ip() called with incomplete context")

    LOGGER.info(f"Moving public IP '{public_ip['ipAddress']}' to "
                f"'{local_net_ctx.wan_ip_id}'.")
    try:
        vcn_client.update_public_ip(public_ip_id, local_net_ctx.wan_ip_id)
    except requests.exceptions.HTTPError as e:
        if e.response is None:
            raise
        if e.response.status_code != 409:
            raise

        # In case an instance already has an ephemeral public IP, release it
        # and assign the reserved IP instead.  This is to support instances
        # that were started with auto assigned public IP address.
        assigned_ip = vcn_client.get_public_ip_by_private_ip_id(
            local_net_ctx.wan_ip_id
        )
        if assigned_ip["lifetime"] != "EPHEMERAL":
            LOGGER.debug("Refusing to remove non-ephemeral public IP")
            raise

        LOGGER.info(f"Private IP '{local_net_ctx.wan_ip_id}' already "
                    f"has a public IP assigned {assigned_ip['ipAddress']}. "
                    f"Replacing with configured reserved IP.")
        vcn_client.delete_public_ip(assigned_ip["id"])
        vcn_client.update_public_ip(public_ip_id, local_net_ctx.wan_ip_id)

    LOGGER.info(f"Public IP '{public_ip['ipAddress']}' has been moved to "
                f"'{local_net_ctx.wan_ip_id}'.")
    return True


def get_instance_ip_addresses(
    clients: OCIClients,
    instance_id: str
) -> list[str]:
    """Get all private IP addresses from the given OCI instance.

    :param clients: OCI clients
    :param instance_id: OCI instance OCID
    :return: list of private IP addresses
    """
    compute_client, vcn_client = clients

    try:
        instance = compute_client.get_instance(instance_id)
    except Exception as e:
        LOGGER.error("Failed to find instance %s:", instance_id, str(e))
        return []

    try:
        # Get all VNIC attachments for the instance
        vnic_attachments = compute_client.list_vnic_attachments(
            compartment_id=instance["compartmentId"],
            instance_id=instance_id
        )
    except Exception as e:
        LOGGER.error("Failed to get VNICs for %s: %s", instance_id, str(e))
        return []

    ip_list = []
    for attachment in vnic_attachments:
        if attachment["lifecycleState"] == "ATTACHED":
            vnic_id = attachment.get("vnicId")

            try:
                vnic = vcn_client.get_vnic(vnic_id)
            except Exception as e:
                LOGGER.error("Failed to get VNIC %s: %s", vnic_id, str(e))
                return []

            private_ip = vnic.get("privateIp")
            if private_ip:
                ip_list.append(private_ip)

    LOGGER.debug("found instance IPs: %s", str(ip_list))
    return ip_list
