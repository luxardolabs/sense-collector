"""Tests for the SenseCollector — auth, the device cache, dispatch and the API worker.

These exercise real behaviour rather than mock choreography: the cache is driven through
its TTL and LRU eviction, dispatch is asserted by which handler actually ran, and the
narrowed exception boundaries are pinned both ways (an expected failure is absorbed, a
collector bug still propagates).
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collector.client import (
    DeviceCache,
    SenseCollector,
    authenticate_with_sense,
)
from app.collector.endpoints import SenseAPIEndpoints

AUTH_RESPONSE = {
    "access_token": "tok-abc",
    "user_id": 4242,
    "monitors": [{"id": 12345}],
}


@pytest.fixture
def storage():
    """An InfluxDBStorage stand-in: every persist_* is awaited and recorded."""
    s = MagicMock()
    s.persist_realtime_data = AsyncMock()
    s.persist_device_data = AsyncMock()
    s.persist_monitor_status = AsyncMock()
    s.persist_timeline_data = AsyncMock()
    s.persist_device_state = AsyncMock()
    s.persist_data_change = AsyncMock()
    s.persist_hello_event = AsyncMock()
    s.device_queue = MagicMock()
    s.device_queue.set_device_name = AsyncMock()
    return s


@pytest.fixture
def collector(storage):
    return SenseCollector("user@example.com", "pw", storage)


# --------------------------------------------------------------------------- DeviceCache


class TestDeviceCache:
    @pytest.mark.asyncio
    async def test_miss_then_hit(self):
        cache = DeviceCache()
        assert await cache.get("dev-1") is None
        await cache.put("dev-1", {"name": "Fridge"})
        assert await cache.get("dev-1") == {"name": "Fridge"}

    @pytest.mark.asyncio
    async def test_entry_expires_after_ttl(self):
        """An entry past its TTL is a miss AND is dropped, not merely hidden."""
        cache = DeviceCache(ttl_seconds=60)
        await cache.put("dev-1", {"name": "Fridge"})
        # Advance the clock rather than sleeping — the TTL is read from time.time().
        with patch("app.collector.client.time.time", return_value=time.time() + 61):
            assert await cache.get("dev-1") is None
        assert "dev-1" not in cache.cache

    @pytest.mark.asyncio
    async def test_eviction_drops_the_oldest_at_capacity(self):
        cache = DeviceCache(max_size=2)
        await cache.put("a", 1)
        await cache.put("b", 2)
        await cache.put("c", 3)
        assert await cache.get("a") is None
        assert await cache.get("b") == 2
        assert await cache.get("c") == 3

    @pytest.mark.asyncio
    async def test_a_read_refreshes_lru_order(self):
        """Reading 'a' must make 'b' the eviction victim, not 'a'."""
        cache = DeviceCache(max_size=2)
        await cache.put("a", 1)
        await cache.put("b", 2)
        await cache.get("a")
        await cache.put("c", 3)
        assert await cache.get("a") == 1
        assert await cache.get("b") is None

    @pytest.mark.asyncio
    async def test_put_is_idempotent_for_one_id(self):
        """Re-putting the same id updates in place — it must not consume capacity twice."""
        cache = DeviceCache(max_size=2)
        await cache.put("a", 1)
        await cache.put("a", 2)
        await cache.put("b", 3)
        assert await cache.get("a") == 2
        assert await cache.get("b") == 3

    @pytest.mark.asyncio
    async def test_clear_empties_cache_and_order(self):
        cache = DeviceCache()
        await cache.put("a", 1)
        await cache.clear()
        assert await cache.get("a") is None
        assert len(cache.access_order) == 0


# --------------------------------------------------------------- authenticate_with_sense


class TestAuthenticateWithSense:
    @pytest.mark.asyncio
    async def test_posts_credentials_and_returns_payload(self):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value=AUTH_RESPONSE)
        client = MagicMock()
        client.post = AsyncMock(return_value=response)

        result = await authenticate_with_sense(client, "user@example.com", "pw")

        assert result == AUTH_RESPONSE
        _, kwargs = client.post.call_args
        assert kwargs["data"] == {"email": "user@example.com", "password": "pw"}

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        """Auth failure must NOT be swallowed — the collector cannot run unauthenticated."""
        client = MagicMock()
        client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(httpx.HTTPError):
            await authenticate_with_sense(client, "user@example.com", "pw")


# ------------------------------------------------------------------ lifecycle + auth


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self, collector):
        """A second connect() must not leak a second connection pool."""
        await collector.connect()
        first = collector.client
        await collector.connect()
        assert collector.client is first
        await collector.close()

    @pytest.mark.asyncio
    async def test_close_clears_the_client(self, collector):
        await collector.connect()
        await collector.close()
        assert collector.client is None

    @pytest.mark.asyncio
    async def test_authenticate_populates_identity_and_headers(self, collector):
        with patch(
            "app.collector.client.authenticate_with_sense",
            AsyncMock(return_value=AUTH_RESPONSE),
        ):
            await collector.authenticate()

        assert collector.access_token == "tok-abc"
        assert collector.user_id == 4242
        assert collector.monitor_id == 12345
        assert collector.headers["Authorization"] == "Bearer tok-abc"
        assert collector.headers["X-Sense-Monitor-Id"] == "12345"
        assert collector.auth_time is not None
        await collector.close()

    @pytest.mark.asyncio
    async def test_authenticate_reraises(self, collector):
        with (
            patch(
                "app.collector.client.authenticate_with_sense",
                AsyncMock(side_effect=httpx.ConnectError("refused")),
            ),
            pytest.raises(httpx.HTTPError),
        ):
            await collector.authenticate()
        await collector.close()

    @pytest.mark.asyncio
    async def test_token_renewal_is_skipped_before_the_interval(self, collector):
        collector.auth_time = None
        with patch.object(collector, "authenticate", AsyncMock()) as auth:
            await collector.check_token_renewal()
            auth.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_renewal_fires_once_expired(self, collector):
        from datetime import UTC, datetime, timedelta

        from app.core import config

        collector.auth_time = datetime.now(UTC) - timedelta(
            seconds=config.TOKEN_RENEW_INTERVAL + 60
        )
        with patch.object(collector, "authenticate", AsyncMock()) as auth:
            await collector.check_token_renewal()
            auth.assert_awaited_once()


class TestShutdownSignalling:
    def test_shutdown_requested_tracks_flag_and_event(self, collector):
        assert collector._shutdown_requested() is False
        collector.shutdown_event = asyncio.Event()
        assert collector._shutdown_requested() is False
        collector.shutdown_event.set()
        assert collector._shutdown_requested() is True

    @pytest.mark.asyncio
    async def test_interruptible_sleep_returns_early_on_shutdown(self, collector):
        """A 60s sleep must not delay shutdown — the event wakes it immediately."""
        collector.shutdown_event = asyncio.Event()
        collector.shutdown_event.set()
        start = time.monotonic()
        await collector._interruptible_sleep(60)
        assert time.monotonic() - start < 1

    @pytest.mark.asyncio
    async def test_interruptible_sleep_waits_out_its_timeout(self, collector):
        collector.shutdown_event = asyncio.Event()
        await collector._interruptible_sleep(0.01)
        assert not collector.shutdown_event.is_set()


# ------------------------------------------------------------------ WebSocket dispatch


class TestProcessWebSocketData:
    @pytest.mark.parametrize(
        ("message_type", "handler"),
        [
            ("realtime_update", "handle_realtime_update"),
            ("new_timeline_event", "handle_timeline_event"),
            ("device_states_changed", "handle_device_state_change"),
            ("data_change", "handle_data_change"),
            ("device_states", "handle_device_states"),
            ("hello", "handle_hello"),
        ],
    )
    @pytest.mark.asyncio
    async def test_routes_each_message_type(self, collector, message_type, handler):
        payload = {"marker": message_type}
        with patch.object(collector, handler, AsyncMock()) as h:
            await collector.process_websocket_data(
                {"type": message_type, "payload": payload}
            )
        h.assert_awaited_once_with(payload)

    @pytest.mark.asyncio
    async def test_unknown_type_is_ignored_quietly(self, collector, storage):
        await collector.process_websocket_data({"type": "who_knows", "payload": {}})
        storage.persist_realtime_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_frame_touches_the_heartbeat(self, collector):
        """The healthcheck reads this file — a delivering WS must keep it fresh."""
        with patch.object(collector, "_touch_heartbeat") as beat:
            await collector.process_websocket_data({"type": "hello", "payload": {}})
        beat.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_failing_handler_does_not_kill_the_reader(self, collector):
        """The outermost net: one bad frame must not take down the WS reader."""
        with patch.object(
            collector, "handle_hello", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await collector.process_websocket_data({"type": "hello", "payload": {}})


class TestHeartbeat:
    def test_touch_is_throttled(self, collector):
        with patch("app.collector.client.Path") as path:
            collector._touch_heartbeat()
            collector._touch_heartbeat()
        assert path.return_value.touch.call_count == 1

    def test_an_unwritable_heartbeat_is_survivable(self, collector):
        """Losing the heartbeat file must not break message processing."""
        with patch("app.collector.client.Path") as path:
            path.return_value.touch.side_effect = OSError("read-only fs")
            collector._touch_heartbeat()


# ------------------------------------------------------------------ payload handlers


class TestHandleRealtimeUpdate:
    @pytest.mark.asyncio
    async def test_persists_a_complete_payload(self, collector, storage):
        collector.monitor_id = 12345
        await collector.handle_realtime_update(
            {
                "hz": 60.0,
                "c": 10.5,
                "w": 1260.0,
                "epoch": 1700000000,
                "voltage": [120.1, 120.3],
                "channels": [630.0, 630.0],
                "devices": [{"id": "ac", "w": 500}],
            }
        )
        storage.persist_realtime_data.assert_awaited_once()
        kwargs = storage.persist_realtime_data.call_args.kwargs
        assert kwargs["monitor_id"] == 12345
        assert kwargs["total_watts"] == 1260.0
        assert kwargs["hertz"] == 60.0

    @pytest.mark.parametrize("missing", ["hz", "c", "w", "epoch"])
    @pytest.mark.asyncio
    async def test_incomplete_payload_is_rejected_before_persisting(
        self, collector, storage, missing
    ):
        payload = {"hz": 60.0, "c": 10.5, "w": 1260.0, "epoch": 1700000000}
        del payload[missing]
        await collector.handle_realtime_update(payload)
        storage.persist_realtime_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_counts_every_update(self, collector):
        payload = {"hz": 60.0, "c": 1.0, "w": 120.0, "epoch": 1}
        await collector.handle_realtime_update(payload)
        await collector.handle_realtime_update(payload)
        assert collector.activity_stats["realtime_updates"] == 2

    @pytest.mark.asyncio
    async def test_a_storage_failure_is_absorbed(self, collector, storage):
        """A transient InfluxDB failure must not drop the WS connection."""
        storage.persist_realtime_data.side_effect = ValueError("bad point")
        await collector.handle_realtime_update(
            {"hz": 60.0, "c": 1.0, "w": 120.0, "epoch": 1}
        )

    @pytest.mark.asyncio
    async def test_a_collector_bug_still_propagates(self, collector, storage):
        """The narrowed catch must NOT hide an AttributeError — that is our own defect."""
        storage.persist_realtime_data.side_effect = AttributeError("renamed method")
        with pytest.raises(AttributeError):
            await collector.handle_realtime_update(
                {"hz": 60.0, "c": 1.0, "w": 120.0, "epoch": 1}
            )


class TestHandleHello:
    @pytest.mark.asyncio
    async def test_persists_online_state(self, collector, storage):
        collector.monitor_id = 12345
        await collector.handle_hello({"online": True, "timestamp": 1700000000})
        storage.persist_hello_event.assert_awaited_once()


class TestHandleDeviceStates:
    @pytest.mark.asyncio
    async def test_persists_each_state_and_counts_them(self, collector, storage):
        collector.monitor_id = 12345
        await collector.handle_device_states(
            {
                "states": [
                    {"device_id": "ac", "mode": "on", "state": "active"},
                    {"device_id": "fridge", "mode": "off", "state": "idle"},
                ]
            }
        )
        assert storage.persist_device_state.await_count == 2
        assert collector.activity_stats["device_state_changes"] == 2

    @pytest.mark.asyncio
    async def test_one_bad_state_does_not_stop_the_rest(self, collector, storage):
        """Per-device isolation: a malformed entry must not drop its siblings."""
        storage.persist_device_state.side_effect = [ValueError("bad"), None]
        await collector.handle_device_states(
            {
                "states": [
                    {"device_id": "ac", "mode": "on", "state": "active"},
                    {"device_id": "fridge", "mode": "off", "state": "idle"},
                ]
            }
        )
        assert storage.persist_device_state.await_count == 2


class TestHandleTimelineEvent:
    @pytest.mark.asyncio
    async def test_processes_each_item_and_queues_a_device_fetch(self, collector):
        with patch.object(collector, "process_timeline_item", AsyncMock()) as item:
            await collector.handle_timeline_event(
                {
                    "items_added": [
                        {"device_id": "ac", "type": "on"},
                        {"device_id": "fridge", "type": "off"},
                    ]
                }
            )
        assert item.await_count == 2
        assert collector.activity_stats["timeline_events"] == 2
        # each item also queues a device-detail lookup for the API workers
        assert collector.api_call_queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_an_item_without_a_device_id_is_skipped(self, collector):
        with patch.object(collector, "process_timeline_item", AsyncMock()) as item:
            await collector.handle_timeline_event({"items_added": [{"type": "on"}]})
        item.assert_not_called()
        assert collector.api_call_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_a_full_queue_drops_the_fetch_without_raising(self, collector):
        """Back-pressure: a saturated queue must degrade, not crash the reader."""
        collector.api_call_queue = asyncio.Queue(maxsize=1)
        collector.api_call_queue.put_nowait({"device_id": "filler"})
        with patch.object(collector, "process_timeline_item", AsyncMock()):
            await collector.handle_timeline_event(
                {"items_added": [{"device_id": "ac", "type": "on"}]}
            )

    @pytest.mark.asyncio
    async def test_empty_timeline_is_a_no_op(self, collector):
        with patch.object(collector, "process_timeline_item", AsyncMock()) as item:
            await collector.handle_timeline_event({})
        item.assert_not_called()


class TestProcessTimelineItem:
    @pytest.mark.asyncio
    async def test_prefers_the_cached_device_name_and_icon(self, collector, storage):
        await collector.device_cache.put(
            "ac", {"device": {"name": "AC Unit", "icon": "ac"}}
        )
        await collector.process_timeline_item({"device_id": "ac", "type": "on"})
        kwargs = storage.persist_timeline_data.call_args.kwargs
        assert kwargs["device_name"] == "AC Unit"
        assert kwargs["icon"] == "ac"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_id_when_uncached(self, collector, storage):
        await collector.process_timeline_item({"device_id": "ac", "icon": "bolt"})
        kwargs = storage.persist_timeline_data.call_args.kwargs
        assert kwargs["device_name"] == "ac"
        assert kwargs["icon"] == "bolt"


# ------------------------------------------------------------------ fetch + API worker


class TestFetchDevices:
    @pytest.mark.asyncio
    async def test_caches_every_device_name_from_a_bare_list(self, collector, storage):
        collector.monitor_id = 12345
        devices = [{"id": "ac", "name": "AC Unit"}, {"id": "fridge", "name": "Fridge"}]
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(
                collector, "make_api_request", AsyncMock(return_value=devices)
            ),
        ):
            await collector.fetch_devices()
        assert storage.device_queue.set_device_name.await_count == 2
        storage.device_queue.set_device_name.assert_any_await("ac", "AC Unit")

    @pytest.mark.asyncio
    async def test_accepts_the_dict_shaped_response_too(self, collector, storage):
        collector.monitor_id = 12345
        payload = {"devices": [{"id": "ac", "name": "AC Unit"}]}
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(
                collector, "make_api_request", AsyncMock(return_value=payload)
            ),
        ):
            await collector.fetch_devices()
        storage.device_queue.set_device_name.assert_any_await("ac", "AC Unit")

    @pytest.mark.asyncio
    async def test_a_device_with_no_id_is_skipped(self, collector, storage):
        collector.monitor_id = 12345
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(
                collector, "make_api_request", AsyncMock(return_value=[{"name": "X"}])
            ),
        ):
            await collector.fetch_devices()
        storage.device_queue.set_device_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_response_is_survivable(self, collector, storage):
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(collector, "make_api_request", AsyncMock(return_value=None)),
        ):
            await collector.fetch_devices()
        storage.device_queue.set_device_name.assert_not_called()


class TestFetchMonitorStatus:
    @pytest.mark.asyncio
    async def test_persists_a_dict_response(self, collector, storage):
        collector.monitor_id = 12345
        status = {"signals": {"status": "OK"}}
        with patch.object(
            collector, "make_api_request", AsyncMock(return_value=status)
        ):
            await collector.fetch_monitor_status()
        storage.persist_monitor_status.assert_awaited_once_with(12345, status)

    @pytest.mark.asyncio
    async def test_a_non_dict_response_is_not_persisted(self, collector, storage):
        with patch.object(collector, "make_api_request", AsyncMock(return_value=[])):
            await collector.fetch_monitor_status()
        storage.persist_monitor_status.assert_not_called()


class TestApiWorker:
    @pytest.mark.asyncio
    async def test_a_cache_hit_skips_the_api(self, collector, storage):
        collector.monitor_id = 12345
        await collector.device_cache.put("ac", {"id": "ac"})
        await collector.api_call_queue.put({"device_id": "ac"})
        await collector.api_call_queue.put(None)  # shutdown sentinel

        with patch.object(collector, "fetch_device_data", AsyncMock()) as fetch:
            await collector.api_worker(0)

        fetch.assert_not_called()
        storage.persist_device_data.assert_awaited_once_with(12345, {"id": "ac"})

    @pytest.mark.asyncio
    async def test_a_cache_miss_fetches_from_the_api(self, collector):
        await collector.api_call_queue.put({"device_id": "unseen"})
        await collector.api_call_queue.put(None)

        with patch.object(collector, "fetch_device_data", AsyncMock()) as fetch:
            await collector.api_worker(0)

        fetch.assert_awaited_once_with("unseen")

    @pytest.mark.asyncio
    async def test_the_none_sentinel_stops_the_worker(self, collector):
        await collector.api_call_queue.put(None)
        await asyncio.wait_for(collector.api_worker(0), timeout=5)


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_sets_the_flag_and_closes_the_client(self, collector):
        await collector.connect()
        await collector.shutdown()
        assert collector.is_shutting_down is True


# ------------------------------------------------------------------------- endpoints


class TestEndpoints:
    def test_templates_format_with_ids(self):
        url = SenseAPIEndpoints.DEVICE_DETAILS.format(monitor_id=1, device_id="ac")
        assert url.endswith("/app/monitors/1/devices/ac")

    def test_websocket_url_carries_monitor_and_token(self):
        url = SenseAPIEndpoints.WEBSOCKET.format(monitor_id=1, access_token="t")
        assert "/monitors/1/realtimefeed" in url
        assert "access_token=t" in url

    def test_every_rest_endpoint_shares_the_base_url(self):
        for url in (
            SenseAPIEndpoints.AUTHENTICATE,
            SenseAPIEndpoints.DEVICES,
            SenseAPIEndpoints.MONITOR_STATUS,
            SenseAPIEndpoints.TIMELINE,
        ):
            assert url.startswith(SenseAPIEndpoints.BASE_URL)


# ------------------------------------------------------------------ make_api_request


def _response(status=200, json_body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json = MagicMock(return_value=json_body if json_body is not None else {})
    r.raise_for_status = MagicMock()
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status}", request=MagicMock(), response=r
        )
    return r


class TestMakeApiRequest:
    @pytest.mark.asyncio
    async def test_returns_the_decoded_payload(self, collector):
        collector.client = MagicMock()
        collector.client.get = AsyncMock(return_value=_response(json_body={"ok": True}))
        collector.min_api_interval = 0
        assert await collector.make_api_request("http://x/y") == {"ok": True}

    @pytest.mark.asyncio
    async def test_no_client_returns_none_instead_of_raising(self, collector):
        collector.client = None
        collector.min_api_interval = 0
        assert await collector.make_api_request("http://x/y") is None

    @pytest.mark.asyncio
    async def test_429_honours_retry_after_then_succeeds(self, collector):
        """A rate-limit response must be retried, not surfaced as a failure."""
        collector.client = MagicMock()
        collector.client.get = AsyncMock(
            side_effect=[
                _response(status=429, headers={"Retry-After": "0"}),
                _response(json_body={"ok": True}),
            ]
        )
        collector.min_api_interval = 0
        with patch("app.collector.client.asyncio.sleep", AsyncMock()):
            assert await collector.make_api_request("http://x/y") == {"ok": True}
        assert collector.client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_401_triggers_reauthentication_and_retries(self, collector):
        collector.client = MagicMock()
        collector.client.get = AsyncMock(
            side_effect=[_response(status=401), _response(json_body={"ok": True})]
        )
        collector.min_api_interval = 0
        with (
            patch.object(collector, "authenticate", AsyncMock()) as auth,
            patch("app.collector.client.asyncio.sleep", AsyncMock()),
        ):
            assert await collector.make_api_request("http://x/y") == {"ok": True}
        auth.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self, collector):
        collector.client = MagicMock()
        collector.client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        collector.min_api_interval = 0
        with patch("app.collector.client.asyncio.sleep", AsyncMock()):
            assert await collector.make_api_request("http://x/y", max_retries=2) is None
        assert collector.client.get.await_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_respects_the_minimum_interval_between_calls(self, collector):
        """Sense rate-limits hard — consecutive calls must be spaced."""
        collector.client = MagicMock()
        collector.client.get = AsyncMock(return_value=_response(json_body={}))
        collector.min_api_interval = 5
        collector.last_api_call_time = time.time()
        with patch("app.collector.client.asyncio.sleep", AsyncMock()) as sleep:
            await collector.make_api_request("http://x/y")
        sleep.assert_awaited_once()
        assert sleep.await_args.args[0] > 0


# ------------------------------------------------------------------ fetch_device_data


class TestFetchDeviceData:
    @pytest.mark.asyncio
    async def test_caches_and_persists_a_dict_response(self, collector, storage):
        collector.monitor_id = 12345
        detail = {"device": {"id": "ac", "name": "AC Unit"}}
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(collector, "make_api_request", AsyncMock(return_value=detail)),
        ):
            await collector.fetch_device_data("ac")
        assert await collector.device_cache.get("ac") == detail
        storage.persist_device_data.assert_awaited_once_with(12345, detail)

    @pytest.mark.asyncio
    async def test_a_non_dict_response_is_neither_cached_nor_persisted(
        self, collector, storage
    ):
        with (
            patch.object(collector, "check_token_renewal", AsyncMock()),
            patch.object(collector, "make_api_request", AsyncMock(return_value=None)),
        ):
            await collector.fetch_device_data("ac")
        assert await collector.device_cache.get("ac") is None
        storage.persist_device_data.assert_not_called()


class TestExports:
    @pytest.mark.asyncio
    async def test_device_list_is_written_as_json(self, collector, tmp_path):
        target = tmp_path / "devices.json"
        with patch(
            "app.collector.client.FilePathValidator.get_safe_export_path",
            return_value=str(target),
        ):
            await collector.export_device_list([{"id": "ac"}])
        assert json.loads(target.read_text()) == [{"id": "ac"}]

    @pytest.mark.asyncio
    async def test_device_data_is_written_as_json(self, collector, tmp_path):
        target = tmp_path / "ac.json"
        with patch(
            "app.collector.client.FilePathValidator.get_safe_export_path",
            return_value=str(target),
        ):
            await collector.export_device_data("ac", {"id": "ac"})
        assert json.loads(target.read_text()) == {"id": "ac"}

    @pytest.mark.asyncio
    async def test_a_rejected_path_writes_nothing(self, collector):
        """A path the validator refuses must not fall back to writing somewhere else."""
        with patch(
            "app.collector.client.FilePathValidator.get_safe_export_path",
            return_value=None,
        ):
            await collector.export_device_data("../escape", {"id": "x"})


# ------------------------------------------------------------------ handle_data_change


class TestHandleDataChange:
    @pytest.mark.asyncio
    async def test_extracts_version_from_a_dict(self, collector, storage):
        storage.persist_data_change_event = AsyncMock()
        collector.monitor_id = 12345
        await collector.handle_data_change(
            {"device_id": "ac", "user_version": {"version": 7}, "guid": "g1"}
        )
        kwargs = storage.persist_data_change_event.call_args.kwargs
        assert kwargs["user_version"] == 7
        assert kwargs["device_id"] == "ac"

    @pytest.mark.asyncio
    async def test_preserves_a_primitive_version(self, collector, storage):
        storage.persist_data_change_event = AsyncMock()
        await collector.handle_data_change({"device_id": "ac", "user_version": 3})
        assert storage.persist_data_change_event.call_args.kwargs["user_version"] == 3

    @pytest.mark.asyncio
    async def test_parses_the_timestamp_to_epoch(self, collector, storage):
        storage.persist_data_change_event = AsyncMock()
        await collector.handle_data_change(
            {"device_id": "ac", "timestamp": "2026-01-01T00:00:00.000Z"}
        )
        assert (
            storage.persist_data_change_event.call_args.kwargs["epoch_timestamp"]
            == 1767225600
        )

    @pytest.mark.asyncio
    async def test_an_unparseable_timestamp_is_none_not_a_crash(
        self, collector, storage
    ):
        storage.persist_data_change_event = AsyncMock()
        await collector.handle_data_change(
            {"device_id": "ac", "timestamp": "not-a-timestamp"}
        )
        assert (
            storage.persist_data_change_event.call_args.kwargs["epoch_timestamp"]
            is None
        )

    @pytest.mark.asyncio
    async def test_counts_each_change(self, collector, storage):
        storage.persist_data_change_event = AsyncMock()
        await collector.handle_data_change({"device_id": "ac"})
        assert collector.activity_stats["data_changes"] == 1


# ------------------------------------------------------------------ periodic loops


class TestPeriodicLoops:
    @pytest.mark.asyncio
    async def test_monitor_status_loop_exits_on_shutdown(self, collector):
        """A signalled shutdown must end the loop rather than sleeping out its interval."""
        collector.shutdown_event = asyncio.Event()
        collector.shutdown_event.set()
        await asyncio.wait_for(collector.periodic_monitor_status(), timeout=5)

    @pytest.mark.asyncio
    async def test_monitor_status_loop_fetches_when_due(self, collector):
        collector.shutdown_event = asyncio.Event()
        collector.last_monitor_status_time = 0  # long overdue

        async def stop_after_first_call():
            collector.shutdown_event.set()

        with patch.object(
            collector,
            "fetch_monitor_status",
            AsyncMock(side_effect=stop_after_first_call),
        ) as fetch:
            await asyncio.wait_for(collector.periodic_monitor_status(), timeout=5)
        fetch.assert_awaited()

    @pytest.mark.asyncio
    async def test_device_fetch_loop_exits_on_shutdown(self, collector):
        collector.shutdown_event = asyncio.Event()
        collector.shutdown_event.set()
        await asyncio.wait_for(collector.periodic_device_fetch(), timeout=5)

    @pytest.mark.asyncio
    async def test_device_fetch_loop_survives_a_fetch_failure(self, collector):
        """A failing poll must not kill the loop — it retries on the next tick."""
        collector.shutdown_event = asyncio.Event()
        collector.last_device_list_time = 0

        async def fail_then_stop():
            collector.shutdown_event.set()
            raise RuntimeError("transient")

        with patch.object(
            collector, "fetch_devices", AsyncMock(side_effect=fail_then_stop)
        ):
            await asyncio.wait_for(collector.periodic_device_fetch(), timeout=5)


class TestPeriodicSummary:
    @pytest.mark.asyncio
    async def test_summary_is_throttled(self, collector, storage):
        storage.write_stats = {"successful": 1, "failed": 0}
        collector.activity_stats["last_summary_time"] = time.time()
        collector.activity_stats["realtime_updates"] = 5
        await collector._log_periodic_summary()
        assert collector.activity_stats["realtime_updates"] == 5  # not reset

    @pytest.mark.asyncio
    async def test_summary_resets_counters_once_due(self, collector, storage):
        storage.write_stats = {"successful": 1, "failed": 0}
        collector.activity_stats["last_summary_time"] = time.time() - 301
        collector.activity_stats["realtime_updates"] = 5
        await collector._log_periodic_summary()
        assert collector.activity_stats["realtime_updates"] == 0
