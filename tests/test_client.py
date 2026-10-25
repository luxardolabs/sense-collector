"""Tests for the SenseCollector — auth, the device cache, dispatch and the API worker.

These exercise real behaviour rather than mock choreography: the cache is driven through
its TTL and LRU eviction, dispatch is asserted by which handler actually ran, and the
narrowed exception boundaries are pinned both ways (an expected failure is absorbed, a
collector bug still propagates).
"""

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.collector import client as client_mod
from app.collector.client import (
    DeviceCache,
    SenseCollector,
    authenticate_with_sense,
)
from app.collector.endpoints import SenseAPIEndpoints
from app.core import config

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
    s.device_queue.get_device_name = AsyncMock(return_value=None)
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
    async def test_a_handled_frame_touches_the_heartbeat(self, collector):
        """The healthcheck reads this file — a delivering WS must keep it fresh."""
        with patch.object(collector, "_touch_heartbeat") as beat:
            await collector.process_websocket_data({"type": "hello", "payload": {}})
        beat.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_written_realtime_frame_touches_the_heartbeat(self, collector):
        frame = {
            "type": "realtime_update",
            "payload": {"hz": 60.0, "c": 1.0, "w": 100.0, "epoch": 1_760_000_000},
        }
        with patch.object(collector, "_touch_heartbeat") as beat:
            await collector.process_websocket_data(frame)
        beat.assert_called_once()

    @pytest.mark.parametrize(
        "frame",
        [
            {"type": "realtime_update", "payload": {"hz": 60.0, "watts": 100.0}},
            {"type": "realtime_v2", "payload": {}},
            [],
            None,
        ],
        ids=["drifted-key", "renamed-type", "list", "null"],
    )
    @pytest.mark.asyncio
    async def test_a_frame_the_collector_cannot_use_does_not_keep_it_healthy(
        self, collector, frame
    ):
        """If Sense renames a key or a type, every frame is rejected and nothing is
        written: the collector itself has stopped doing its job and must go unhealthy."""
        with patch.object(collector, "_touch_heartbeat") as beat:
            await collector.process_websocket_data(frame)
        beat.assert_not_called()

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

    @pytest.mark.asyncio
    async def test_uses_the_device_list_name_when_the_detail_cache_misses(
        self, collector, storage
    ):
        """fetch_devices names every device hourly; a detail-cache miss must not hold the
        event back waiting for a name the collector already has."""
        storage.device_queue.get_device_name = AsyncMock(return_value="AC Unit")
        await collector.process_timeline_item({"device_id": "ac"})
        assert (
            storage.persist_timeline_data.call_args.kwargs["device_name"] == "AC Unit"
        )

    @pytest.mark.asyncio
    async def test_uses_the_events_own_device_name_before_the_id(
        self, collector, storage
    ):
        await collector.process_timeline_item({"device_id": "ac", "device_name": "AC"})
        assert storage.persist_timeline_data.call_args.kwargs["device_name"] == "AC"

    @pytest.mark.asyncio
    async def test_a_non_string_event_name_falls_back_to_the_id(
        self, collector, storage
    ):
        await collector.process_timeline_item(
            {"device_id": "ac", "device_name": {"x": 1}}
        )
        assert storage.persist_timeline_data.call_args.kwargs["device_name"] == "ac"


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
    async def test_a_cache_hit_skips_the_api_and_writes_nothing_stale(
        self, collector, storage
    ):
        """The cached detail was written when it was fetched; writing it again stamped
        values up to 15 minutes old as if they had just been observed."""
        collector.monitor_id = 12345
        await collector.device_cache.put("ac", {"id": "ac"})
        await collector.api_call_queue.put({"device_id": "ac"})
        await collector.api_call_queue.put(None)  # shutdown sentinel

        with patch.object(collector, "fetch_device_data", AsyncMock()) as fetch:
            await collector.api_worker(0)

        fetch.assert_not_called()
        storage.persist_device_data.assert_not_awaited()

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

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("86400", config.API_RETRY_AFTER_MAX),  # a day would park the REST path
            ("-5", 0.0),
            ("2.5", 2.5),
            ("Wed, 21 Oct 2026 07:28:00 GMT", 1.0),  # HTTP-date: fall back to backoff
        ],
    )
    @pytest.mark.asyncio
    async def test_retry_after_is_parsed_and_capped(self, collector, header, expected):
        collector.client = MagicMock()
        collector.client.get = AsyncMock(
            side_effect=[
                _response(status=429, headers={"Retry-After": header}),
                _response(json_body={"ok": True}),
            ]
        )
        collector.min_api_interval = 0
        slept = AsyncMock()
        with patch("app.collector.client.asyncio.sleep", slept):
            assert await collector.make_api_request("http://x/y") == {"ok": True}
        assert slept.await_args_list[0].args[0] == expected

    @pytest.mark.asyncio
    async def test_a_rate_limit_wait_ends_at_shutdown(self, collector):
        """A Retry-After wait must not hold the process past a SIGTERM."""
        collector.client = MagicMock()
        collector.client.get = AsyncMock(
            return_value=_response(status=429, headers={"Retry-After": "300"})
        )
        collector.min_api_interval = 0
        collector.shutdown_event = asyncio.Event()
        task = asyncio.create_task(collector.make_api_request("http://x/y"))
        await asyncio.sleep(0.05)
        collector.shutdown_event.set()
        assert await asyncio.wait_for(task, timeout=2) is None

    @pytest.mark.asyncio
    async def test_401_triggers_reauthentication_and_retries(self, collector):
        collector.client = MagicMock()
        collector.client.get = AsyncMock(
            side_effect=[_response(status=401), _response(json_body={"ok": True})]
        )
        collector.min_api_interval = 0

        async def renew() -> None:  # what the real authenticate() does to the token
            collector.auth_time = datetime.now(UTC)

        with (
            patch.object(
                collector, "authenticate", AsyncMock(side_effect=renew)
            ) as auth,
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


class TestAuthenticationPacing:
    """Password logins against Sense's rate-limited auth endpoint must stay rare: a login
    flood keeps a 429 going and can get the account locked."""

    @staticmethod
    def _collector(storage, counter, auth_status=200, rest_status=200):
        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.05)  # a real round trip: the caller suspends here
            if request.url.path.endswith("/authenticate"):
                counter["auth"] += 1
                if auth_status != 200:
                    return httpx.Response(auth_status, json={"error": "rate limited"})
                return httpx.Response(200, json=AUTH_RESPONSE)
            counter["rest"] += 1
            return httpx.Response(rest_status, json={})

        c = SenseCollector("u", "p", storage)
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c.access_token, c.monitor_id = "t1", "9"
        c.auth_time = datetime.now(UTC) - timedelta(
            seconds=config.TOKEN_RENEW_INTERVAL + 1
        )
        return c

    @staticmethod
    async def _periodic_for(c, seconds):
        c.shutdown_event = asyncio.Event()
        tasks = [
            asyncio.create_task(c.periodic_monitor_status()),
            asyncio.create_task(c.periodic_device_fetch()),
        ]
        await asyncio.sleep(seconds)
        c.shutdown_event.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await c.client.aclose()

    @pytest.mark.asyncio
    async def test_a_due_renewal_logs_in_about_once(self, storage):
        counter = {"auth": 0, "rest": 0}
        await self._periodic_for(self._collector(storage, counter), 3.0)
        assert counter["auth"] <= 2, counter

    @pytest.mark.asyncio
    async def test_a_failing_auth_endpoint_is_backed_off_not_hammered(self, storage):
        counter = {"auth": 0, "rest": 0}
        await self._periodic_for(
            self._collector(storage, counter, auth_status=429), 5.0
        )
        assert counter["auth"] <= 3, f"{counter['auth']} /authenticate calls in 5s"

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_renewal(self, storage):
        counter = {"auth": 0, "rest": 0}
        c = self._collector(storage, counter)
        await asyncio.gather(*(c.fetch_device_data(f"d{i}") for i in range(5)))
        await c.client.aclose()
        assert counter["auth"] == 1, f"{counter['auth']} logins for one renewal"

    @pytest.mark.asyncio
    async def test_a_persistent_401_does_not_log_in_on_every_retry(
        self, storage, monkeypatch
    ):
        """A fresh token that still gets 401 is not fixed by logging in again."""

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(client_mod.asyncio, "sleep", no_sleep)
        counter = {"auth": 0, "rest": 0}
        c = self._collector(storage, counter, rest_status=401)
        c.auth_time = datetime.now(UTC)
        for i in range(3):
            await c.fetch_device_data(f"d{i}")
        await c.client.aclose()
        assert counter["auth"] <= 1, f"{counter['auth']} logins for 3 lookups"


class TestAuthenticationRecovery:
    """Pacing must never strand the collector with a revoked token. The fake below accepts
    only the newest token, so every login revokes the one before it."""

    @staticmethod
    def _collector(state, token_age_s, auth_mode="ok", rest_delay=0.0):
        tokens = iter(range(2, 1000))

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/authenticate"):
                state["auth"] += 1
                await asyncio.sleep(0.01)
                if auth_mode == "nonjson":
                    return httpx.Response(200, text="<html>maintenance</html>")
                if auth_mode == "503":
                    return httpx.Response(503, text="down")
                state["valid"] = f"tok-{next(tokens)}"
                return httpx.Response(
                    200,
                    json={
                        "access_token": state["valid"],
                        "user_id": 1,
                        "monitors": [{"id": 9}],
                    },
                )
            sent = request.headers.get("Authorization", "")
            await asyncio.sleep(rest_delay)  # the request is in flight
            state["rest"] += 1
            if sent != f"Bearer {state['valid']}":
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"ok": True})

        c = SenseCollector("u", "p", MagicMock())
        c.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c.access_token, c.monitor_id = "tok-1", "9"
        c.headers = {"Authorization": "Bearer tok-1"}
        c.auth_time = datetime.now(UTC) - timedelta(seconds=token_age_s)
        c._auth_mono = time.monotonic() - token_age_s
        c.min_api_interval = 0
        return c

    @pytest.mark.asyncio
    async def test_a_stale_token_that_gets_401_is_renewed(self):
        state = {"auth": 0, "rest": 0, "valid": "tok-revoked"}
        c = self._collector(state, token_age_s=120)
        result = await c.make_api_request("https://api.sense.com/x", max_retries=1)
        await c.client.aclose()
        assert result == {"ok": True} and state["auth"] == 1

    @pytest.mark.asyncio
    async def test_a_request_in_flight_during_a_renewal_retries_with_the_new_token(
        self,
    ):
        state = {"auth": 0, "rest": 0, "valid": "tok-1"}
        c = self._collector(
            state, token_age_s=config.TOKEN_RENEW_INTERVAL + 5, rest_delay=0.2
        )
        request = asyncio.create_task(
            c.make_api_request("https://api.sense.com/x", max_retries=1)
        )
        await asyncio.sleep(0.05)  # sent with tok-1
        await c.check_token_renewal()  # revokes tok-1 while the request is in flight
        result = await request
        await c.client.aclose()
        assert result == {"ok": True}, f"gave up: {result!r} {state}"

    @pytest.mark.parametrize("auth_mode", ["nonjson", "503"])
    @pytest.mark.asyncio
    async def test_a_failed_renewal_is_backed_off_whatever_the_failure(self, auth_mode):
        state = {"auth": 0, "rest": 0, "valid": "tok-1"}
        c = self._collector(
            state, token_age_s=config.TOKEN_RENEW_INTERVAL + 5, auth_mode=auth_mode
        )
        for _ in range(10):
            await c.check_token_renewal()
        await c.client.aclose()
        assert state["auth"] == 1, f"{state['auth']} logins in 10 back-to-back checks"

    @pytest.mark.asyncio
    async def test_a_wall_clock_step_back_does_not_make_an_old_token_look_fresh(self):
        state = {"auth": 0, "rest": 0, "valid": "tok-revoked"}
        c = self._collector(state, token_age_s=600)
        c.auth_time = datetime.now(UTC) + timedelta(minutes=50)  # NTP stepped back 1h
        result = await c.make_api_request("https://api.sense.com/x", max_retries=1)
        await c.client.aclose()
        assert result == {"ok": True}, f"gave up, logins={state['auth']}"

    @pytest.mark.asyncio
    async def test_any_successful_login_ends_a_backoff(self):
        state = {"auth": 0, "rest": 0, "valid": "tok-1"}
        c = self._collector(state, token_age_s=120)
        c._auth_failures, c._auth_retry_at = 10, time.monotonic() + 900
        await c.authenticate()  # the WebSocket's refresh path
        c._auth_mono = time.monotonic() - 120  # two minutes pass
        state["valid"] = "tok-revoked"  # Sense revokes the token
        result = await c.make_api_request("https://api.sense.com/x", max_retries=1)
        await c.client.aclose()
        assert result == {"ok": True}, f"gave up, logins={state['auth']}"


class TestExportFiles:
    """Exports hold per-second energy data and device names; on a bind-mounted output
    directory a 0644 file is readable by every user on the Docker host."""

    @pytest.mark.asyncio
    async def test_export_files_are_readable_by_their_owner_only(
        self, collector, tmp_path, monkeypatch
    ):
        import stat

        from app.collector.websocket import WebSocketHandler

        monkeypatch.setattr(config, "EXPORT_FOLDER", str(tmp_path))
        await collector.export_device_data("fridge", {"device": {"id": "fridge"}})
        await collector.export_device_list([{"id": "fridge"}])
        ws = WebSocketHandler("wss://x", {}, AsyncMock())
        await ws._write_received_data_async({"type": "hello"})

        modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in tmp_path.iterdir()}
        assert set(modes) == {
            "device_fridge.json",
            "devices.json",
            "received_data.jsonl",
        }
        assert all(mode == 0o600 for mode in modes.values()), modes
