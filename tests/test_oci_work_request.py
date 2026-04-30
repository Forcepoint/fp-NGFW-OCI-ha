"""Tests for OCI work request polling."""

from unittest.mock import patch

import pytest
import requests
import responses

from ha_script.oci.api import OCI_API_VERSION, WR_POLL_INTERVAL


HOST = "iaas.us-phoenix-1.oraclecloud.com"
BASE = f"https://{HOST}/{OCI_API_VERSION}"
WR_ID = "ocid1.workrequest.oc1..wr123"
WR_URL = f"{BASE}/workRequests/{WR_ID}"

RT_URL = f"{BASE}/routeTables/rt-1"
RT_PATH = f"/{OCI_API_VERSION}/routeTables/rt-1"


# --- No work request header ---


@responses.activate
def test_wr_no_header_returns_immediately(oci_client):
    """PUT without opc-work-request-id returns immediately."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200)

    result = oci_client.put(RT_PATH, {"foo": "bar"})
    assert result == {"id": "rt-1"}
    assert len(responses.calls) == 1


# --- Work request status polling ---


@responses.activate
def test_wr_succeeds_on_first_poll(oci_client):
    """Work request returns SUCCEEDED on the first poll."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    result = oci_client.put(RT_PATH, {"foo": "bar"})
    assert result == {"id": "rt-1"}
    assert len(responses.calls) == 2


@responses.activate
def test_wr_polls_through_in_progress(oci_client):
    """Polls ACCEPTED -> IN_PROGRESS -> SUCCEEDED."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "ACCEPTED"}, status=200)
    responses.get(WR_URL, json={"status": "IN_PROGRESS"}, status=200)
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    with patch("ha_script.oci.api.time.sleep") as mock_sleep:
        result = oci_client.put(RT_PATH, {"foo": "bar"})

    assert result == {"id": "rt-1"}
    assert len(responses.calls) == 4
    assert mock_sleep.call_count == 2


@responses.activate
def test_wr_failed_raises(oci_client):
    """FAILED status raises HTTPError."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "FAILED"}, status=200)

    with pytest.raises(requests.HTTPError, match="FAILED"):
        oci_client.put(RT_PATH, {"foo": "bar"})


@responses.activate
def test_wr_canceled_raises(oci_client):
    """CANCELED status raises HTTPError."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "CANCELED"}, status=200)

    with pytest.raises(requests.HTTPError, match="CANCELED"):
        oci_client.put(RT_PATH, {"foo": "bar"})


# --- Retry-After / interval ---


@responses.activate
def test_wr_respects_retry_after(oci_client):
    """Uses Retry-After header value as sleep interval."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "IN_PROGRESS"}, status=200,
                  headers={"Retry-After": "5"})
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    with patch("ha_script.oci.api.time.sleep") as mock_sleep:
        oci_client.put(RT_PATH, {"foo": "bar"})

    mock_sleep.assert_called_once_with(5)


@responses.activate
def test_wr_default_interval(oci_client):
    """Falls back to WR_POLL_INTERVAL when no Retry-After."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "IN_PROGRESS"}, status=200)
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    with patch("ha_script.oci.api.time.sleep") as mock_sleep:
        oci_client.put(RT_PATH, {"foo": "bar"})

    mock_sleep.assert_called_once_with(WR_POLL_INTERVAL)


@responses.activate
def test_wr_invalid_retry_after_falls_back_to_default(oci_client):
    """Non-integer Retry-After falls back to WR_POLL_INTERVAL."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "IN_PROGRESS"}, status=200,
                  headers={"Retry-After": "Wed, 28 Apr 2026 07:28:00 GMT"})
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    with patch("ha_script.oci.api.time.sleep") as mock_sleep:
        oci_client.put(RT_PATH, {"foo": "bar"})

    mock_sleep.assert_called_once_with(WR_POLL_INTERVAL)


# --- Timeout ---


@responses.activate
def test_wr_timeout_raises(oci_client):
    """Raises HTTPError when polling exceeds WR_TIMEOUT."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "IN_PROGRESS"}, status=200)

    with patch("ha_script.oci.api.time.sleep"), \
         patch("ha_script.oci.api.time.monotonic") as mock_mono:
        mock_mono.side_effect = [0.0, 0.0, 200.0]
        with pytest.raises(requests.HTTPError, match="timed out"):
            oci_client.put(RT_PATH, {"foo": "bar"})


# --- 401 retry tests ---


@responses.activate
def test_wr_poll_retries_on_401(oci_client):
    """401 during poll triggers token refresh and immediate retry."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"error": "unauth"}, status=401)
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    oci_client.put(RT_PATH, {"foo": "bar"})
    oci_client.request_signer.invalidate.assert_called_once()
    assert len(responses.calls) == 3


@responses.activate
def test_wr_poll_double_401_raises(oci_client):
    """401 on both poll attempts raises HTTPError."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"error": "unauth"}, status=401)
    responses.get(WR_URL, json={"error": "unauth"}, status=401)

    with pytest.raises(requests.HTTPError):
        oci_client.put(RT_PATH, {"foo": "bar"})


# --- Poll error / method tests ---


@responses.activate
def test_wr_poll_error_raises(oci_client):
    """A non-retryable error (403) during polling raises HTTPError."""
    responses.put(RT_URL, json={"id": "rt-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, status=403)

    with pytest.raises(requests.HTTPError):
        oci_client.put(RT_PATH, {"foo": "bar"})


@responses.activate
def test_wr_delete_triggers_polling(oci_client):
    """DELETE with work request header triggers polling."""
    url = f"{BASE}/publicIps/pip-1"
    responses.delete(url, body=b"", status=200,
                     headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    oci_client.delete(f"/{OCI_API_VERSION}/publicIps/pip-1")
    assert len(responses.calls) == 2


@responses.activate
def test_wr_post_triggers_polling(oci_client):
    """POST with work request header triggers polling."""
    url = f"{BASE}/clusters"
    responses.post(url, json={"id": "c-1"}, status=200,
                   headers={"opc-work-request-id": WR_ID})
    responses.get(WR_URL, json={"status": "SUCCEEDED"}, status=200)

    result = oci_client.post(f"/{OCI_API_VERSION}/clusters", {"name": "test"})
    assert result == {"id": "c-1"}
    assert len(responses.calls) == 2


@responses.activate
def test_wr_get_does_not_poll(oci_client):
    """GET never triggers work request polling even with header."""
    url = f"{BASE}/instances/i-1"
    responses.get(url, json={"id": "i-1"}, status=200,
                  headers={"opc-work-request-id": WR_ID})

    result = oci_client.get(f"/{OCI_API_VERSION}/instances/i-1")
    assert result == {"id": "i-1"}
    assert len(responses.calls) == 1
