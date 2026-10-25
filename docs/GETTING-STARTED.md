# Getting Started

Sense Collector runs as a single Docker container. It authenticates against the Sense cloud, streams the realtime WebSocket feed, makes concurrent REST calls for device and monitor detail, and writes the results to InfluxDB for visualization in Grafana. It reaches OUT to Sense and OUT to InfluxDB — it publishes no inbound port.

## Prerequisites

- [Docker](https://docs.docker.com/install) and [Docker Compose](https://docs.docker.com/compose/install)
- [InfluxDB 2.x](https://docs.influxdata.com/influxdb/v2/) — the collector writes here (InfluxDB 1.x is not supported)
- [Grafana](https://grafana.com/oss/grafana/) — for the dashboards (see [GRAFANA.md](GRAFANA.md))
- A [Sense](https://sense.com/) account (the email and password you use in the Sense app)

## The image

The public multi-arch image (linux/amd64, linux/arm64) is published to GitHub Container Registry, one immutable tag per release:

```
ghcr.io/luxardolabs/sense-collector:<version>
```

Pin the release you run (see [Releases](https://github.com/luxardolabs/sense-collector/releases) for the newest), and upgrade by changing the tag: in your env file that is `TAG=<version>` (the template's `SENSE_IMAGE` reads it). A pinned tag always names the same bits, so you know what is running and can roll back to it.

**`:latest` is no longer updated.** It stays at `2026.10.2`, the last release that moved it; later releases publish only their version tag. If you run `:latest`, switch to a version tag.

## Configure

Copy the template and fill in your Sense credentials and InfluxDB connection. The template documents every variable inline; the full reference is in [CONFIGURATION.md](CONFIGURATION.md).

```bash
cp .env.example .env.prod-tyle
# edit .env.prod-tyle — set TAG=<version>, SENSE_COLLECTOR_API_USERNAME / _API_PASSWORD and the four INFLUXDB_* values
```

The six required variables:

| Variable                          | Description                       |
| --------------------------------- | --------------------------------- |
| `SENSE_COLLECTOR_API_USERNAME`    | Your Sense account email          |
| `SENSE_COLLECTOR_API_PASSWORD`    | Your Sense account password       |
| `SENSE_COLLECTOR_INFLUXDB_URL`    | URL of your InfluxDB 2.x instance |
| `SENSE_COLLECTOR_INFLUXDB_TOKEN`  | InfluxDB API token                |
| `SENSE_COLLECTOR_INFLUXDB_ORG`    | InfluxDB organization             |
| `SENSE_COLLECTOR_INFLUXDB_BUCKET` | InfluxDB bucket (e.g. `sense`)    |

Everything else is optional and has sensible defaults — see [CONFIGURATION.md](CONFIGURATION.md).

## Run

With Docker Compose (recommended — one `compose.yml`; pick the profile for your stack and its `.env.<stack>`):

```bash
docker compose --profile prod-tyle --env-file .env.prod-tyle up -d
```

Or with `docker run`:

```bash
docker run -d \
  --name sense-collector \
  --env-file .env.prod-tyle \
  --restart always \
  -v /mnt/docker/sense-collector/output:/app/output \
  ghcr.io/luxardolabs/sense-collector:<version>
```

The collector reaches out to the Sense cloud and your InfluxDB; it exposes no ports.

The output directory is only written when `SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA=true`. Then create it first and give it to the container's user (uid 1000), or the collector refuses to start: `sudo mkdir -p /mnt/docker/sense-collector/output && sudo chown 1000:1000 /mnt/docker/sense-collector/output`. Docker would otherwise create it owned by root.

## Verify

The container ships a Docker `HEALTHCHECK` driven by a heartbeat the collector touches for every WebSocket frame it can use, so an unhealthy container is a real liveness signal: a dead WebSocket, a stuck collector, or a Sense payload change that makes every frame unusable.

```bash
docker compose --profile prod-tyle ps        # HEALTH shows healthy once data is flowing
docker logs -f sense-collector               # watch it authenticate, connect the WS, and write
```

Within a minute you should see mains data (`sense_mains`) and your devices (`sense_devices`) landing in InfluxDB. What each measurement contains is described in [COLLECTOR-DETAILS.md](COLLECTOR-DETAILS.md).

## Try the full stack

To see a correctly wired end-to-end stack — collector, InfluxDB, and Grafana with the data source, DBRP mapping, and dashboards all provisioned — clone the repo and run the self-contained demo (no Sense account needed; it uses a built-in emulator):

```bash
make demo-up      # fake Sense + bundled InfluxDB + Grafana on http://localhost:13302
```

This is a working reference you can compare your own Grafana setup against. `make dev-up` is the same bundled stack but pointed at your real Sense account.

## Next steps

- [GRAFANA.md](GRAFANA.md) — add the InfluxQL data source and import the dashboards
- [CONFIGURATION.md](CONFIGURATION.md) — every environment variable
- [COLLECTOR-DETAILS.md](COLLECTOR-DETAILS.md) — what the collector gathers
