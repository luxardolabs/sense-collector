#!/usr/bin/env bash
# End-to-end harness runner: fake Sense endpoint -> collector -> InfluxDB, no hardware.
# Brings up compose.e2e.yml, waits for the collector to authenticate, stream the realtime
# WebSocket, and write mains + device data to InfluxDB, then asserts it landed. Always
# tears the stack down. Driven by `make test-e2e` (which builds + passes SENSE_IMAGE).
set -euo pipefail

DC="docker compose -f compose.e2e.yml"
TOKEN="sense-e2e-token"
MONITOR_ID="12345"
# Device names the fake streams in realtime_update — must reach the sense_devices measurement.
EXPECTED_DEVICES=("AC Unit" "Refrigerator")
TIMEOUT="${E2E_TIMEOUT:-120}"

cleanup() { $DC down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "▶ building fake Sense + collector, starting e2e stack (SENSE_IMAGE=${SENSE_IMAGE:-default})…"
$DC up -d --build

# Query InfluxDB (InfluxQL over the v1-compat API) from inside the influx container.
influx_query() {
  $DC exec -T sense_influxdb curl -s -G "http://localhost:8086/query" \
    --data-urlencode "db=sense" \
    --data-urlencode "q=$1" \
    -H "Authorization: Token ${TOKEN}" 2>/dev/null || true
}

echo "▶ waiting up to ${TIMEOUT}s for mains data to appear in InfluxDB…"
deadline=$(( SECONDS + TIMEOUT ))
mains_ok=""
while [ "$SECONDS" -lt "$deadline" ]; do
  resp="$(influx_query "SHOW TAG VALUES FROM \"sense_mains\" WITH KEY = \"monitor_id\"")"
  if echo "$resp" | grep -q "$MONITOR_ID"; then
    mains_ok="$resp"
    break
  fi
  sleep 5
done

if [ -z "$mains_ok" ]; then
  echo "✗ FAIL: no sense_mains data for monitor ${MONITOR_ID} within ${TIMEOUT}s"
  echo "---- collector logs ----"; $DC logs --tail=80 sense-collector || true
  echo "---- fake logs ----"; $DC logs --tail=20 sense_fake || true
  exit 1
fi
echo "✓ PASS: sense_mains has data for monitor ${MONITOR_ID}"

# Device names from the realtime feed must reach sense_devices.
dev_resp="$(influx_query "SHOW TAG VALUES FROM \"sense_devices\" WITH KEY = \"device_name\"")"
missing=()
for name in "${EXPECTED_DEVICES[@]}"; do
  echo "$dev_resp" | grep -q "$name" || missing+=("$name")
done
if [ "${#missing[@]}" -ne 0 ]; then
  echo "✗ FAIL: sense_devices missing expected device(s): ${missing[*]}"
  echo "   got: $dev_resp"
  echo "---- collector logs ----"; $DC logs --tail=80 sense-collector || true
  exit 1
fi
echo "✓ PASS: sense_devices contains the streamed devices: ${EXPECTED_DEVICES[*]}"

# The collector must still be running (didn't crash on any message type).
if ! $DC ps --status running --services | grep -q '^sense-collector$'; then
  echo "✗ FAIL: collector is not running (may have crashed)"
  $DC logs --tail=80 sense-collector || true
  exit 1
fi

count="$(influx_query "SELECT COUNT(\"watts\") FROM \"sense_mains\"")"
echo "✓ collector healthy. influx sense_mains watts count: ${count}"
echo "✓ e2e PASSED"
