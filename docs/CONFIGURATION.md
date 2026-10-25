# Sense Collector Configuration Guide

This document describes all available configuration options for Sense Collector. All configuration is done through environment variables with the `SENSE_COLLECTOR_` prefix.

## Table of Contents

- [Required Configuration](#required-configuration)
- [API Configuration](#api-configuration)
- [WebSocket Configuration](#websocket-configuration)
- [HTTP Configuration](#http-configuration)
- [Connection Pool Configuration](#connection-pool-configuration)
- [Queue Configuration](#queue-configuration)
- [Device Configuration](#device-configuration)
- [InfluxDB Configuration](#influxdb-configuration)
- [Logging Configuration](#logging-configuration)
- [Export Configuration](#export-configuration)
- [Data Processing Configuration](#data-processing-configuration)
- [Measurement Names](#measurement-names)

## Required Configuration

These environment variables MUST be set for the application to run:

| Variable                          | Description                                                                                                                | Example                 |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| `SENSE_COLLECTOR_API_USERNAME`    | Your Sense account email                                                                                                   | `user@example.com`      |
| `SENSE_COLLECTOR_API_PASSWORD`    | Your Sense account password                                                                                                | `your-password`         |
| `SENSE_COLLECTOR_INFLUXDB_URL`    | InfluxDB server URL                                                                                                        | `http://localhost:8086` |
| `SENSE_COLLECTOR_INFLUXDB_TOKEN`  | InfluxDB authentication token. A stack that reads the real Sense cloud refuses the demo/e2e tokens committed in this repo. | `your-token`            |
| `SENSE_COLLECTOR_INFLUXDB_ORG`    | InfluxDB organization                                                                                                      | `your-org`              |
| `SENSE_COLLECTOR_INFLUXDB_BUCKET` | InfluxDB bucket name                                                                                                       | `sense-data`            |

## API Configuration

### URLs and Endpoints

| Variable                       | Default                                   | Description        |
| ------------------------------ | ----------------------------------------- | ------------------ |
| `SENSE_COLLECTOR_API_BASE_URL` | `https://api.sense.com/apiservice/api/v1` | Sense API base URL |
| `SENSE_COLLECTOR_WS_BASE_URL`  | `wss://clientrt.sense.com`                | WebSocket base URL |

No URL setting takes credentials. Startup refuses any `@` (a `user:password@`, encoded or not) in `SENSE_COLLECTOR_API_BASE_URL`, `SENSE_COLLECTOR_WS_BASE_URL` or `SENSE_COLLECTOR_INFLUXDB_URL`, because Sense authenticates with the account login and InfluxDB with its token, and the URL appears in error log lines.

### Headers and Versions

| Variable                              | Default              | Description                           |
| ------------------------------------- | -------------------- | ------------------------------------- |
| `SENSE_COLLECTOR_CLIENT_VERSION`      | `3.0.0`              | Client version sent in headers        |
| `SENSE_COLLECTOR_PROTOCOL_VERSION`    | `3`                  | Protocol version for X-Sense-Protocol |
| `SENSE_COLLECTOR_USER_AGENT`          | `okhttp/3.8.0`       | User agent for API requests           |
| `SENSE_COLLECTOR_USER_AGENT_INTERNAL` | `SenseCollector/3.0` | Internal user agent                   |

### Token Management

| Variable                               | Default            | Min  | Max   | Description                                                                                                                                         |
| -------------------------------------- | ------------------ | ---- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SENSE_COLLECTOR_API_TOKEN_RENEW`      | `43200` (12 hours) | 3600 | 86400 | Token renewal interval in seconds                                                                                                                   |
| `SENSE_COLLECTOR_AUTH_RETRY_MAX_DELAY` | `900`              | 60   | 86400 | Longest wait after a failed login before the next attempt (the current token is kept meanwhile); a login 429's `Retry-After` is honoured up to this |
| `SENSE_COLLECTOR_AUTH_MIN_REAUTH_AGE`  | `60`               | 0    | 3600  | A REST 401 logs in again only if the token is older than this; a fresh token that gets 401 is not expired                                           |

### Rate Limiting

| Variable                                    | Default | Min  | Max  | Description                                            |
| ------------------------------------------- | ------- | ---- | ---- | ------------------------------------------------------ |
| `SENSE_COLLECTOR_API_RATE_LIMIT_CONCURRENT` | `10`    | 1    | 50   | Maximum concurrent API calls                           |
| `SENSE_COLLECTOR_API_MIN_INTERVAL`          | `0.1`   | 0.01 | 5.0  | Minimum interval between API calls (seconds)           |
| `SENSE_COLLECTOR_API_RETRY_MAX`             | `3`     | 1    | 10   | Maximum API call retries                               |
| `SENSE_COLLECTOR_API_RETRY_BACKOFF_BASE`    | `2`     | 2    | 10   | Exponential backoff base for retries                   |
| `SENSE_COLLECTOR_API_RETRY_AFTER_MAX`       | `300`   | 10   | 3600 | Longest a 429 Retry-After may park a request (seconds) |

### Fetch Intervals

| Variable                                  | Default | Min | Max   | Description                             |
| ----------------------------------------- | ------- | --- | ----- | --------------------------------------- |
| `SENSE_COLLECTOR_MONITOR_STATUS_INTERVAL` | `60`    | 30  | 3600  | Monitor status fetch interval (seconds) |
| `SENSE_COLLECTOR_DEVICE_LIST_INTERVAL`    | `3600`  | 300 | 86400 | Device list fetch interval (seconds)    |

## WebSocket Configuration

| Variable                                     | Default | Min | Max  | Description                                                           |
| -------------------------------------------- | ------- | --- | ---- | --------------------------------------------------------------------- |
| `SENSE_COLLECTOR_WS_HEARTBEAT_INTERVAL`      | `10`    | 1   | 300  | WebSocket heartbeat interval (seconds)                                |
| `SENSE_COLLECTOR_WS_HEARTBEAT_TIMEOUT`       | `30`    | 5   | 600  | WebSocket heartbeat timeout (seconds)                                 |
| `SENSE_COLLECTOR_WS_CLOSE_TIMEOUT`           | `2.0`   | 0.5 | 10   | Seconds to wait for the server's close frame on shutdown or reconnect |
| `SENSE_COLLECTOR_WS_RECONNECT_DELAY_INITIAL` | `5`     | 1   | 60   | Initial reconnection delay (seconds)                                  |
| `SENSE_COLLECTOR_WS_RECONNECT_DELAY_CAP`     | `60`    | 10  | 3600 | Maximum reconnection delay (seconds)                                  |
| `SENSE_COLLECTOR_WS_HEALTH_LOG_INTERVAL`     | `300`   | 60  | 3600 | Health status log interval (seconds)                                  |
| `SENSE_COLLECTOR_WS_HEALTH_CHECK_INTERVAL`   | `1`     | 1   | 10   | Health check interval (seconds)                                       |

## Container Health Check

| Variable                                    | Default                                                                                            | Min | Max                                         | Description                                                                                          |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------- | --- | ------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `SENSE_COLLECTOR_HEALTH_CHECK_MAX_AGE`      | `120`                                                                                              | 10  | 86400                                       | Max heartbeat age before unhealthy (seconds); the collector and the healthcheck read it the same way |
| `SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE`     | `<export folder>/.heartbeat`, or `/tmp/sense-collector.heartbeat` when that folder is not writable | —   | —                                           | Heartbeat file the collector touches as data flows; the healthcheck picks the same default           |
| `SENSE_COLLECTOR_HEALTH_HEARTBEAT_INTERVAL` | `15`                                                                                               | 1   | half of `HEALTH_CHECK_MAX_AGE`, at most 300 | Min seconds between heartbeat touches (throttle)                                                     |

## HTTP Configuration

| Variable                               | Default | Min | Max | Description                              |
| -------------------------------------- | ------- | --- | --- | ---------------------------------------- |
| `SENSE_COLLECTOR_HTTP_TIMEOUT_TOTAL`   | `60`    | 10  | 300 | Total HTTP request timeout (seconds)     |
| `SENSE_COLLECTOR_HTTP_TIMEOUT_CONNECT` | `10`    | 5   | 60  | HTTP connection timeout (seconds)        |
| `SENSE_COLLECTOR_HTTP_TIMEOUT_READ`    | `30`    | 10  | 120 | HTTP read timeout (seconds)              |
| `SENSE_COLLECTOR_HTTP_AUTH_TIMEOUT`    | `30`    | 10  | 60  | Authentication request timeout (seconds) |

## Connection Pool Configuration

| Variable                                   | Default | Min | Max  | Description                 |
| ------------------------------------------ | ------- | --- | ---- | --------------------------- |
| `SENSE_COLLECTOR_CONN_POOL_LIMIT`          | `100`   | 10  | 500  | Total connection pool limit |
| `SENSE_COLLECTOR_CONN_POOL_LIMIT_PER_HOST` | `30`    | 5   | 100  | Per-host connection limit   |
| `SENSE_COLLECTOR_CONN_POOL_TTL_DNS`        | `300`   | 60  | 3600 | DNS cache TTL (seconds)     |

## Queue Configuration

| Variable                              | Default | Min | Max   | Description                        |
| ------------------------------------- | ------- | --- | ----- | ---------------------------------- |
| `SENSE_COLLECTOR_QUEUE_SIZE_API`      | `1000`  | 100 | 10000 | API call queue size                |
| `SENSE_COLLECTOR_QUEUE_SIZE_DEVICE`   | `1000`  | 100 | 10000 | Device data queue size             |
| `SENSE_COLLECTOR_QUEUE_TIMEOUT`       | `1.0`   | 0.1 | 10.0  | Queue get timeout (seconds)        |
| `SENSE_COLLECTOR_QUEUE_BATCH_SIZE`    | `100`   | 10  | 1000  | Batch processing size              |
| `SENSE_COLLECTOR_QUEUE_BATCH_TIMEOUT` | `1.0`   | 0.1 | 10.0  | Batch collection timeout (seconds) |

## Device Configuration

| Variable                                        | Default | Min | Max  | Description                     |
| ----------------------------------------------- | ------- | --- | ---- | ------------------------------- |
| `SENSE_COLLECTOR_DEVICE_CACHE_EXPIRY_SECONDS`   | `900`   | 300 | 3600 | Device cache expiry (seconds)   |
| `SENSE_COLLECTOR_DEVICE_CACHE_MAX_SIZE`         | `500`   | 50  | 5000 | Maximum cached devices          |
| `SENSE_COLLECTOR_DEVICE_MAX_CONCURRENT_LOOKUPS` | `4`     | 1   | 20   | Concurrent device lookups       |
| `SENSE_COLLECTOR_DEVICE_LOOKUP_DELAY_SECONDS`   | `0.5`   | 0.1 | 10.0 | Delay between lookups (seconds) |

## InfluxDB Configuration

| Variable                               | Default | Min  | Max    | Description                     |
| -------------------------------------- | ------- | ---- | ------ | ------------------------------- |
| `SENSE_COLLECTOR_INFLUXDB_TIMEOUT`     | `30000` | 5000 | 120000 | InfluxDB timeout (milliseconds) |
| `SENSE_COLLECTOR_INFLUXDB_ENABLE_GZIP` | `true`  | -    | -      | Enable gzip compression         |

## Logging Configuration

The collector logs the fleet way (`app/core/logging_config.py`, emitted by luxarch): one JSON object per line on stdout, with the caller's fields under `attributes`. Collect it with your container log driver (`docker logs`, Loki, …); there is no file output. Every line passes through a redactor: the WebSocket access token is replaced with `[REDACTED]` and control characters are escaped, at any level and in either format.

| Variable                            | Default | Values                                | Description                                              |
| ----------------------------------- | ------- | ------------------------------------- | -------------------------------------------------------- |
| `SENSE_COLLECTOR_LOG_LEVEL_API`     | `INFO`  | DEBUG, INFO, WARNING, ERROR, CRITICAL | API (Sense REST + WebSocket) logging level               |
| `SENSE_COLLECTOR_LOG_LEVEL_STORAGE` | `INFO`  | DEBUG, INFO, WARNING, ERROR, CRITICAL | Storage (InfluxDB) logging level                         |
| `SENSE_COLLECTOR_LOG_LEVEL_GENERAL` | `INFO`  | DEBUG, INFO, WARNING, ERROR, CRITICAL | General logging level                                    |
| `LOG_LEVEL`                         | `INFO`  | DEBUG, INFO, WARNING, ERROR, CRITICAL | Root level (third-party libraries)                       |
| `LOG_MODULE_LEVELS`                 | \`\`    | JSON object                           | Per-logger overrides at runtime, e.g. `{"api": "DEBUG"}` |
| `LOG_FORMAT`                        | `json`  | json, text                            | `text` gives a readable line for local development only  |

A number outside a setting's Min/Max is clamped to the nearest bound (and logged); a value that is not a number falls back to the default.

## Export Configuration

| Variable                                    | Default         | Description                           |
| ------------------------------------------- | --------------- | ------------------------------------- |
| `SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA`      | `false`         | Export raw WebSocket data to files    |
| `SENSE_COLLECTOR_EXPORT_FOLDER`             | `output`        | Export directory path                 |
| `SENSE_COLLECTOR_EXPORT_FILE_RECEIVED_DATA` | `received_data` | Filename for received data            |
| `SENSE_COLLECTOR_EXPORT_FILE_DEVICE_PREFIX` | `device_`       | Prefix for device files               |
| `SENSE_COLLECTOR_EXPORT_FILE_EXTENSION`     | `.json`         | File extension for exports            |
| `SENSE_COLLECTOR_FILE_MAX_DEVICE_ID_LENGTH` | `100`           | Maximum device ID length in filenames |

With exports on, the collector writes `device_<id>.json` per device, `devices.json` (the device list) and `received_data.jsonl` (raw WebSocket frames), each readable by its owner only (0600). In a device ID, characters other than letters, numbers, `_` and `-` are percent-encoded, so two devices never share a file; an ID too long for the limit is cut and given a hash of the whole ID.

## Data Processing Configuration

| Variable                                | Default | Min    | Max   | Description                                                            |
| --------------------------------------- | ------- | ------ | ----- | ---------------------------------------------------------------------- |
| `SENSE_COLLECTOR_MAX_CHANNELS`          | `2`     | 1      | 10    | Maximum power channels to process                                      |
| `SENSE_COLLECTOR_YEARLY_COST_DIVISOR`   | `100`   | 1      | 1000  | Divisor for yearly cost values (`sense_devices` and `sense_always_on`) |
| `SENSE_COLLECTOR_DEFAULT_VOLTAGE`       | `0.0`   | 0.0    | 240.0 | Default voltage value                                                  |
| `SENSE_COLLECTOR_DEFAULT_WIFI_STRENGTH` | `0.0`   | -100.0 | 0.0   | Default WiFi strength                                                  |

## Measurement Names

Customize InfluxDB measurement names:

| Variable                                           | Default                      | Description                  |
| -------------------------------------------------- | ---------------------------- | ---------------------------- |
| `SENSE_COLLECTOR_MEASUREMENT_MAINS`                | `sense_mains`                | Main power data              |
| `SENSE_COLLECTOR_MEASUREMENT_O11Y`                 | `sense_o11y`                 | Observability metrics        |
| `SENSE_COLLECTOR_MEASUREMENT_DEVICES`              | `sense_devices`              | Device power data            |
| `SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON`            | `sense_always_on`            | Always-on device data        |
| `SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON_COMPARISON` | `sense_always_on_comparison` | Comparison data              |
| `SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON_DEVICES`    | `sense_always_on_devices`    | Individual always-on devices |
| `SENSE_COLLECTOR_MEASUREMENT_EVENT`                | `sense_event`                | Timeline events              |
| `SENSE_COLLECTOR_MEASUREMENT_HELLO`                | `hello_event`                | Connection status            |
| `SENSE_COLLECTOR_MEASUREMENT_DATA_CHANGE`          | `data_change_event`          | Data change events           |
| `SENSE_COLLECTOR_MEASUREMENT_DEVICE_STATE`         | `device_state_event`         | Device state changes         |
| `SENSE_COLLECTOR_MEASUREMENT_MONITOR_STATUS`       | `sense_monitor_status`       | Monitor status               |
| `SENSE_COLLECTOR_MEASUREMENT_DEVICE_DETECTION`     | `sense_device_detection`     | Device detection progress    |

## Worker Configuration

| Variable                                 | Default | Min | Max | Description                            |
| ---------------------------------------- | ------- | --- | --- | -------------------------------------- |
| `SENSE_COLLECTOR_API_WORKER_ERROR_SLEEP` | `1`     | 1   | 60  | Sleep after API worker error (seconds) |

## Example Configuration File

Configuration is loaded from an env file. Copy `.env.example` to a **gitignored** `.env.dev` (or `.env.prod`) and fill it in — only `.env.example` is committed. Example `.env.dev`:

```bash
TZ=America/Chicago

# Required Configuration
SENSE_COLLECTOR_API_USERNAME=your-email@example.com
SENSE_COLLECTOR_API_PASSWORD=your-password
SENSE_COLLECTOR_INFLUXDB_URL=http://influxdb.example.com:8086
SENSE_COLLECTOR_INFLUXDB_TOKEN=your-influxdb-token
SENSE_COLLECTOR_INFLUXDB_ORG=your-org
SENSE_COLLECTOR_INFLUXDB_BUCKET=sense

# Optional: Increase logging for debugging
SENSE_COLLECTOR_LOG_LEVEL_API=DEBUG
SENSE_COLLECTOR_LOG_LEVEL_STORAGE=DEBUG
SENSE_COLLECTOR_LOG_LEVEL_GENERAL=DEBUG

# Optional: Readable log lines instead of JSON (local development only)
LOG_FORMAT=text

# Optional: Export raw data (written under the gitignored output/ dir)
SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA=true
SENSE_COLLECTOR_EXPORT_FOLDER=output

# Optional: Adjust intervals
SENSE_COLLECTOR_MONITOR_STATUS_INTERVAL=120  # 2 minutes
SENSE_COLLECTOR_DEVICE_LIST_INTERVAL=7200    # 2 hours
```

Then run the collector — via Docker (the normal path):

```bash
make dev-up      # the dev-tyle stack: collector + bundled InfluxDB + Grafana (uses .env.dev)
```

The dev stacks bundle their own InfluxDB and Grafana, so their env file also needs the values those services are set up with on first start (the commented dev-stack block in `.env.example`): `INFLUX_USER`, `INFLUX_PASSWORD`, `INFLUX_ORG`, `INFLUX_BUCKET`, `INFLUX_TOKEN` (the same token as `SENSE_COLLECTOR_INFLUXDB_TOKEN`, and never the demo token), `GRAFANA_PASSWORD` and `GRAFANA_INFLUX_PASSWORD` (Grafana's read-only InfluxDB sign-in). Grafana listens on `127.0.0.1` only; set `GRAFANA_BIND=0.0.0.0` to reach it from the LAN.

…or directly for local hacking (env must be exported into the shell first):

```bash
set -a; . ./.env.dev; set +a
python -m app.main
```
