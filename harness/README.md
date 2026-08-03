# Fake Sense harness

`fake_sense.py` is a **pure-stdlib** emulator of the Sense cloud API + realtime
WebSocket. It lets `sense-collector` run end-to-end with **no Sense account and no
hardware** — used by the `demo` and `e2e` stacks.

## What it speaks

One HTTP port (default `8080`) serves both the REST API and the realtime WebSocket:

| Method | Path | Response |
|--------|------|----------|
| POST | `/authenticate` | auth JSON — `access_token`, `user_id`, `monitors[0].id` |
| GET | `/app/monitors/{id}/devices` | device list (bare array) |
| GET | `/app/monitors/{id}/devices/{device_id}` | device detail |
| GET | `/app/monitors/{id}/status` | monitor status |
| GET | `/monitors/{id}/realtimefeed` (`Upgrade: websocket`) | streams `realtime_update` frames (+ `hello`, `device_states`, `new_timeline_event`) |

The JSON shapes mirror exactly what `app/collector/client.py` parses, and the realtime
payload carries `hz`/`c`/`w`/`epoch` (required) plus `voltage`/`channels`/`devices[].sd`
so both the plug and non-plug branches of `persist_realtime_data` run.

## Pointing the collector at it

```
SENSE_COLLECTOR_API_BASE_URL=http://<host>:8080
SENSE_COLLECTOR_WS_BASE_URL=ws://<host>:8080
```

Any username/password is accepted.

## Env knobs

| Var | Default | Meaning |
|-----|---------|---------|
| `SENSE_FAKE_PORT` | `8080` | listen port |
| `SENSE_FAKE_MONITOR_ID` | `12345` | monitor id returned by auth |
| `SENSE_FAKE_BASE_W` | `1200` | approximate whole-home baseline watts |
| `SENSE_FAKE_INTERVAL` | `1.0` | seconds between realtime frames |
| `SENSE_FAKE_HZ` | `60.0` | mains frequency reported |

## Protocol-fidelity note

The WebSocket layer is a minimal hand-rolled RFC 6455 server (handshake + framing),
because the collector connects with the `websockets` library and expects real frames
and ping/pong. If you change the collector's WS handling, validate against this first:
bring up `make demo-up` and confirm the collector logs `WebSocket connected` and rows
land in `sense_mains`.
