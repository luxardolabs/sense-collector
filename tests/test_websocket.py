import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from websockets.exceptions import InvalidHandshake, WebSocketException

from app.collector.websocket import WebSocketHandler, handle_websocket_connection
from app.core import config


class _IterableWS:
    """A socket that yields a fixed list of frames, optionally raising at the end."""

    def __init__(self, frames, raises=None):
        self._frames = list(frames)
        self._raises = raises
        self.closed = False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for f in self._frames:
            yield f
        if self._raises:
            raise self._raises

    async def close(self):
        self.closed = True


class MockWS:
    """Minimal stand-in for a `websockets` client connection (current API)."""

    def __init__(self, open_=True):
        self.open = open_
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True
        self.open = False


@pytest.fixture
def handler():
    """A fresh handler per test — shared by every class in this module."""
    return WebSocketHandler(
        "wss://test.example.com/ws",
        {"Authorization": "Bearer test_token"},
        AsyncMock(),
    )


class TestWebSocketHandler:
    """Exercises the websockets-based WebSocketHandler (post aiohttp migration)."""

    @pytest.mark.asyncio
    async def test_connect_success(self, handler):
        mock_ws = MockWS()
        with patch(
            "app.collector.websocket.websockets.connect",
            AsyncMock(return_value=mock_ws),
        ) as conn:
            result = await handler.connect()
        assert result is True
        assert handler.ws is mock_ws
        conn.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            OSError("connection refused"),  # DNS / refused / reset
            InvalidHandshake("bad upgrade"),  # a WebSocketException subclass
            TimeoutError("handshake timed out"),
        ],
        ids=["oserror", "handshake", "timeout"],
    )
    async def test_connect_failure(self, handler, failure):
        """A real transport failure is caught: connect() reports False, ws stays unset."""
        with patch(
            "app.collector.websocket.websockets.connect",
            AsyncMock(side_effect=failure),
        ):
            result = await handler.connect()
        assert result is False
        assert handler.ws is None

    @pytest.mark.asyncio
    async def test_connect_does_not_swallow_unexpected_errors(self, handler):
        """A non-transport error is a collector bug and must propagate, not read as False.

        connect() narrowed its catch to (OSError, WebSocketException, TimeoutError) so a
        genuine defect — e.g. an AttributeError from a renamed attribute — surfaces instead
        of being logged and reported as an ordinary connection failure.
        """
        with (
            patch(
                "app.collector.websocket.websockets.connect",
                AsyncMock(side_effect=AttributeError("renamed attribute")),
            ),
            pytest.raises(AttributeError),
        ):
            await handler.connect()

    @pytest.mark.asyncio
    async def test_send_ping_success(self, handler):
        handler.ws = MockWS(open_=True)
        result = await handler.send_ping()
        assert result is True
        assert json.loads(handler.ws.sent[0]) == {"type": "ping"}

    @pytest.mark.asyncio
    async def test_send_ping_no_connection(self, handler):
        handler.ws = None
        assert await handler.send_ping() is False

    @pytest.mark.asyncio
    async def test_handle_message_text_dispatches(self, handler):
        payload = {"type": "realtime_update", "payload": {"w": 123}}
        with patch("app.core.config.OUTPUT_RECEIVED_DATA", False):
            result = await handler.handle_message(json.dumps(payload))
        assert result is True
        handler.process_data_callback.assert_called_once_with(payload)

    @pytest.mark.asyncio
    async def test_handle_message_disconnect_closes(self, handler):
        data = {"type": "error", "message": "disconnect: closing connection"}
        with patch("app.core.config.OUTPUT_RECEIVED_DATA", False):
            result = await handler.handle_message(json.dumps(data))
        assert result is False  # signals the run loop to reconnect

    @pytest.mark.asyncio
    async def test_handle_message_invalid_json_survives(self, handler):
        with patch("app.core.config.OUTPUT_RECEIVED_DATA", False):
            result = await handler.handle_message("{not valid json")
        assert result is True  # bad frame is logged, connection kept
        handler.process_data_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_write_received_data_async_handles_unsafe_path(self, handler):
        with (
            patch("app.core.config.EXPORT_FOLDER", "/tmp"),
            patch(
                "app.utils.file_validator.FilePathValidator.get_safe_export_path",
                return_value=None,
            ) as mock_validator,
        ):
            await handler._write_received_data_async({"type": "test"})
            mock_validator.assert_called_once()

    @pytest.mark.asyncio
    async def test_shutdown(self, handler):
        handler.ws = MockWS()
        await handler.shutdown()
        assert handler.is_shutting_down is True
        assert handler.ws.closed is True


class TestMonitorConnectionHealth:
    """The health monitor is what notices a connection that is up but no longer delivering."""

    @pytest.mark.asyncio
    async def test_a_silent_connection_triggers_a_reconnect(self, handler):
        """Past WS_HEARTBEAT_TIMEOUT with no frames, the socket is dead to us."""
        handler.ws = MockWS(open_=True)
        handler.last_message_time = time.time() - (config.WS_HEARTBEAT_TIMEOUT + 5)
        assert await handler.monitor_connection_health() is False

    @pytest.mark.asyncio
    async def test_a_failed_ping_triggers_a_reconnect(self, handler):
        """Quiet but not yet timed out: we ping, and a failed ping ends the connection."""
        handler.ws = MockWS(open_=True)
        handler.last_message_time = time.time() - (config.WS_HEARTBEAT_INTERVAL + 1)
        with patch.object(handler, "send_ping", AsyncMock(return_value=False)):
            assert await handler.monitor_connection_health() is False

    @pytest.mark.asyncio
    async def test_a_healthy_connection_keeps_monitoring_until_shutdown(self, handler):
        """A fresh connection must NOT be torn down — it keeps looping until shutdown."""
        handler.ws = MockWS(open_=True)
        handler.last_message_time = time.time()

        async def stop(_delay):
            handler.is_shutting_down = True

        with patch(
            "app.collector.websocket.asyncio.sleep", AsyncMock(side_effect=stop)
        ):
            assert await handler.monitor_connection_health() is True

    @pytest.mark.asyncio
    async def test_a_transport_error_ends_the_connection(self, handler):
        handler.ws = MockWS(open_=True)
        handler.last_message_time = time.time()
        with patch(
            "app.collector.websocket.asyncio.sleep",
            AsyncMock(side_effect=OSError("reset")),
        ):
            assert await handler.monitor_connection_health() is False


class TestHandleMessages:
    @pytest.mark.asyncio
    async def test_no_socket_is_not_an_error(self, handler):
        handler.ws = None
        assert await handler._handle_messages() is False

    @pytest.mark.asyncio
    async def test_decodes_bytes_frames(self, handler):
        """The server may frame text as bytes; both must reach the callback identically."""
        handler.ws = _IterableWS([b'{"type":"hello"}'])
        with patch.object(handler, "handle_message", AsyncMock(return_value=True)) as h:
            await handler._handle_messages()
        h.assert_awaited_once_with('{"type":"hello"}')

    @pytest.mark.asyncio
    async def test_stops_when_a_message_reports_failure(self, handler):
        handler.ws = _IterableWS(['{"a":1}', '{"b":2}'])
        with patch.object(
            handler, "handle_message", AsyncMock(return_value=False)
        ) as h:
            assert await handler._handle_messages() is False
        assert h.await_count == 1

    @pytest.mark.asyncio
    async def test_stops_promptly_on_shutdown(self, handler):
        handler.ws = _IterableWS(['{"a":1}', '{"b":2}', '{"c":3}'])

        async def first_then_shutdown(_msg):
            handler.is_shutting_down = True
            return True

        with patch.object(
            handler, "handle_message", AsyncMock(side_effect=first_then_shutdown)
        ) as h:
            assert await handler._handle_messages() is False
        assert h.await_count == 1

    @pytest.mark.asyncio
    async def test_a_websocket_error_ends_the_loop(self, handler):
        handler.ws = _IterableWS([], raises=WebSocketException("gone"))
        assert await handler._handle_messages() is False


class TestReconnectionDelay:
    @pytest.mark.asyncio
    async def test_sleeps_for_the_requested_delay(self, handler):
        with patch("app.collector.websocket.asyncio.sleep", AsyncMock()) as sleep:
            await handler._handle_reconnection_delay(7)
        sleep.assert_awaited_once_with(7)


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_closes_the_socket_and_sets_the_flag(self, handler):
        ws = MockWS(open_=True)
        handler.ws = ws
        await handler.shutdown()
        assert handler.is_shutting_down is True
        assert ws.closed is True

    @pytest.mark.asyncio
    async def test_shutdown_without_a_socket_is_safe(self, handler):
        handler.ws = None
        await handler.shutdown()
        assert handler.is_shutting_down is True


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_a_failed_connect_backs_off_and_retries(self, handler):
        """Backoff must grow rather than hot-looping against a down endpoint."""
        delays = []

        async def record(delay):
            delays.append(delay)
            if len(delays) == 3:
                handler.is_shutting_down = True

        with (
            patch.object(handler, "connect", AsyncMock(return_value=False)),
            patch.object(
                handler, "_handle_reconnection_delay", AsyncMock(side_effect=record)
            ),
        ):
            await asyncio.wait_for(handler.run(), timeout=5)

        assert delays == sorted(delays), "backoff must be non-decreasing"
        assert delays[-1] > delays[0], "backoff must actually grow"
        assert handler.reconnect_count == 3

    @pytest.mark.asyncio
    async def test_backoff_is_capped(self, handler):
        delays = []

        async def record(delay):
            delays.append(delay)
            if len(delays) >= 12:
                handler.is_shutting_down = True

        with (
            patch.object(handler, "connect", AsyncMock(return_value=False)),
            patch.object(
                handler, "_handle_reconnection_delay", AsyncMock(side_effect=record)
            ),
        ):
            await asyncio.wait_for(handler.run(), timeout=10)

        assert max(delays) <= config.WS_RECONNECT_DELAY_CAP

    @pytest.mark.asyncio
    async def test_shutdown_skips_the_reconnection_delay(self, handler):
        """SIGTERM must not stall for up to WS_RECONNECT_DELAY_CAP."""
        handler.is_shutting_down = True
        with patch.object(handler, "_handle_reconnection_delay", AsyncMock()) as delay:
            await asyncio.wait_for(handler.run(), timeout=5)
        delay.assert_not_called()


class TestHandleWebSocketConnection:
    @pytest.mark.asyncio
    async def test_builds_a_handler_and_runs_it(self):
        with patch("app.collector.websocket.WebSocketHandler.run", AsyncMock()) as run:
            await handle_websocket_connection("ws://x", {"A": "b"}, AsyncMock())
        run.assert_awaited_once()
