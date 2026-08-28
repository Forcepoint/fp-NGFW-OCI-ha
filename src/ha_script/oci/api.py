import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
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

WR_POLL_INTERVAL = 1   # seconds between work request polls
WR_TIMEOUT = 120       # maximum wait in seconds


@dataclass
class LocalNetContext:
    # All values here are resolved on startup.

    # Internal network interface ID as seen from cloud.
    internal_nic_id: str

    # Internal network private IP address.
    internal_ip: str

    # Internal network private IP address OCID.
    internal_ip_id: str

    # Source address the remote probe socket binds to. Resolved on
    # startup from remote_probe_nic_idx.
    remote_probe_src_ip: str = ""

    # [(public_ip_ocid, target_private_ip_ocid, public_ip_address), ...]
    public_ip_targets: list[tuple[str, str, str]] = field(default_factory=list)


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
        if response.status_code == 401:
            LOGGER.warning(
                "OCI API returned 401, refreshing token and retrying: "
                "%s %s", method, url,
            )
            self.request_signer.invalidate()
            response = self._session.request(
                method=method,
                url=url,
                auth=self.request_signer,
                data=json.dumps(body) if body else None,
                timeout=30,
            )
        if not response.ok:
            LOGGER.error(
                "OCI API request failed: %s %s - Status: %d - Response: %s",
                method, url, response.status_code, response.text
            )
        response.raise_for_status()
        if method in ("PUT", "PATCH", "POST", "DELETE"):
            self._poll_work_request(response)
        return response

    def _poll_work_request(self, response: requests.Response) -> None:
        """Poll an OCI work request until it completes.

        OCI mutating operations may be asynchronous.  When the response
        includes an opc-work-request-id header, the caller must poll the
        work-request endpoint until the operation reaches a terminal
        state (SUCCEEDED, FAILED, or CANCELED).  The Retry-After header,
        when present, dictates the polling interval.

        https://docs.oracle.com/en-us/iaas/Content/API/Concepts/workrequests.htm
        """
        wr_id = response.headers.get("opc-work-request-id")
        if not wr_id:
            return

        url = f"https://{self.host}/{OCI_API_VERSION}/workRequests/{wr_id}"
        deadline = time.monotonic() + WR_TIMEOUT
        LOGGER.debug("Polling work request %s", wr_id)

        while time.monotonic() < deadline:
            poll_resp = self._session.get(
                url,
                auth=self.request_signer,
                timeout=30,
            )
            if poll_resp.status_code == 401:
                LOGGER.warning(
                    "Work request poll returned 401, refreshing token "
                    "and retrying: %s", url,
                )
                self.request_signer.invalidate()
                poll_resp = self._session.get(
                    url,
                    auth=self.request_signer,
                    timeout=30,
                )
            if not poll_resp.ok:
                LOGGER.error(
                    "Work request poll failed: "
                    "%s - Status: %d - Response: %s",
                    url, poll_resp.status_code, poll_resp.text,
                )
            poll_resp.raise_for_status()

            status = poll_resp.json().get("status", "")
            if status == "SUCCEEDED":
                LOGGER.debug("Work request %s succeeded", wr_id)
                return
            if status in ("FAILED", "CANCELED"):
                raise requests.HTTPError(
                    f"Work request {wr_id} {status}",
                    response=poll_resp,
                )

            try:
                retry_after = int(poll_resp.headers["Retry-After"])
            except (KeyError, ValueError, TypeError) as err:
                LOGGER.debug("Unable to read Retry-After header: %s", str(err))
                retry_after = WR_POLL_INTERVAL

            LOGGER.debug("Work request status: %s, retrying in %ds...",
                         status, retry_after)
            time.sleep(retry_after)

        raise requests.HTTPError(
            f"Work request {wr_id} timed out after {WR_TIMEOUT}s",
            response=response,
        )

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

    def get_public_ip_by_ip_address(self, ip_address: str) -> Any:
        return self.post(
            f"/{OCI_API_VERSION}/publicIps/actions/getByIpAddress",
            {"ipAddress": ip_address},
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
                             clients: OCIClients,
                             is_primary: bool = True) -> LocalNetContext:
    """Create a context out of the instance networking

    :param config: configuration from the main program
    :param clients: OCI clients
    :param is_primary: True if this instance is the primary
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

    # Build IP address → OCID map across all VNICs for public IP resolution
    ip_to_id: dict[str, str] = {}
    for v in vnics:
        for p in vcn_client.list_private_ips(v["vnicId"]):
            ip_to_id[p["ipAddress"]] = p["id"]

    # The remote probe source address defaults to the internal VNIC IP;
    # remote_probe_nic_idx selects another VNIC explicitly.
    remote_probe_src_ip = internal_ip
    if config.remote_probe_nic_idx >= 0:
        try:
            remote_probe_vnic = vnics[config.remote_probe_nic_idx]
        except IndexError:
            raise HAScriptError(
                f"Failed to find remote probe NIC at index "
                f"{config.remote_probe_nic_idx}"
            )
        remote_probe_src_ip = remote_probe_vnic.get("privateIp", "")
        if not remote_probe_src_ip:
            raise HAScriptError(
                f"VNIC at index {config.remote_probe_nic_idx} missing "
                f"privateIp"
            )

    # Resolve public IP targets from config
    public_ip_targets: list[tuple[str, str, str]] = []
    for name, value in config.reserved_public_ips.items():
        if value.startswith("ocid"):
            # OCID format: route to private IP on wan_nic_idx VNIC
            try:
                wan_vnic = vnics[config.wan_nic_idx]
            except IndexError:
                raise HAScriptError(
                    f"Out of bounds wan_nic_idx '{config.wan_nic_idx}' for "
                    f"'reserved_public_ip_{name}'. "
                    f"Make sure this instance has the expected vnics attached."
                )
            wan_ip = wan_vnic.get("privateIp")
            if not wan_ip or wan_ip not in ip_to_id:
                raise HAScriptError(
                    f"Failed to find WAN VNIC private IP for "
                    f"'reserved_public_ip_{name}'"
                )
            try:
                pub_ip = vcn_client.get_public_ip(value)
            except Exception as e:
                raise HAScriptError(
                    f"Failed to resolve 'reserved_public_ip_{name}' "
                    f"'{value}': {e}"
                ) from e
            ip_address = pub_ip.get("ipAddress", "")
            public_ip_targets.append((value, ip_to_id[wan_ip], ip_address))
        else:
            # Tuple format: pub_addr,primary_priv_addr,secondary_priv_addr
            parts = [part.strip() for part in value.split(",")]
            pub_addr, primary_priv_addr, secondary_priv_addr = parts
            if is_primary:
                my_priv_addr = primary_priv_addr
            else:
                my_priv_addr = secondary_priv_addr
            if my_priv_addr not in ip_to_id:
                raise HAScriptError(
                    f"Private IP '{my_priv_addr}' from "
                    f"'reserved_public_ip_{name}' not found on any VNIC"
                )
            try:
                pub_ip = vcn_client.get_public_ip_by_ip_address(pub_addr)
                pub_ip_id = pub_ip["id"]
            except Exception as e:
                raise HAScriptError(
                    f"Failed to resolve public IP '{pub_addr}' for "
                    f"'reserved_public_ip_{name}': {e}"
                ) from e
            public_ip_targets.append(
                (pub_ip_id, ip_to_id[my_priv_addr], pub_addr)
            )

    ctx = LocalNetContext(
        internal_nic_id=internal_nic_id,
        internal_ip=internal_ip,
        internal_ip_id=internal_ip_id,
        remote_probe_src_ip=remote_probe_src_ip,
        public_ip_targets=public_ip_targets,
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
                local_net_ctx.internal_ip_id,
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


def resolve_public_ip(clients: OCIClients, public_ip_id: str) -> Optional[str]:
    """Get the current assignee of a public IP.

    :param clients: OCI clients
    :param public_ip_id: OCID of the reserved public IP
    :return: OCID of the private IP currently assigned, or None
    """
    vcn_client = clients[1]
    pub_ip = vcn_client.get_public_ip(public_ip_id)
    return pub_ip.get("assignedEntityId")


def move_public_ip(
    config: HAScriptConfig,
    clients: OCIClients,
    public_ip_id: str,
    target_private_ip_id: str,
) -> bool:
    """Move reserved public IP to the given private IP

    :param config: configuration from the main program
    :param clients: OCI clients
    :param public_ip_id: OCID of the reserved public IP to move
    :param target_private_ip_id: target private IP OCID
    :return: True if the move is successful, False otherwise.
    """
    vcn_client = clients[1]
    dest_ip_id = target_private_ip_id

    try:
        public_ip = vcn_client.get_public_ip(public_ip_id)
    except Exception as e:
        send_error_to_smc(
            config, f"Failed to get public IP '{public_ip_id}': {e}")
        return False

    ip_addr = public_ip['ipAddress']

    if config.dry_run:
        LOGGER.warning(
            "DRY-RUN: Do not move public ip, dest: %s, internal_ip_id: %s",
            ip_addr, dest_ip_id,
        )
        return True

    if not dest_ip_id:
        send_error_to_smc(
            config, "move_public_ip() called with incomplete context")
        return False

    LOGGER.info(f"Moving public IP '{ip_addr}' to '{dest_ip_id}'.")
    try:
        vcn_client.update_public_ip(public_ip_id, dest_ip_id)
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 409:
            send_error_to_smc(
                config, f"Failed to move public IP '{ip_addr}': {e}")
            return False

        # In case an instance already has an ephemeral public IP, release it
        # and assign the reserved IP instead.  This is to support instances
        # that were started with auto assigned public IP address.
        try:
            assigned_ip = vcn_client.get_public_ip_by_private_ip_id(
                dest_ip_id)
        except Exception as inner:
            send_error_to_smc(
                config,
                f"Failed to move public IP '{ip_addr}': "
                f"409 conflict and unable to resolve: {inner}")
            return False

        if assigned_ip["lifetime"] != "EPHEMERAL":
            LOGGER.debug("Refusing to remove non-ephemeral public IP")
            send_error_to_smc(
                config,
                f"Failed to move public IP '{ip_addr}': "
                f"target already has a non-ephemeral public IP")
            return False

        LOGGER.info(f"Private IP '{dest_ip_id}' already "
                    f"has a public IP assigned {assigned_ip['ipAddress']}. "
                    f"Replacing with configured reserved IP.")
        try:
            vcn_client.delete_public_ip(assigned_ip["id"])
            vcn_client.update_public_ip(public_ip_id, dest_ip_id)
        except Exception as inner:
            send_error_to_smc(
                config,
                f"Failed to move public IP '{ip_addr}' "
                f"after removing ephemeral IP: {inner}")
            return False
    except Exception as e:
        send_error_to_smc(
            config, f"Failed to move public IP '{ip_addr}': {e}")
        return False

    LOGGER.info(f"Public IP '{ip_addr}' has been moved to '{dest_ip_id}'.")
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
