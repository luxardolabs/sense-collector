"""An MFA-enabled Sense account logs in headless, through the fake Sense's MFA mode.

The fake answers `/authenticate` with 401 + `mfa_token` and checks `/authenticate/mfa` with
its own RFC 6238 implementation, so these also check the collector's TOTP code against an
independent one.
"""

import threading
import time
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.collector.client import SenseCollector, SenseMFAError, authenticate_with_sense
from app.collector.endpoints import SenseAPIEndpoints
from app.core import config
from harness import fake_sense

SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def fake_base(monkeypatch) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake_sense.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(SenseAPIEndpoints, "AUTHENTICATE", base + "/authenticate")
    monkeypatch.setattr(
        SenseAPIEndpoints, "AUTHENTICATE_MFA", base + "/authenticate/mfa"
    )
    yield base
    server.shutdown()
    server.server_close()


@pytest.fixture
def mfa_account(monkeypatch) -> None:
    monkeypatch.setattr(fake_sense, "MFA_SECRET", SECRET)


def _recording_client() -> tuple[httpx.AsyncClient, list[str]]:
    paths: list[str] = []

    async def record(request: httpx.Request) -> None:
        paths.append(request.url.path)

    return httpx.AsyncClient(event_hooks={"request": [record]}), paths


@pytest.mark.asyncio
@pytest.mark.usefixtures("mfa_account")
async def test_an_mfa_account_logs_in_with_the_totp_secret(fake_base, caplog):
    client, paths = _recording_client()
    async with client:
        payload = await authenticate_with_sense(client, "u@example.com", "pw", SECRET)
    assert payload["access_token"] == "fake-access-token"
    assert payload["monitors"][0]["id"] == fake_sense.MONITOR_ID
    assert paths == ["/authenticate", "/authenticate/mfa"]
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.usefixtures("mfa_account")
async def test_an_mfa_account_without_a_secret_names_the_setting(fake_base):
    client, paths = _recording_client()
    async with client:
        with pytest.raises(SenseMFAError, match="SENSE_COLLECTOR_MFA_SECRET"):
            await authenticate_with_sense(client, "u@example.com", "pw", "")
    assert paths == ["/authenticate"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("mfa_account")
async def test_a_code_from_the_wrong_secret_is_refused(fake_base):
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await authenticate_with_sense(
                client, "u@example.com", "pw", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
            )
    assert caught.value.response.status_code == 401


@pytest.mark.asyncio
async def test_a_password_only_account_ignores_the_secret(fake_base):
    client, paths = _recording_client()
    async with client:
        payload = await authenticate_with_sense(client, "u@example.com", "pw", SECRET)
    assert payload["access_token"] == "fake-access-token"
    assert paths == ["/authenticate"]


@pytest.mark.asyncio
async def test_a_401_without_an_mfa_token_is_a_failed_login():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": "error", "error_reason": "bad pw"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await authenticate_with_sense(client, "u@example.com", "pw", SECRET)


@pytest.mark.asyncio
@pytest.mark.usefixtures("mfa_account")
async def test_the_collector_logs_in_and_renews_through_mfa(fake_base, monkeypatch):
    monkeypatch.setattr(config, "MFA_SECRET", SECRET)
    collector = SenseCollector("u@example.com", "pw", MagicMock())
    with patch.object(collector, "fetch_devices"):
        await collector.authenticate()
        assert collector.access_token == "fake-access-token"
        collector.access_token = "expired"
        await collector._renew("due", min_age=-1)
    assert collector.access_token == "fake-access-token"
    await collector.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("mfa_account")
async def test_mfa_required_without_a_secret_backs_off_a_renewal(
    fake_base, monkeypatch
):
    """A renewal that meets MFA with no secret is a failed login like any other: the old
    token stays and the next attempt waits, so the callers cannot re-POST at will."""
    monkeypatch.setattr(config, "MFA_SECRET", "")
    collector = SenseCollector("u@example.com", "pw", MagicMock())
    await collector.connect()
    collector.access_token, collector.monitor_id = "old-token", "9"
    await collector._renew("due", min_age=-1)
    assert collector.access_token == "old-token"
    assert collector._auth_failures == 1
    assert collector._auth_retry_at > time.monotonic()
    await collector.close()


def test_the_fake_with_a_bad_secret_answers_401_instead_of_dropping(
    fake_base, monkeypatch
):
    monkeypatch.setattr(fake_sense, "MFA_SECRET", "NOT-BASE32!")
    response = httpx.post(
        fake_base + "/authenticate/mfa",
        data={"totp": "123456", "mfa_token": fake_sense.MFA_TOKEN, "client_time": "x"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "client_time", ["not-a-timestamp", "2026-11-04 12:00:00", "2000-01-01T00:00:00Z"]
)
@pytest.mark.usefixtures("mfa_account")
def test_the_fake_refuses_a_malformed_or_stale_client_time(fake_base, client_time):
    """The fake checks client_time the way the login sends it, so a format change fails."""
    response = httpx.post(
        fake_base + "/authenticate/mfa",
        data={
            "totp": fake_sense.totp(SECRET, time.time()),
            "mfa_token": fake_sense.MFA_TOKEN,
            "client_time": client_time,
        },
    )
    assert response.status_code == 401
