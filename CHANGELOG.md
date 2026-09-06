# Changelog

All notable changes to Sense Collector.

Format based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versioning is **CalVer** `YYYY.0M.MICRO` — zero-padded month, `MICRO` counting from `0` within that month.

## [2026.09.0] - 2026-09-06

Maintenance and hardening release. No configuration changes and no breaking changes: existing `.env` files, compose stacks and dashboards work unchanged.

### Security

- **Patched four CVEs in transitive dependencies.** Both packages sit on live paths — `aiohttp` is the transport for every InfluxDB write, and `h2` is the HTTP/2 stack behind the Sense API client — and both were pulled in with unbounded upper ranges, so a plain re-lock resolved straight back to the vulnerable versions. Explicit version floors are now declared in `pyproject.toml`:
  - `aiohttp >= 3.14.3` — CVE-2026-69244 (out-of-bounds heap read in the C response parser while building an error message for a malformed response, reachable from a misbehaving or compromised InfluxDB), CVE-2026-69243 (request smuggling via WebSocket upgrades), CVE-2026-59881 (client decompresses RSV1 frames when `permessage-deflate` was never negotiated).
  - `h2 >= 4.4.1` — CVE-2026-71554 (duplicate `Host` headers forwarded to the consumer, enabling smuggling where HTTP/2 is downgraded to HTTP/1.1).

### Fixed

- **Health check could misreport across a DST boundary.** The heartbeat age was computed by subtracting two *naive*, container-local datetimes, so at a daylight-saving transition it could shift by an hour — reporting a live collector as unhealthy, or a stale one as healthy. Both sides are now timezone-aware UTC instants.
- **Naive timestamps in the reconnect and startup log lines** are now created aware in UTC and converted to the container's zone only for display.
- **Configuration warnings bypassed the app's logging setup.** `app/core/config.py` logged through the root logger, so those records ignored the per-area levels (`LOG_LEVEL_GENERAL` / `_API` / `_STORAGE`) and could not be filtered by source. It now uses a module logger.

### Changed

- **Exception handling is narrower.** The collector still logs-and-continues on individual failures — that behaviour is unchanged — but 26 `except Exception` sites were narrowed to the errors that can actually occur there (malformed payload, filesystem, transport, InfluxDB). Previously a genuine defect such as an `AttributeError` from a renamed method was swallowed and logged as though it were an ordinary transient failure; it now surfaces. Six deliberate last-resort handlers (the per-message dispatcher, the WebSocket task supervisor and reconnect loop, the top-level fatal handler, and two teardown paths) keep a broad catch and carry an inline justification.
- **Build backend is now hatchling**, with the version read dynamically from the `VERSION` file so it can no longer drift from `pyproject.toml`. Poetry remains the dependency manager (`package-mode = false`); `poetry.lock` and every `poetry install --no-root` path are unaffected.
- **Version scheme corrected to zero-padded CalVer** (`2026.08.0` rather than `2026.8.0`).
- The e2e stack's InfluxDB data now uses `tmpfs` instead of an implicit anonymous Docker volume, which previously orphaned a fresh volume on every run.

### Internal

Not user-visible, recorded for maintainers:

- Test suite grew from 52 to 197 tests; measured coverage went from an unmeasured 41% to 78%, now enforced by a ratchet in `make test` that fails if coverage regresses. `client.py` — the core collector, previously at 0% — is at 81%.
- The repo is fully onboarded to the three fleet guards (`luxarch`, `luxlint`, `luxaudit`) with a canonical `make check` gate, an honesty step that fails if a guard scanned nothing, and committed guard-status files.
- One shared, GC-capped buildx builder replaces the per-project one.

## [2026.8.0] - 2026-08-03

First Luxardo Labs fleet-standard release.

### Added

- Python 3.14 headless collector: streams the Sense cloud WebSocket and REST API, writes energy metrics to InfluxDB for visualization in Grafana.
- Public multi-arch image (`linux/amd64`, `linux/arm64`) at `ghcr.io/luxardolabs/sense-collector`.
- Licensed AGPL-3.0-only.

______________________________________________________________________

## Earlier history

Versions before the fleet-standard migration used SemVer-ish CalVer (`2025.7.x`) and are recorded in the project's internal changelog rather than here. In summary, `2025.7.x` covered the aiohttp → httpx (HTTP/2) migration, the move to the `websockets` library, timeline-event persistence fixes, the 15-minute device-name cache added for Sense's rate limits, bounded queues, path-traversal validation, structured JSON logging, and the first pytest suite.
