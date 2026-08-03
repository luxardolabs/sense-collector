# Grafana

The dashboards query InfluxDB with **InfluxQL**, so you configure the Grafana data source with token authentication via a custom HTTP header. The dashboard definitions live in [`grafana/shared-local/`](../grafana/shared-local).

## Data source

Create an InfluxDB data source in Grafana pointed at your InfluxDB 2.x instance, using **InfluxQL** as the query language. Because InfluxQL talks to a v2 bucket, authenticate with your InfluxDB API token as a custom header rather than the user/password fields:

1. **Connection → URL**: your InfluxDB URL (e.g. `http://influxdb.example.com:8086`).
1. **Query Language**: InfluxQL.
1. **Custom HTTP Headers → Add header**:
   - **Header**: `Authorization`
   - **Value**: `Token <your-influxdb-api-token>` (the literal word `Token`, a space, then the token).
1. **Database**: your bucket name (e.g. `sense`); leave user/password blank.

InfluxQL against a v2 bucket also requires a **DBRP mapping** (database/retention-policy → bucket) in InfluxDB. See the InfluxData guide for the current steps: <https://docs.influxdata.com/influxdb/v2/tools/grafana/?t=InfluxQL>.

> The bundled dev/demo stacks (`make dev-up` / `make demo-up`) provision this data source and the DBRP mapping automatically — this section is for wiring the collector into your own Grafana.

## Dashboards

Import the three dashboards from [`grafana/shared-local/`](../grafana/shared-local) (Grafana → Dashboards → New → Import → Upload JSON). Each has dropdowns to filter by device, choose a smoothing interval, toggle On/Off/Idle status annotations, jump between dashboards, and set the time range.

### Device Overview

`sense_collector-device_overview.json` — the primary dashboard.

- **Current Wattage**: a bubble chart of current wattage by device (larger circle = more watts).
- **Wattage By Device (stacked)**: wattage over time per device, stacked to total household draw.
- **Device Status**: a state timeline of On/Off/Idle events from the monitor.
- **Event Timeline**: a table of each device's state changes.
- **Device Details**: current, monthly, and yearly per-device statistics.
- **Smart Device Details**: voltage and amperage for [Sense-compatible smart plugs](https://help.sense.com/hc/en-us/articles/360012089393).

### Mains Overview

`sense_collector-mains_overview.json` — whole-home electrical service.

- **Wattage (stacked)**, **Voltages**, and **Frequency** panels.
- Toggle between the two legs of the split-phase service.
- On/Off/Idle event annotations can be overlaid.

### Monitor & Detection

`sense_collector-monitor_and_detection.json` — observability of the Sense monitor itself: detection status, Wi-Fi signal strength, and connectivity detail (from the `sense_monitor_status` measurement).
