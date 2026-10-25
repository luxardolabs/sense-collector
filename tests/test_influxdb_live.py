"""InfluxDBStorage against a REAL InfluxDB 2.7 — the service the deployed stacks run.

`make test` starts an ephemeral `influxdb:2.7` (compose profile `test`, service
`sense_test_influxdb`) on an isolated network and passes its coordinates as TEST_INFLUXDB_*.
These tests write through the collector's own storage layer and read the points back with
Flux, so line protocol, precision, tags and auth are proven against the server, not a mock.
"""

import os
import time
import uuid
from datetime import UTC, datetime

import pytest
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

from app.storage.influxdb import InfluxDBStorage

ENV_KEYS = ("url", "token", "org", "bucket")


@pytest.fixture
def influx_params() -> dict[str, str]:
    params = {k: os.environ.get(f"TEST_INFLUXDB_{k.upper()}", "") for k in ENV_KEYS}
    missing = [f"TEST_INFLUXDB_{k.upper()}" for k, v in params.items() if not v]
    if missing:
        pytest.fail(
            f"{', '.join(missing)} unset — run the suite with `make test`, which starts "
            "the real InfluxDB these tests write to"
        )
    return params


async def _query(params: dict[str, str], flux: str) -> list[dict[str, object]]:
    async with InfluxDBClientAsync(
        url=params["url"], token=params["token"], org=params["org"]
    ) as client:
        tables = await client.query_api().query(flux)
    return [record.values for table in tables for record in table.records]


@pytest.mark.asyncio
async def test_realtime_cycle_round_trips(influx_params: dict[str, str]) -> None:
    monitor_id = f"live-{uuid.uuid4().hex[:12]}"
    epoch = int(time.time()) - 5
    storage = InfluxDBStorage(influx_params)
    await storage.connect()
    try:
        await storage.device_queue.set_device_name("dev-fridge", "Fridge")
        await storage.persist_realtime_data(
            monitor_id=monitor_id,
            hertz=60.0,
            total_current=12.5,
            total_watts=1450.0,
            epoch=epoch,
            voltage=[121.0, 120.5],
            devices=[{"id": "dev-fridge", "name": "dev-fridge", "w": 140.0}],
            channels=[700.0, 750.0],
        )
    finally:
        await storage.close()

    # 1 mains total + 2 legs + 1 o11y + 1 device, all in one batch.
    assert storage.write_stats == {"successful": 5, "failed": 0, "pending": 0}

    rows = await _query(
        influx_params,
        f'from(bucket: "{influx_params["bucket"]}") |> range(start: -1h) '
        f'|> filter(fn: (r) => r.monitor_id == "{monitor_id}")',
    )
    by_key = {
        (r["_measurement"], r.get("leg"), r.get("device_name"), r["_field"]): r
        for r in rows
    }

    mains = by_key[("sense_mains", None, None, "watts")]
    assert mains["_value"] == 1450.0
    # Written with second precision at the payload's epoch, not the write time.
    assert mains["_time"] == datetime.fromtimestamp(epoch, UTC)
    assert by_key[("sense_mains", "L2", None, "voltage")]["_value"] == 120.5
    # The device is tagged with the cached friendly name, not the raw id in the payload.
    assert by_key[("sense_devices", None, "Fridge", "watts")]["_value"] == 140.0
    assert ("sense_o11y", None, None, "time_difference") in by_key


@pytest.mark.asyncio
async def test_rejected_token_is_counted_not_raised(
    influx_params: dict[str, str],
) -> None:
    storage = InfluxDBStorage({**influx_params, "token": "not-a-valid-token"})
    # ping() is unauthenticated, so connect succeeds; the write is what the server refuses.
    await storage.connect()
    try:
        await storage.persist_monitor_status(
            f"live-{uuid.uuid4().hex[:12]}", {"monitor_info": {"online": True}}
        )
    finally:
        await storage.close()

    assert storage.write_stats["successful"] == 0
    assert storage.write_stats["failed"] == 1


@pytest.mark.asyncio
async def test_integral_json_numbers_do_not_conflict_with_float_fields(
    influx_params: dict[str, str],
) -> None:
    """InfluxDB fixes a field's type per shard: an int after a float is refused (422).

    Sense spells most readings as floats, but a whole number can arrive as a JSON int and
    an omitted device `w` defaults to 0. Both frames must land, whichever comes first.
    """
    monitor_id = f"live-{uuid.uuid4().hex[:12]}"
    epoch = int(time.time()) - 10
    storage = InfluxDBStorage(influx_params)
    await storage.connect()
    try:
        await storage.device_queue.set_device_name("dev-tv", "TV")
        frame = {
            "monitor_id": monitor_id,
            "total_current": 12,
            "voltage": [121, 120],
        }
        await storage.persist_realtime_data(
            **frame,
            hertz=60.0,
            total_watts=1450.0,
            epoch=epoch,
            devices=[{"id": "dev-tv", "w": 55.5, "sd": {"w": 1.5, "v": 120.0}}],
            channels=[700.0, 750.0],
        )
        await storage.persist_realtime_data(
            **frame,
            hertz=60,
            total_watts=1500,
            epoch=epoch + 1,
            devices=[{"id": "dev-tv", "sd": {"w": 2, "v": 120}}],
            channels=[700, 800],
        )
    finally:
        await storage.close()

    rows = await _query(
        influx_params,
        f'from(bucket: "{influx_params["bucket"]}") |> range(start: -1h) '
        f'|> filter(fn: (r) => r.monitor_id == "{monitor_id}")',
    )
    second = {
        (r["_measurement"], r.get("leg"), r["_field"]): r["_value"]
        for r in rows
        if r["_time"] == datetime.fromtimestamp(epoch + 1, UTC)
    }
    assert second[("sense_mains", None, "watts")] == 1500.0
    assert second[("sense_mains", None, "hertz")] == 60.0
    assert second[("sense_mains", "L2", "watts")] == 800.0
    assert second[("sense_devices", None, "watts")] == 0.0
    assert second[("sense_devices", None, "sd_watts")] == 2.0
    assert storage.write_stats["failed"] == 0
