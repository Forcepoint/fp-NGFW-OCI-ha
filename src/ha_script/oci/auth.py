"""OCI API Authentication

A standalone authentication implementation for OCI API with IAM instance
principal credentials.  Used on an instance to authenticate against OCI API.

Implementation provides a 'requests' authentication method for the draft RFC
draft-cavage-http-signatures-08 [1] against OCI API.

Federated access is based on a two-step approach. First exchange instance
certificates for a token, then sign API request headers with token.

Algorithm:

    1. Fetch instance certificates from metadata service
    2. Generate ephemeral session key pair
    3. Request security token from auth service
    4. Sign API requests with token and session private key

Example usage:

    >>> import requests
    >>> from oci.auth import RequestSigner
    >>> requests.get(url, auth=RequestSigner())

[1] https://datatracker.ietf.org/doc/html/draft-cavage-http-signatures-08
"""

import json
import time
import base64
import hashlib
import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Optional, Union

import requests
import requests.adapters
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key
)

import ha_script.oci.metadata as metadata
import ha_script.oci


LOGGER = logging.getLogger(__name__)

SIGNED_HEADERS = ["date", "(request-target)", "host",]
SIGNED_HEADERS_BODY = ["content-length", "content-type", "x-content-sha256",]
SIGNED_HEADERS_ALL = SIGNED_HEADERS + SIGNED_HEADERS_BODY

TOKEN_URL_FMT = "https://auth.{}.oraclecloud.com/v1/x509"


class Token:

    def __init__(self, value: str, expires: int) -> None:
        self.value = value
        self.expires = expires
        self.renew_headroom = 120

    def expired(self) -> bool:
        return time.time() >= self.expires - self.renew_headroom


class RequestSigner(requests.auth.AuthBase):
    def __init__(self) -> None:
        self.credentials: Optional[Token] = None
        self.token: Optional[Token] = None

    def __call__(self,
                 r: requests.PreparedRequest) -> requests.PreparedRequest:
        if not r.method or not r.url:
            return r

        if (
            self.credentials is None
            or self.token is None
            or self.token.expired()
        ):
            LOGGER.debug("Refreshing security token...")
            self.credentials = _session_credentials()
            self.token = _request_token(self.credentials)

        r.headers.update(_sign_request(
            self.credentials["session_key"],
            self.token,
            r.method,
            r.url,
            r.body
        ))

        return r


def _session_credentials() -> dict[str, Any]:
    """Create instance specific session credentials"""
    identity_key_pem = metadata.get_identity_key()
    identity_key = load_pem_private_key(
        identity_key_pem.encode(),
        None,
        default_backend()
    )
    identity_cert_pem = metadata.get_identity_cert()
    identity_cert = x509.load_pem_x509_certificate(
        identity_cert_pem.encode(),
        default_backend()
    )

    for attr in identity_cert.subject:
        if attr.oid == x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME:
            value = attr.value
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="ignore")
            if value.startswith("opc-tenant:"):
                tenancy_id = value.split(":", 1)[1]
                break
    else:
        raise ValueError("Tenancy OCID not found in certificate")

    session_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    session_cert = session_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode()

    return {
        "region": metadata.get_instance_region(),
        "tenancy_id": tenancy_id,
        "identity_key": identity_key,
        "identity_key_pem": identity_key_pem,
        "identity_cert": identity_cert,
        "identity_cert_pem": identity_cert_pem,
        "intermediate_cert_pem": metadata.get_identity_intermediate_cert(),
        "session_key": session_key,
        "session_cert_pem": session_cert,
    }


def _strip_pem(pem: str) -> str:
    """Remove PEM headers/footers and whitespace from certificate."""
    lines = [line.strip() for line in pem.strip().split('\n')
             if not line.startswith('-----') and line.strip()]
    return ''.join(lines)


def _create_signature_date() -> str:
    """Create a 'Date' string header used in signing"""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _create_signature_payload(method: str, url: str,
                              headers: dict[str, str]) -> str:
    """Create a payload from headers that is used to create the signature"""
    parts = []

    parsed = urllib.parse.urlparse(url)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    for header in SIGNED_HEADERS_ALL:
        if header in headers:
            parts.append(f"{header}: {headers[header]}")
        if header == "(request-target)":
            parts.append(f"(request-target): {method.lower()} {path}")

    return "\n".join(parts)


def _create_signature(data: str, key: rsa.RSAPrivateKey) -> str:
    """Sign the data with instance private session key"""
    return base64.b64encode(key.sign(
        data.encode(),
        padding.PKCS1v15(),
        hashes.SHA256()
    )).decode()


def _create_authorization_header(headers: list[str], key_id: str,
                                 signature: str) -> str:
    """Create 'Authorization' header that describes the signing"""
    return (
        f'Signature version="1",headers="{" ".join(headers)}",'
        f'keyId="{key_id}",algorithm="rsa-sha256",signature="{signature}"'
    )


def _create_body_headers(body: Union[str, bytes]) -> dict[str, str]:
    """Create common headers needed for signing requests with a body"""
    encoded = body.encode("utf-8") if isinstance(body, str) else body
    return {
        "content-length": str(len(encoded)),
        "x-content-sha256": base64.b64encode(
            hashlib.sha256(encoded).digest()
        ).decode()
    }


def _parse_token(data: dict[str, str]) -> Token:
    """Parse the JWT token to extract expiration"""
    if not isinstance(data, dict):
        raise ValueError(f"Invalid token type: {type(data)}")
    if "token" not in data:
        raise ValueError(f"No token in response: {data}")

    token = data['token']
    try:
        _, payload, _ = token.split(".")
    except ValueError:
        raise ValueError("Malformed JWT token from remote") from None

    # JWT parts need to be realigned at 4 byte boundary
    payload = payload + '='*(-len(payload) % 4)
    decoded = json.loads(base64.b64decode(payload).decode())

    try:
        expiry = int(decoded["exp"])
    except KeyError:
        raise ValueError("Missing JWT token expiration") from None

    return Token(token, expiry)


def _request_token(credentials: dict[str, Any]) -> Token:
    """Request a new token against instance session credentials"""
    fingerprint = ":".join(
        f"{b:02X}"
        for b in credentials["identity_cert"].fingerprint(hashes.SHA1())
    )
    key_id = f"{credentials['tenancy_id']}/fed-x509/{fingerprint}"

    url = TOKEN_URL_FMT.format(credentials['region'])
    body = json.dumps({
        "certificate": _strip_pem(credentials["identity_cert_pem"]),
        "publicKey": _strip_pem(credentials["session_cert_pem"]),
        "intermediateCertificates": [
            _strip_pem(credentials["intermediate_cert_pem"])
        ],
    })
    headers = {
        "date": _create_signature_date(),
        "content-type": "application/json",
        **_create_body_headers(body),
    }
    headers["authorization"] = _create_authorization_header(
        [h for h in SIGNED_HEADERS_ALL if h != "host"],
        key_id,
        _create_signature(
            _create_signature_payload("POST", url, headers),
            credentials["identity_key"],
        ),
    )

    LOGGER.debug("Requesting security token from %s", url)
    session = ha_script.oci.session_with_retry()
    response = session.post(url, data=body, headers=headers, timeout=30)
    response.raise_for_status()

    return _parse_token(response.json())


def _sign_request(
    session_key: rsa.RSAPrivateKey,
    token: Token,
    method: str,
    url: str,
    body: Optional[Union[str, bytes]] = None
) -> dict[str, str]:
    """Sign a request using the federated keys"""
    headers = {
        "date": _create_signature_date(),
        "host": urllib.parse.urlparse(url).netloc,
        "accept": "application/json",
    }
    if body:
        headers.update({
            "content-type": "application/json",
            **_create_body_headers(body),
        })

    headers["authorization"] = _create_authorization_header(
        SIGNED_HEADERS_ALL if body else SIGNED_HEADERS,
        f"ST${token.value}",
        _create_signature(
            _create_signature_payload(method, url, headers),
            session_key,
        ),
    )

    LOGGER.debug("Signed %s %s", method, url)
    return headers
