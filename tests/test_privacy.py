"""What must never reach a log line, through the collector's real logging setup.

Reproductions from the /adversarial pass on the 0.276.0 bump: with the fleet logging module
installed, websockets' DEBUG request line printed the live WebSocket access token, and
LOG_FORMAT=text let a device name containing a newline forge a whole log line.
"""

import asyncio
import logging
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.asyncio.client import connect

from app.collector.client import SenseCollector
from app.core.privacy import redact
from app.main import setup_logging
from harness import fake_sense

TOKEN = "LIVE-SENSE-ACCESS-TOKEN-0123456789"
FORGED = (
    "Fridge\n2026-10-10 12:00:00,000 CRITICAL general InfluxDB token rotated; "
    "send to evil\x1b[2J"
)
TOUCHED = ("general", "api", "storage", "httpx", "httpcore", "hpack", "h2")


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch):
    """setup_logging mutates process-global state; put every piece of it back."""
    for var in ("LOG_FORMAT", "LOG_LEVEL", "LOG_MODULE_LEVELS"):
        monkeypatch.delenv(var, raising=False)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {name: logging.getLogger(name).level for name in TOUCHED}
    monkeypatch.setattr(logging.Logger, "makeRecord", logging.Logger.makeRecord)
    yield
    for handler in root.handlers:
        handler.filters.clear()
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


@pytest.fixture
def fake_sense_port():
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake_sense.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_the_ws_access_token_is_never_logged(
    capsys, monkeypatch, fake_sense_port, level
):
    monkeypatch.setenv("LOG_LEVEL", level)
    setup_logging()
    url = f"ws://127.0.0.1:{fake_sense_port}/monitors/12345/realtimefeed?access_token={TOKEN}"

    async def receive_one() -> None:
        async with connect(url) as ws:
            await asyncio.wait_for(ws.recv(), 5)

    asyncio.run(receive_one())
    out = capsys.readouterr().out
    assert TOKEN not in out
    if level == "DEBUG":
        # The request line IS logged at DEBUG, so the redaction is what is being tested.
        assert "access_token=[REDACTED]" in out


def _feed_timeline_event(device_name: str) -> None:
    storage = MagicMock()
    storage.persist_timeline_data = AsyncMock()
    collector = SenseCollector("user@example.com", "pw", storage)
    asyncio.run(
        collector.process_websocket_data(
            {
                "type": "new_timeline_event",
                "payload": {
                    "items_added": [
                        {
                            "device_id": "d1",
                            "device_name": device_name,
                            "type": "DeviceWasOn",
                        }
                    ]
                },
            }
        )
    )


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_a_remote_device_name_cannot_forge_a_line_or_emit_escapes(
    capsys, monkeypatch, fmt
):
    monkeypatch.setenv("LOG_FORMAT", fmt)
    setup_logging()
    _feed_timeline_event(FORGED)

    out = capsys.readouterr().out
    assert "Timeline event" in out
    assert "\x1b" not in out
    assert not any(
        line.startswith("2026-10-10 12:00:00,000 CRITICAL") for line in out.splitlines()
    )


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x9b", "\ud800"])
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_unicode_line_breaks_and_c1_controls_cannot_forge_a_line(
    capsys, monkeypatch, fmt, separator
):
    """str.splitlines() and many log viewers break lines on U+2028/U+2029/NEL too, and
    U+009B is an 8-bit CSI that a C1-honouring terminal acts on."""
    monkeypatch.setenv("LOG_FORMAT", fmt)
    setup_logging()
    _feed_timeline_event(f"Fridge{separator}2026-10-10 12:00:00,000 CRITICAL forged")

    out = capsys.readouterr().out
    assert "Timeline event" in out
    assert separator not in out
    assert not any(
        line.startswith("2026-10-10 12:00:00,000 CRITICAL") for line in out.splitlines()
    )


def test_a_traceback_keeps_its_lines_but_loses_the_token():
    line = redact(
        {
            "message": "ws failed for ?access_token=abc123&x=1",
            "exception": "Traceback (most recent call last):\n  url=...?access_token=abc123\n",
        }
    )
    assert line["message"] == "ws failed for ?access_token=[REDACTED]&x=1"
    assert line["exception"].count("\n") == 2
    assert "abc123" not in line["exception"]


def test_non_string_values_pass_through():
    assert redact({"line": 42, "attributes_ok": None}) == {
        "line": 42,
        "attributes_ok": None,
    }


@pytest.mark.parametrize("fmt", ["json", "text"])
@pytest.mark.parametrize(
    "module_levels", [None, '{"hpack": "DEBUG", "hpack.hpack": "DEBUG", "h2": "DEBUG"}']
)
def test_http2_header_blocks_never_reach_the_log(
    capsys, monkeypatch, fmt, module_levels
):
    """hpack logs each encoded header block at DEBUG, Huffman-coded, so the token regex
    cannot see the REST `Authorization: Bearer` in it, yet any hpack decoder recovers it."""
    import hpack

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_FORMAT", fmt)
    if module_levels:
        monkeypatch.setenv("LOG_MODULE_LEVELS", module_levels)
    setup_logging()

    hpack.Encoder().encode([(b"authorization", f"Bearer {TOKEN}".encode())])

    out = capsys.readouterr().out
    assert "Encoded header block" not in out
    assert "hpack" not in out
