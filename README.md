# Sense Collector

![Sense Collector Header](docs/images/sense_collector_header.png)

**Sense Collector** is a headless Docker service that collects data from the [Sense](https://sense.com/) home energy monitor and stores it in InfluxDB for visualization in Grafana. It authenticates against the Sense cloud, streams the realtime WebSocket feed, resolves your devices via the REST API, and writes energy metrics to InfluxDB — reaching OUT to Sense and OUT to InfluxDB, with no inbound port.

## Key Features

- **Realtime collection**: streams whole-home mains (watts, volts, Hz) and per-device wattage from the Sense WebSocket feed.
- **Device resolution**: names your devices via the Sense API, cached to respect rate limits.
- **InfluxDB storage**: writes to InfluxDB 2.x for flexible, scalable time-series storage.
- **Grafana dashboards**: ready-to-import dashboards for device, mains, and monitor views.
- **Simple deploy**: a single multi-arch container, configured entirely through environment variables.

## Quick Start

```bash
cp .env.example .env.prod          # then edit: Sense credentials + InfluxDB connection
docker compose -f compose.prod.yml up -d
docker compose -f compose.prod.yml ps   # HEALTH shows healthy once data is flowing
```

The image is published at `ghcr.io/luxardolabs/sense-collector:latest`. Full walkthrough: **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)**.

## Documentation

- **[Getting Started](docs/GETTING-STARTED.md)** — prerequisites, install, run, and verify.
- **[Configuration](docs/CONFIGURATION.md)** — every environment variable.
- **[Grafana](docs/GRAFANA.md)** — data source setup and the dashboards.
- **[What the Collector Gathers](docs/COLLECTOR-DETAILS.md)** — the measurements and where they come from.

## Dashboards

Three Grafana dashboards ship in [`grafana/shared-local/`](grafana/shared-local): **Device Overview**, **Mains Overview**, and **Monitor & Detection**. See [docs/GRAFANA.md](docs/GRAFANA.md) for import and data-source setup.

## Support

Questions or issues? Please [open an issue](https://github.com/luxardolabs/sense-collector/issues).

## Acknowledgements

- [Grafana Labs](https://grafana.com/)
- [InfluxData](https://www.influxdata.com/)
- [Sense](https://sense.com/)

## License

Licensed under the [GNU Affero General Public License v3.0 (AGPL-3.0-only)](LICENSE).
