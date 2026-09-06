# Release Notes

Release notes live in **[`app/release_notes/`](app/release_notes/)** — one file per version, named to match the `VERSION` file (`app/release_notes/2026.09.0.md`).

These are written for the people running this collector: what you would actually notice, in plain language. For the technical detail behind each release, see **[`CHANGELOG.md`](CHANGELOG.md)**.

The format is defined in [`app/release_notes/writing-guide.md`](app/release_notes/writing-guide.md).

## Versions

- [2026.09.0](app/release_notes/2026.09.0.md) — 2026-09-06 — security patches and a health-check fix; drop-in upgrade

## Surfaces

Sense Collector is headless — it has no user interface, so the surface badges are labels only and nothing renders them. This repo uses two:

- `Collector` — the running collector itself: what it reads, writes and logs.
- `Deployment` — the image, compose stacks and anything you run to operate it.
