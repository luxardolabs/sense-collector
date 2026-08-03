# Getting Started

Sense Collector runs as a single Docker container. It authenticates against the Sense cloud, streams the realtime WebSocket feed, makes concurrent REST calls for device and monitor detail, and writes the results to InfluxDB for visualization in Grafana. It reaches OUT to Sense and OUT to InfluxDB — it publishes no inbound port.

## Prerequisites

- [Docker](https://docs.docker.com/install) and [Docker Compose](https://docs.docker.com/compose/install)
- [InfluxDB 2.x](https://docs.influxdata.com/influxdb/v2/) — the collector writes here (InfluxDB 1.x is not supported)
- [Grafana](https://grafana.com/oss/grafana/) — for the dashboards (see [GRAFANA.md](GRAFANA.md))
- A [Sense](https://sense.com/) account (the email and password you use in the Sense app)

## The image

The public multi-arch image (linux/amd64, linux/arm64) is published to GitHub Container Registry:

```
ghcr.io/luxardolabs/sense-collector:latest
ghcr.io/luxardolabs/sense-collector:2026.8.0
```

## Configure

Copy the template and fill in your Sense credentials and InfluxDB connection. The template documents every variable inline; the full reference is in [CONFIGURATION.md](CONFIGURATION.md).

```bash
cp .env.example .env.prod
# edit .env.prod — set SENSE_COLLECTOR_API_USERNAME / _API_PASSWORD and the four INFLUXDB_* values
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

With Docker Compose (recommended — uses `compose.prod.yml` and your `.env.prod`):

```bash
docker compose -f compose.prod.yml up -d
```

Or with `docker run`:

```bash
docker run -d \
  --name sense-collector \
  --env-file .env.prod \
  --restart always \
  -v /mnt/docker/sense-collector/output:/app/output \
  ghcr.io/luxardolabs/sense-collector:latest
```

The collector reaches out to the Sense cloud and your InfluxDB; it exposes no ports.

## Verify

The container ships a Docker `HEALTHCHECK` driven by a heartbeat the collector touches on every WebSocket message, so an unhealthy container is a real liveness signal.

```bash
docker compose -f compose.prod.yml ps        # HEALTH shows healthy once data is flowing
docker logs -f sense-collector               # watch it authenticate, connect the WS, and write
```

Within a minute you should see mains data (`sense_mains`) and your devices (`sense_devices`) landing in InfluxDB. What each measurement contains is described in [COLLECTOR-DETAILS.md](COLLECTOR-DETAILS.md).

## Next steps

- [GRAFANA.md](GRAFANA.md) — add the InfluxQL data source and import the dashboards
- [CONFIGURATION.md](CONFIGURATION.md) — every environment variable
- [COLLECTOR-DETAILS.md](COLLECTOR-DETAILS.md) — what the collector gathers
