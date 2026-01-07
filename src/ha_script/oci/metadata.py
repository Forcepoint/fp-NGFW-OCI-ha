"""Interface for instance metadata service on Oracle cloud"""

import logging
from typing import Any

import requests
import requests.adapters

import ha_script.oci as oci


LOGGER = logging.getLogger(__name__)
METADATA_URL = "http://169.254.169.254/opc/v2"


def get_metadata(path: str) -> requests.Response:
    """Get metadata from OCI Instance Metadata Service."""
    session = oci.session_with_retry()
    resp = session.get(
        f"{METADATA_URL}/{path}",
        headers={
            "Authorization": "Bearer Oracle",
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp


def get_instance_id() -> str:
    """Get the OCID of the current compute instance."""
    return get_metadata("instance/id").text


def get_compartment_id() -> str:
    """Get the compartment OCID of the current instance."""
    return get_metadata("instance/compartmentId").text


def get_vnics() -> Any:
    """Get vnics of the current instance."""
    return get_metadata("vnics").json()


def get_instance_region() -> str:
    return get_metadata("instance/region").text


def get_identity_cert() -> str:
    return get_metadata("identity/cert.pem").text


def get_identity_key() -> str:
    return get_metadata("identity/key.pem").text


def get_identity_intermediate_cert() -> str:
    return get_metadata("identity/intermediate.pem").text
