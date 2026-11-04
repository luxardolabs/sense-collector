"""An MFA-enabled Sense account logs in headless, through the fake Sense's MFA mode.

The fake answers `/authenticate` with 401 + `mfa_token` and checks `/authenticate/mfa` with
its own RFC 6238 implementation, so these also check the collector's TOTP code against an
independent one.
"""

import threading
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
