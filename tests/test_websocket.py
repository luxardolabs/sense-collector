import json
from unittest.mock import AsyncMock, patch

import pytest

from app.collector.websocket import WebSocketHandler


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


class TestWebSocketHandler:
    """Exercises the websockets-based WebSocketHandler (post aiohttp migration)."""

    @pytest.fixture
    def handler(self):
        return WebSocketHandler(
            "wss://test.example.com/ws",
            {"Authorization": "Bearer test_token"},
            AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_connect_success(self, handler):
        mock_ws = MockWS()
        with patch(
            "app.collector.websocket.websockets.connect", AsyncMock(return_value=mock_ws)
        ) as conn:
            result = await handler.connect()
        assert result is True
        assert handler.ws is mock_ws
        conn.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_failure(self, handler):
        with patch(
            "app.collector.websocket.websockets.connect",
            AsyncMock(side_effect=Exception("Connection failed")),
        ):
            result = await handler.connect()
        assert result is False
        assert handler.ws is None

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
                "app.utils.file_validator.FilePathValidator.get_safe_export_path", return_value=None
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
