# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

<!-- luxarch:claude-pointer asset v4 - DO NOT edit this marker line; it is how repo.claude_pointer_present knows your copy is current. Re-emit with `luxarch --emit claude-pointer`. -->

## How to work here (fleet conduct — read the standard, not just this block)

**`luxarch --doc FLEET-AGENT-CONDUCT-STANDARD` — read it in full before your first change.** It is the one home for *how* agents work in this fleet. This block is a pointer plus the handful of rules that get broken most; it is not a summary and does not replace reading it.

**Report the result, not the mountain.** No "heavy", "multi-hour", "the big one", no narrating difficulty. Done + next in one line, with numbers.

**Decide; do not hand back a menu.** Whether to ask the owner is decided by the **class of action**, never by how confident you feel:

- **Ask** — deleting anything; changing scope; a deferral/allowlist/exemption; publishing outward (pushing another repo, a force-push, a history rewrite); a genuine product fork where the choice is taste, not correctness.
- **Do it** — aligning code to a ratified standard or a guard red; anything you have evidence for that is reversible in one commit. The standard already decided; say what you did.

When you do ask: **one decision per message**, the evidence that makes it answerable, your recommendation stated as one, and a question answerable in one word. **A recommendation that ends in a menu is not a recommendation** — if you rejected the alternatives, re-offering them asks the owner to redo your analysis.

**Use the fleet skills; don't improvise the procedure.** `/wrap-up` before you call anything done (tests, every red in touched files, docs, gate, LuxPM closed out, all with evidence). `/pin-bump` to upgrade the guards. `/escalate` when a guard is wrong. `/release` to cut a release.

**You touched it, you own it.** Edit a file for any reason and it has a mypy, ruff or luxarch red: fix every one in that file, not just yours. Never spend time proving a red predates you; fix it. Test what you changed first. **Before fixing any mypy red, read `luxlint --playbook mypy-sweep` in full.**

**Align or escalate; never route around.** A guard red is fixed by changing the code, or escalated to the guard maintainer as genuinely wrong. Never by an exemption, a `# noqa`, a deferral, or a local config. Verification is not authorization: proving something is unreferenced does not license deleting it.

**Escalations go in THIS repo's LuxPM project** — label `fleet-escalation`, title `[<guard> ESCALATION] …`, self-contained enough to forward whole. **Search LuxPM for an existing issue first** (and comment on it if found); filing a new one is pre-authorized. **Never a GitHub issue** — there is no fallback. The maintainer sweeps the label across every project and picks it up where you filed it.

**A red stays RED while its escalation is open.** The fleet does not gate CI on red. A lit red is honest; a silenced one is a lie you will inherit.

**Commit as `luxardolabs`** using the global git config, and never `git -c user.email=…`. No AI attribution in commit messages.

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

make check           # THE gate: guard-version-check honest lint mypy test arch audit gitleaks
make plan            # THE worklist: reds + sweep TRIAGE + inert AUDIT (start here, not `check`)
make guard-version-check  # FATAL: fails if any guard pin is behind :latest (first step of check)
make guard-upgrade   # bump every guard pin to latest + print what newly bites
make honest          # honesty gate: luxarch --assert-scans + luxlint --preflight
make lint            # ruff via luxlint (mount-only, canonical config)
make mypy            # mypy via luxlint (mount-only; fleet typed deps baked — no in-repo tail)
make format          # THE canonical fixer (luxlint --format) — never a bare ruff/mdformat
make arch            # architecture conformance via the pinned luxarch container (.luxarch.toml)
make test            # pytest + the [test].coverage_min ratchet (lock-built image, mounted source)
make status          # regenerate the committed .lux*-status.json (commit them)
make onboard-check   # proves all three guards are wired + honest (NOT that they are green)
make poetry-lock     # regenerate poetry.lock (poetry-in-docker; no host poetry needed)
make release         # build + push :VERSION + :latest (multi-arch) to the private registry
make release-public  # promote that released image to ghcr.io/luxardolabs/<collector> by digest

# Run directly (after setting env; see Configuration)
python -m app.main
python -m app.health.check   # container healthcheck
```

Dependencies are managed with **Poetry** (`pyproject.toml` + committed `poetry.lock`). There is no `requirements.txt`. The **build backend is hatchling** and the version is `dynamic` from the `VERSION` file — Poetry stays the dependency manager in `package-mode = false`, so the lockfile and every `poetry install --no-root` path are unaffected. `make lint`/`make mypy` run **mount-only inside the luxlint image** (the repo installs nothing); `make test` builds from the lock and mounts current source — never exec into the baked container (stale code).

**Guard pins move often** — read `--changelog --since <pin>` and `--new-rules --since <pin>` before `make guard-upgrade`. A clean `--new-rules` is *not* "no impact": a tightened rule fires without being a new rule. `make check` is a pass/fail gate that stops at the first failure; **`make plan` is the full board** and includes work that does not turn the gate red (sweep findings, inert rules, un-adopted overlays).

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

## The run stacks

**ONE `compose.yml`**; each stack is a compose `profiles:` key, so a service is inert unless its profile is named. Two Sense monitors (tyle, bb) x two environments gives the four real-account stacks. Everything runs on the **bridge network** (Sense is a cloud API — no host networking), and only Grafana publishes a port. **Compose never builds** — the fake-Sense image is built by `make harness-build` and referenced by tag.

| Stack     | profile of `compose.yml` | source          | InfluxDB/Grafana          | make              |
| --------- | ------------------------ | --------------- | ------------------------- | ----------------- |
| dev-tyle  | `dev-tyle`               | real (tyle)     | bundled, auto-provisioned | `make dev-up`     |
| dev-bb    | `dev-bb`                 | real (bb)       | bundled, auto-provisioned | `make bb-up`      |
| prod-tyle | `prod-tyle`              | real (tyle)     | external (your fleet)     | `make prod-up`    |
| prod-bb   | `prod-bb`                | real (bb)       | external (your fleet)     | `make prod-bb-up` |
| demo      | `demo`                   | fake (emulator) | bundled, auto-provisioned | `make demo-up`    |
| test      | `e2e`                    | fake            | ephemeral, no Grafana     | `make test-e2e`   |

- Bundled `influxdb:2.7` + Grafana are dev/demo/test only. InfluxQL dashboards need a DBRP mapping (`ops/influxdb/init-dbrp.sh`); Grafana is provisioned via `grafana/provisioning/` (datasource pinned uid `uDxwFcOGz`; dashboards from `grafana/shared-local/`, using the `${data_source}` picker var).
- **Emulator**: `harness/fake_sense.py` — pure-stdlib Sense cloud fake (HTTP auth + REST + hand-rolled RFC 6455 WebSocket). Point the collector at it with `SENSE_COLLECTOR_API_BASE_URL` / `SENSE_COLLECTOR_WS_BASE_URL`. See `harness/README.md`.

## Configuration

All config is via `SENSE_COLLECTOR_*` environment variables (see `app/core/config.py` for the full validated list, and `docs/CONFIGURATION.md`). **Secrets live in a gitignored `.env.<stack>` per real-account stack** — `.env.dev-tyle`, `.env.dev-bb`, `.env.prod-tyle`, `.env.prod-bb` (copy from `.env.example`). `.env.demo` and `.env.e2e` are committed and hold no secrets. Run `make gitleaks-staged` before committing.

Required: `API_USERNAME`, `API_PASSWORD`, `INFLUXDB_URL`, `INFLUXDB_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` (all `SENSE_COLLECTOR_`-prefixed).

## Important Implementation Notes

1. **WebSocket message types** handled: `realtime_update`, `new_timeline_event`, `hello`, `data_change`, `device_states`, `device_states_changed`.
1. **Rate limiting**: strict Sense API limits — device names cached ~15 min to minimize calls.
1. **Device name resolution**: devices are queued for lookup when discovered; cache respects rate limits.
1. **InfluxDB writes** (fleet ingestion standard): the asyncio-native `InfluxDBClientAsync` (aiohttp), opened in `connect()`; each poll cycle's points are written as ONE `await write_api.write(...)` — no batch/flush knobs, no background buffer to flush. `ping()` fails fast on an unreachable server; auth/bucket errors (401/404) are logged and retried next cycle.
1. **Lifecycle & error handling** (fleet canonical spine): long-lived clients open in `connect()` and close in `close()` (never a fresh client/session per request); shutdown is `loop.add_signal_handler` + an `asyncio.Event` threaded into the periodic loops (interruptible `asyncio.wait_for(shutdown.wait(), timeout=…)` sleeps); env is read via `ConfigValidator` and logged once through `config.describe_settings()`; **every log call uses lazy `%s` args, never f-strings** (ruff `G` fails lint on a violation). The app logs-and-continues on individual failures — but the catches are **narrowed** to what can actually fail at each site (`MALFORMED_PAYLOAD` / `EXPORT_WRITE_ERRORS` per module), so a bare `except Exception` no longer hides an `AttributeError` from a real bug; the six genuine last-resort nets carry an inline `# swallowed-exceptions: <reason>` waiver. Do not widen these back. `make arch` (folded into `make check`) statically guards these so they can't drift.
1. **Docker**: four-stage `Dockerfile` (builder → builder-dev → base → dev). Prod pulls `:latest`; dev/demo/test pull/build `:dev`. `Dockerfile.lint` overlays current source for lint/test.
1. **NO AI attribution** in commits/PRs (house rule — no Co-Authored-By, "Generated with", robot emoji).

Release notes live in `CHANGELOG.md` at the repo root (Keep a Changelog, CalVer) — it is the public record and the source for the GitHub release body. Pre-fleet-standard history (`2025.7.x`) stays on the LuxPM "Changelog" page (project `SENSECOLLE`); work items are tracked as LuxPM issues.
