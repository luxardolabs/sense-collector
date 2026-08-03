# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Sense Collector is a **Python 3.14** headless data collector that bridges the Sense home energy monitor's cloud API with **InfluxDB** for time-series storage, visualized in **Grafana**. It authenticates against the Sense cloud, streams the realtime WebSocket feed (via the `websockets` library), and makes concurrent REST calls (via `httpx` with HTTP/2). It reaches OUT to Sense and OUT to InfluxDB — it publishes **no inbound port**.

This is a Luxardo Labs fleet collector; it follows `/mnt/luxardolabs/COLLECTOR-FLEET-STANDARD.md` with **kasa-collector** as the reference implementation.

## Common Development Commands

The `Makefile` is the source of truth. `VERSION` (repo root) is the version source of truth.

```bash
make help            # grouped command help
make build-local     # build the runtime image from current source (no push)
make test-e2e        # hardware-free end-to-end: fake Sense -> collector -> InfluxDB, asserted
make demo-up         # self-contained demo: fake Sense + bundled InfluxDB + Grafana (localhost:3000)
make dev-up          # dev stack: real Sense account + bundled InfluxDB + Grafana

make lint            # luxlint: canonical ruff (mount-only) + mypy --strict tail (.luxlint.toml)
make arch            # architecture conformance via the pinned luxarch container (.luxarch.toml)
make test            # pytest suite (needs the :dev image; dev-build-push first, or run locally)
make check           # lint + arch + test
make poetry-lock     # regenerate poetry.lock (poetry-in-docker; no host poetry needed)
make release         # build + push :VERSION + :latest (multi-arch) to the private registry
make release-public  # promote that released image to ghcr.io/luxardolabs/<collector> by digest

# Run directly (after setting env; see Configuration)
python -m app.main
python -m app.health.check   # container healthcheck
```

Dependencies are managed with **Poetry** (`pyproject.toml` + committed `poetry.lock`). There is no `requirements.txt`. `make lint`/`make test` build a fresh image from CURRENT source (never exec into the baked container — stale code).

## Architecture Overview

Event-driven asyncio. `app/main.py` orchestrates concurrent tasks: WebSocket receive, device-lookup queue workers, periodic monitor-status poll, periodic device-list refresh, and periodic token renewal.

### Layout (fleet standard — `app/` package at the repo root, `app.`-prefixed imports)

- `app/main.py` — entrypoint / orchestrator (`python -m app.main`)
- `app/core/config.py` — env-driven config + validation (all `SENSE_COLLECTOR_*` vars)
- `app/collector/` — the collection logic:
  - `client.py` — `SenseCollector` (auth, REST, device cache, queue workers, WS-data dispatch)
  - `websocket.py` — `WebSocketHandler` (auto-reconnect, heartbeat/ping, health monitor)
  - `endpoints.py` — centralized Sense API + WebSocket URL builders
- `app/storage/influxdb.py` — asyncio-native InfluxDB writes (`InfluxDBClientAsync`, one awaited batch per poll cycle) (`sense_mains`, `sense_devices`, `sense_event`, `sense_monitor_status`, `sense_o11y`, `sense_always_on*`, …)
- `app/utils/` — `logging.py`, `time.py`, `file_validator.py`
- `app/health/check.py` — Docker HEALTHCHECK

Validate layout conformance with `python /mnt/luxardolabs/check_layout.py .` (must be green).

### Data flow

```
Sense cloud API/WebSocket -> app/collector (client + websocket) -> app/storage/influxdb -> InfluxDB -> Grafana
```

### Key design patterns

- **Queue-based processing** decouples WS reception from API lookups (semaphore-limited concurrency).
- **Caching with expiry**: device names cached ~15 min to respect Sense rate limits.
- **Automatic WebSocket reconnection** with exponential backoff + heartbeat.
- Three async transports by design: **httpx** (Sense REST, HTTP/2), **websockets** 16.x (Sense realtime WS — `websockets.asyncio.client` / `additional_headers`), and **aiohttp** (InfluxDB writes, via `InfluxDBClientAsync`, per the fleet ingestion standard).

## The four run stacks

Distinguished by source (real vs fake Sense) and observability (external vs bundled). All are `.yml`, short-form volumes, and run on the **bridge network** (Sense is a cloud API — no host networking). Compose never builds except the fake-Sense service in demo/e2e (`build: ./harness`).

| Stack          | compose file                         | source          | InfluxDB/Grafana          | make                 |
| -------------- | ------------------------------------ | --------------- | ------------------------- | -------------------- |
| collector-only | `compose.yml` (+ `compose.prod.yml`) | real            | external (your fleet)     | `make up` / `prod-*` |
| dev            | `compose.dev.yml`                    | real            | bundled, auto-provisioned | `make dev-up`        |
| demo           | `compose.demo.yml`                   | fake (emulator) | bundled, auto-provisioned | `make demo-up`       |
| test           | `compose.e2e.yml`                    | fake            | ephemeral, no Grafana     | `make test-e2e`      |

- Bundled `influxdb:2.7` + Grafana are dev/demo/test only. InfluxQL dashboards need a DBRP mapping (`ops/influxdb/init-dbrp.sh`); Grafana is provisioned via `grafana/provisioning/` (datasource pinned uid `uDxwFcOGz`; dashboards from `grafana/shared-local/`, using the `${data_source}` picker var).
- **Emulator**: `harness/fake_sense.py` — pure-stdlib Sense cloud fake (HTTP auth + REST + hand-rolled RFC 6455 WebSocket). Point the collector at it with `SENSE_COLLECTOR_API_BASE_URL` / `SENSE_COLLECTOR_WS_BASE_URL`. See `harness/README.md`.

## Configuration

All config is via `SENSE_COLLECTOR_*` environment variables (see `app/core/config.py` for the full validated list, and `docs/CONFIGURATION.md`). **Secrets live in gitignored `.env.dev` / `.env.prod`** (copy from `.env.example`); the dev stack layers an optional gitignored `.env.dev.local` (copy from `.env.dev.local.example`) with your real Sense account. `.env.demo` is committed, non-secret bundled-stack config. Run `make gitleaks-staged` before committing.

Required: `API_USERNAME`, `API_PASSWORD`, `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` (all `SENSE_COLLECTOR_`-prefixed).

## Important Implementation Notes

1. **WebSocket message types** handled: `realtime_update`, `new_timeline_event`, `hello`, `data_change`, `device_states`, `device_states_changed`.
1. **Rate limiting**: strict Sense API limits — device names cached ~15 min to minimize calls.
1. **Device name resolution**: devices are queued for lookup when discovered; cache respects rate limits.
1. **InfluxDB writes** (fleet ingestion standard): the asyncio-native `InfluxDBClientAsync` (aiohttp), opened in `connect()`; each poll cycle's points are written as ONE `await write_api.write(...)` — no batch/flush knobs, no background buffer to flush. `ping()` fails fast on an unreachable server; auth/bucket errors (401/404) are logged and retried next cycle.
1. **Lifecycle & error handling** (fleet canonical spine): long-lived clients open in `connect()` and close in `close()` (never a fresh client/session per request); shutdown is `loop.add_signal_handler` + an `asyncio.Event` threaded into the periodic loops (interruptible `asyncio.wait_for(shutdown.wait(), timeout=…)` sleeps); env is read via `ConfigValidator` and logged once through `config.describe_settings()`; **every log call uses lazy `%s` args, never f-strings** (ruff `G` fails lint on a violation). The app logs-and-continues on individual failures. `make arch` (folded into `make check`) statically guards these so they can't drift.
1. **Docker**: four-stage `Dockerfile` (builder → builder-dev → base → dev). Prod pulls `:latest`; dev/demo/test pull/build `:dev`. `Dockerfile.lint` overlays current source for lint/test.
1. **NO AI attribution** in commits/PRs (house rule — no Co-Authored-By, "Generated with", robot emoji).

Version history prior to the fleet-standard migration lives in the LuxPM "Changelog" page (project `SENSECOLLE`); migration chunks are tracked as LuxPM issues.
