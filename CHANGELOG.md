# Change Log

Technical change logs live in **[`app/change_logs/`](app/change_logs/)** — one file per version, named to match the `VERSION` file (`app/change_logs/2026.09.0.md`).

These are written for developers and operators: issue IDs, refactors, infra and ops changes, deploy steps. For the plain-language version of what changed, see **[`RELEASE_NOTES.md`](RELEASE_NOTES.md)**.

The format is defined in [`app/change_logs/writing-guide.md`](app/change_logs/writing-guide.md); the release process is `luxarch --doc FLEET-RELEASE-PROCESS`.

## Versions

- [2026.09.0](app/change_logs/2026.09.0.md) — 2026-09-06 — security patches, health-check DST fix, narrowed exception handling

Releases before `2026.09.0` predate this standard and have no per-version file. `2026.8.0` (the first Luxardo Labs fleet-standard release) is described on its [GitHub release](https://github.com/luxardolabs/sense-collector/releases/tag/2026.8.0); the `2025.7.x` history is recorded internally.
