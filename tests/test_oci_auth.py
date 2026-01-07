"""Tests for OCI authentication module."""

import re
import hmac
import json
import time
import base64
import datetime
from unittest import mock

import pytest
import requests
import responses
from cryptography import x509
from cryptography.hazmat import primitives, backends
from cryptography.hazmat.primitives.asymmetric import rsa

import ha_script.oci.auth as auth


DEFAULT_INSTANCE_ID = "ocid1.instance.oc1.phx.test123"
DEFAULT_INSTANCE_REGION = "us-phoenix-1"
DEFAULT_TENANCY_ID = "ocid1.tenancy.oc1..aaaaaaaatesttenancy"
DEFAULT_TOKEN_URL = auth.TOKEN_URL_FMT.format(DEFAULT_INSTANCE_REGION)


def generate_test_key_pair():
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=backends.default_backend()
    )
    return private_key, private_key.public_key()


def generate_test_certificate(
    private_key,
    tenancy_id=DEFAULT_TENANCY_ID,
    common_name="Test Instance Certificate",
    validity_days=365
):
    name = x509.Name([
        x509.NameAttribute(
            x509.oid.NameOID.COMMON_NAME,
            common_name
        ),
        x509.NameAttribute(
            x509.oid.NameOID.ORGANIZATIONAL_UNIT_NAME,
            f"opc-tenant:{tenancy_id}"
        ),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .sign(
            private_key,
            primitives.hashes.SHA256(),
            backend=backends.default_backend()
        )
    )

    return cert.public_bytes(
        encoding=primitives.serialization.Encoding.PEM
    ).decode('utf-8')


def generate_intermediate_certificate(
    ca_private_key,
    subject_public_key,
    common_name="Test Intermediate CA",
    validity_days=365
):
    issuer = x509.Name([
        x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Test Root CA"),
    ])

    subject = x509.Name([
        x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, common_name),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(subject_public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .sign(
            ca_private_key,
            primitives.hashes.SHA256(),
            backend=backends.default_backend()
        )
    )

    return cert.public_bytes(
        encoding=primitives.serialization.Encoding.PEM
    ).decode('utf-8')


def key_to_pem(private_key):
    return private_key.private_bytes(
        encoding=primitives.serialization.Encoding.PEM,
        format=primitives.serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=primitives.serialization.NoEncryption()
    ).decode('utf-8')


@pytest.fixture(scope='session')
def crypto_materials():
    instance_private_key, _ = generate_test_key_pair()

    return {
        'identity_key_pem': key_to_pem(instance_private_key),
        'identity_cert_pem': generate_test_certificate(
            instance_private_key,
            tenancy_id=DEFAULT_TENANCY_ID,
            common_name="Instance Identity Certificate"
        ),
        'intermediate_cert_pem': generate_intermediate_certificate(
            generate_test_key_pair()[0],
            generate_test_key_pair()[1],
            common_name="OCI Intermediate CA"
        ),
    }


@pytest.fixture
def credentials(crypto_materials):
    with patch_instance_metadata(crypto_materials):
        yield auth._session_credentials()


@pytest.fixture
def request_signer(crypto_materials):
    with patch_instance_metadata(crypto_materials):
        yield auth.RequestSigner()


def b64urlencode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def create_jwt(payload):
    header = b64urlencode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64urlencode(json.dumps(payload).encode())
    signature = b64urlencode(hmac.new(
        b"secret",
        (header + "." + payload).encode(),
        digestmod="sha256"
    ).digest())
    return f"{header}.{payload}.{signature}"


def create_token(expire):
    return {"token": create_jwt({"exp": expire})}


@pytest.fixture
def token_active():
    return create_token(time.time() + 1800)


@pytest.fixture
def token_next():
    return create_token(time.time() + 3600)


@pytest.fixture
def token_expired():
    return create_token(time.time() - 1800)


def patch_instance_metadata(
    crypto_materials,
    instance_id=DEFAULT_INSTANCE_ID,
    instance_region=DEFAULT_INSTANCE_REGION,
    identity_cert=None,
    intermediate_cert=None,
    identity_key=None,
):
    return mock.patch.multiple(
        "ha_script.oci.metadata",
        get_instance_id=mock.MagicMock(return_value=instance_id),
        get_instance_region=mock.MagicMock(return_value=instance_region),
        get_identity_cert=mock.MagicMock(
            return_value=(
                identity_cert or crypto_materials["identity_cert_pem"]
            ),
        ),
        get_identity_intermediate_cert=mock.MagicMock(
            return_value=(
                intermediate_cert or crypto_materials["intermediate_cert_pem"]
            ),
        ),
        get_identity_key=mock.MagicMock(
            return_value=(
                identity_key or crypto_materials["identity_key_pem"]
            ),
        ),
    )


def test_session_credentials(crypto_materials):
    with patch_instance_metadata(crypto_materials):
        credentials = auth._session_credentials()
        assert credentials["region"] == DEFAULT_INSTANCE_REGION
        assert credentials["identity_cert_pem"] == \
            crypto_materials["identity_cert_pem"]
        assert credentials["identity_key_pem"] == \
            crypto_materials["identity_key_pem"]
        assert credentials["intermediate_cert_pem"] == \
            crypto_materials["intermediate_cert_pem"]
        assert credentials["tenancy_id"] == DEFAULT_TENANCY_ID
        assert credentials["session_key"]
        assert credentials["session_cert_pem"]


def test_parse_token_invalid():
    with pytest.raises(ValueError):
        auth._parse_token("invalid token")

    with pytest.raises(ValueError):
        auth._parse_token({"invalid": "token"})

    with pytest.raises(ValueError):
        auth._parse_token({"token": "invalid"})

    with pytest.raises(ValueError):
        auth._parse_token({"token": create_jwt({"no": "exp"})})


def test_parse_token(token_active, token_expired):
    active = auth._parse_token(token_active)
    assert active
    assert active.value
    assert isinstance(active, auth.Token)
    assert not active.expired()

    expired = auth._parse_token(token_expired)
    assert expired
    assert expired.value
    assert isinstance(expired, auth.Token)
    assert expired.expired()


@responses.activate
def test_token(crypto_materials, token_active, token_next):
    responses.add(responses.POST, DEFAULT_TOKEN_URL, json=token_active)
    responses.add(responses.POST, DEFAULT_TOKEN_URL, json=token_next)
    with patch_instance_metadata(crypto_materials):
        credentials = auth._session_credentials()

        token = auth._request_token(credentials)
        assert token.value == token_active["token"]
        assert not token.expired()

        token2 = auth._request_token(credentials)
        assert token2.value == token_next["token"]
        assert not token2.expired()
        assert token2.expires > token.expires


@responses.activate
def test_token_headers(credentials, token_active):
    responses.add(
        responses.POST,
        DEFAULT_TOKEN_URL,
        json=token_active,
        match=[
            responses.matchers.header_matcher({
                "authorization": re.compile(
                    r'^Signature .*headers="{}".+'.format(
                        " ".join([
                            "date",
                            r"\(request-target\)",
                            "content-length",
                            "content-type",
                            "x-content-sha256",
                        ]),
                    )
                ),
                "date": re.compile(r"^.* GMT$"),
            })
        ],
    )
    token = auth._request_token(credentials)
    assert token.value == token_active["token"]
    assert not token.expired()


@responses.activate
def test_token_retry(credentials, token_active):
    responses.add(responses.POST, DEFAULT_TOKEN_URL, status=500)
    responses.add(responses.POST, DEFAULT_TOKEN_URL, json=token_active)

    token = auth._request_token(credentials)
    assert token.value == token_active["token"]
    assert not token.expired()


@responses.activate
def test_signer_get(request_signer, token_active):
    url = "http://localhost/api"
    responses.add(responses.POST, DEFAULT_TOKEN_URL, json=token_active)
    responses.add(
        responses.GET,
        url,
        json={},
        match=[
            responses.matchers.header_matcher({
                "authorization": re.compile(
                    r'^Signature .*keyId="ST\${}".+'.format(
                        token_active['token']
                    )
                ),
                "date": re.compile(r"^.* GMT$"),
                "host": "localhost",
            })
        ],
    )
    resp = requests.get(url, auth=request_signer)
    assert resp.ok


@responses.activate
def test_signer_post(request_signer, token_active):
    url = "http://localhost/api"
    responses.add(responses.POST, DEFAULT_TOKEN_URL, json=token_active)
    responses.add(
        responses.POST,
        url,
        json={},
        match=[
            responses.matchers.header_matcher({
                "authorization": re.compile(
                    r'^Signature .*keyId="ST\${}".+'.format(
                        token_active['token']
                    )
                ),
                "date": re.compile(r"^.* GMT$"),
                "host": "localhost",
            })
        ],
    )
    resp = requests.post(url, auth=request_signer, data={"foo": "bar"})
    assert resp.ok


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
