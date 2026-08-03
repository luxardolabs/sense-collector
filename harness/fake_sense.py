#!/usr/bin/env python3
"""Pure-stdlib emulator of the Sense cloud API + realtime WebSocket.

Lets sense-collector be tested end-to-end with NO Sense account and NO hardware.
One HTTP port (default 8080) serves both the REST API and the realtime WebSocket:

  POST /authenticate                            -> auth JSON (access_token + monitor id)
  GET  /app/monitors/{id}/devices               -> device list
  GET  /app/monitors/{id}/devices/{device_id}   -> device detail
  GET  /app/monitors/{id}/status                -> monitor status
  GET  /monitors/{id}/realtimefeed  (Upgrade)   -> streams realtime_update frames

Point the collector at it with:
  SENSE_COLLECTOR_API_BASE_URL=http://<host>:8080
  SENSE_COLLECTOR_WS_BASE_URL=ws://<host>:8080

Protocol fidelity is the point: the JSON shapes mirror what app/collector/client.py
parses (auth: access_token/user_id/monitors[0].id; realtime payload: hz/c/w/epoch +
optional voltage/channels/devices[].sd). No third-party deps — stdlib only.

Env knobs: SENSE_FAKE_PORT (8080), SENSE_FAKE_MONITOR_ID (12345),
SENSE_FAKE_BASE_W (1200), SENSE_FAKE_INTERVAL (1.0), SENSE_FAKE_HZ (60.0).
"""

import base64
import hashlib
import json
import math
import os
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.getenv("SENSE_FAKE_PORT", "8080"))
MONITOR_ID = int(os.getenv("SENSE_FAKE_MONITOR_ID", "12345"))
USER_ID = int(os.getenv("SENSE_FAKE_USER_ID", "67890"))
ACCOUNT_ID = int(os.getenv("SENSE_FAKE_ACCOUNT_ID", "11111"))
BASE_W = float(os.getenv("SENSE_FAKE_BASE_W", "1200"))
INTERVAL = float(os.getenv("SENSE_FAKE_INTERVAL", "1.0"))
HZ = float(os.getenv("SENSE_FAKE_HZ", "60.0"))
VOLTAGE = 120.0

# RFC 6455 magic GUID for the Sec-WebSocket-Accept handshake.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# The simulated home: a mix of plug-metered (sd) and inferred (no sd) devices, so the
# collector exercises both the is_plug and non-plug branches of persist_realtime_data.
DEVICES = [
    {"id": "ac", "name": "AC Unit", "icon": "ac", "frac": 0.45, "plug": False},
    {"id": "fridge", "name": "Refrigerator", "icon": "fridge", "frac": 0.12, "plug": True},
    {"id": "oven", "name": "Oven", "icon": "stove", "frac": 0.20, "plug": False},
    {"id": "washer", "name": "Washer", "icon": "washer", "frac": 0.08, "plug": True},
]


def _now() -> int:
    return int(time.time())


# ----- JSON payload builders (shapes mirror the real Sense API) --------------

def auth_response() -> dict:
    return {
        "authorized": True,
        "account_id": ACCOUNT_ID,
        "user_id": USER_ID,
        "access_token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "monitors": [
            {
                "id": MONITOR_ID,
                "serial_number": "FAKE0000000",
                "time_zone": "America/Chicago",
                "solar_connected": False,
                "online": True,
                "attributes": {"id": MONITOR_ID, "name": "Fake Home"},
            }
        ],
        "bridge_server": f"ws://localhost:{PORT}",
        "totp_enabled": False,
    }


def device_list() -> list:
    # The client accepts a bare list (see fetch_devices).
    return [{"id": d["id"], "name": d["name"], "icon": d["icon"]} for d in DEVICES]


def device_detail(device_id: str) -> dict:
    d = next((x for x in DEVICES if x["id"] == device_id), None)
    name = d["name"] if d else device_id
    icon = d["icon"] if d else ""
    return {
        "device": {
            "id": device_id,
            "name": name,
            "icon": icon,
            "tags": {"DeviceListAllowed": "true"},
        },
        "usage": {"avg_monthly_KWH": 12.3, "avg_monthly_cost": 1500, "yearly_KWH": 148.0},
    }


def monitor_status() -> dict:
    return {
        "monitor_id": MONITOR_ID,
        "signals": {"progress": 100, "status": "OK"},
        "device_detection": {"in_progress": [], "found": len(DEVICES), "num_detected": len(DEVICES)},
        "monitor_info": {"online": True, "version": "1.0-fake", "ssid": "fake-wifi", "signal": -55},
    }


def realtime_payload(t: float) -> dict:
    """Build a realtime_update payload with smoothly-varying per-device load."""
    devices = []
    total = 0.0
    for i, d in enumerate(DEVICES):
        wobble = 0.6 + 0.8 * (0.5 * (1 + math.sin(t / 7.0 + i)))
        w = round(BASE_W * d["frac"] * wobble, 1)
        total += w
        entry = {"id": d["id"], "name": d["name"], "icon": d["icon"], "w": w}
        if d["plug"]:
            entry["sd"] = {
                "w": w,
                "i": round(w / VOLTAGE, 3),
                "v": VOLTAGE,
                "e": round(w * 0.5, 1),
            }
        devices.append(entry)
    total = round(total, 1)
    l1 = round(total * 0.5, 1)
    l2 = round(total - l1, 1)
    return {
        "hz": HZ,
        "c": round(total / VOLTAGE, 3),
        "w": total,
        "epoch": _now(),
        "voltage": [round(VOLTAGE + 0.1, 1), round(VOLTAGE + 0.3, 1)],
        "channels": [l1, l2],
        "devices": devices,
    }


# ----- Minimal RFC 6455 WebSocket framing (server side, unmasked out) --------

def ws_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Frame a server->client message (FIN set, never masked)."""
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


def ws_read_frame(rfile):
    """Read one client->server frame. Returns (opcode, payload) or (None, None) on EOF."""
    hdr = rfile.read(2)
    if len(hdr) < 2:
        return None, None
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", rfile.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    data = rfile.read(length)
    if masked:
        data = bytes(data[i] ^ mask[i % 4] for i in range(length))
    return opcode, data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence per-request logging
        pass

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)  # drain the credentials body (any creds accepted)
        if path.endswith("/authenticate"):
            self._json(auth_response())
        else:
            self._json({"error": "not found"}, 404)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if "/realtimefeed" in path and self.headers.get("Upgrade", "").lower() == "websocket":
            self._serve_ws()
            return
        if path.endswith("/devices"):
            self._json(device_list())
        elif "/devices/" in path:
            self._json(device_detail(path.rsplit("/", 1)[-1]))
        elif path.endswith("/status"):
            self._json(monitor_status())
        else:
            self._json({"error": "not found"}, 404)

    def _serve_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        sock = self.connection
        send_lock = threading.Lock()
        stop = threading.Event()

        def send(msg_obj=None, *, opcode=0x1, raw=None):
            payload = raw if raw is not None else json.dumps(msg_obj).encode()
            with send_lock:
                sock.sendall(ws_frame(payload, opcode=opcode))

        def reader():
            # Respond to client WS ping frames with pongs; exit on close/EOF.
            try:
                while not stop.is_set():
                    opcode, data = ws_read_frame(self.rfile)
                    if opcode is None or opcode == 0x8:  # EOF or close
                        break
                    if opcode == 0x9:  # ping -> pong
                        send(raw=data, opcode=0xA)
                    # text frames (app-level {"type":"ping"}) are simply ignored
            except Exception:
                pass
            finally:
                stop.set()

        threading.Thread(target=reader, daemon=True).start()

        try:
            send({"type": "hello", "payload": {"online": True}})
            frame = 0
            t0 = time.time()
            while not stop.is_set():
                send({"type": "realtime_update", "payload": realtime_payload(time.time() - t0)})
                frame += 1
                if frame == 3:
                    send({
                        "type": "device_states",
                        "payload": {
                            "update_type": "initial",
                            "states": [{"device_id": "fridge", "mode": "active", "state": "on"}],
                        },
                    })
                if frame == 6:
                    send({
                        "type": "new_timeline_event",
                        "payload": {
                            "items_added": [{
                                "device_id": "ac",
                                "device_name": "AC Unit",
                                "type": "DeviceOn",
                                "time": _now(),
                                "body": "AC Unit turned on",
                                "icon": "ac",
                            }]
                        },
                    })
                stop.wait(INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            stop.set()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"fake-sense: listening on :{PORT} (monitor {MONITOR_ID}, {len(DEVICES)} devices)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
