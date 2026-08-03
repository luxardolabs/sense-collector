# What the Collector Gathers

Sense Collector combines two Sense data sources — the realtime WebSocket feed and the REST API — and writes them to InfluxDB as the measurements below. Realtime electrical data arrives on the WebSocket; device names, monitor status, and detection detail come from periodic REST polls (device names are cached to respect Sense's API rate limits).

## Measurements

| Measurement            | Source           | Contains                                                                                                                         |
| ---------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `sense_mains`          | WebSocket        | Whole-home mains: total wattage, per-leg voltage, and line frequency (Hz).                                                       |
| `sense_devices`        | WebSocket + REST | Per-device realtime wattage, with device names resolved and cached from the API.                                                 |
| `sense_event`          | WebSocket        | Timeline events — device on/off/idle state changes.                                                                              |
| `sense_monitor_status` | REST             | Sense monitor health and connectivity: online state, IP/MAC, Wi-Fi SSID and signal strength, serial, and detection (NDT) status. |
| `sense_always_on`      | REST             | The always-on baseline draw Sense attributes to your home.                                                                       |
| `sense_o11y`           | Collector        | Collector observability — internal metrics about the collector's own operation for troubleshooting.                              |

## The three collection paths

**Mains and devices (realtime).** The WebSocket `realtime_update` stream drives `sense_mains` (voltage, watts, Hz) and `sense_devices` (per-device wattage). This is the high-frequency data behind the Device Overview and Mains Overview dashboards.

**Device detail (REST).** Devices discovered on the stream are queued for name resolution against the Sense API; the results are cached (~15 minutes by default) so the collector stays within Sense's rate limits. Smart-plug devices additionally report voltage and amperage.

**Monitor status (REST).** A periodic poll records the monitor's own health into `sense_monitor_status` — whether it is online, its network detail, Wi-Fi signal, and whether device detection is active. This is what the Monitor & Detection dashboard visualizes.
