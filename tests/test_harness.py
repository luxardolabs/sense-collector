"""The fake Sense the demo and e2e stacks run must behave like the real cloud where the
collector depends on it: a WebSocket close is answered, and payloads keep Sense's shape."""

import asyncio
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from websockets.asyncio.client import connect

from harness import fake_sense


@pytest.fixture
def fake_sense_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake_sense.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_websocket_close_is_answered(fake_sense_port):
    """RFC 6455: the server echoes the close frame. A silent one holds every collector
    shutdown in the demo and e2e stacks for the whole close timeout."""
    url = f"ws://127.0.0.1:{fake_sense_port}/monitors/12345/realtimefeed?access_token=t"

    async def open_then_close() -> float:
        async with connect(url, close_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), 5)
            started = time.monotonic()
            await ws.close()
            return time.monotonic() - started

    assert asyncio.run(open_then_close()) < 1.0


def test_device_detection_lists_devices_as_sense_does():
    """persist_monitor_status iterates found/in_progress as lists of device objects."""
    detection = fake_sense.monitor_status()["device_detection"]
    for status in ("found", "in_progress"):
        assert isinstance(detection[status], list)
        assert all(isinstance(d, dict) for d in detection[status])
