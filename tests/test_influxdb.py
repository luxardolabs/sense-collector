from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from influxdb_client import Point
from influxdb_client.client.exceptions import InfluxDBError

from app.storage.influxdb import DeviceDataQueue, InfluxDBStorage


@pytest.fixture
def influxdb_params():
    """Create test InfluxDB parameters."""
    return {
        "url": "http://localhost:8086",
        "token": "test-token",
        "org": "test-org",
        "bucket": "test-bucket",
    }


@pytest.fixture
def mock_influxdb_client():
    """Patch the asyncio-native client so start()/shutdown() never touch the network."""
    with patch("app.storage.influxdb.InfluxDBClientAsync") as mock_cls:
        client = MagicMock()
        client.ping = AsyncMock(return_value=True)
        client.close = AsyncMock()
        client.write_api = MagicMock(return_value=AsyncMock())
        mock_cls.return_value = client
        yield client


@pytest.mark.asyncio
async def test_write_points_writes_batch(influxdb_params):
    """write_points awaits ONE batched write for the cycle and counts it."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = AsyncMock()
    await storage.write_points([Point("test").field("value", 1)])
    storage.write_api.write.assert_awaited_once()
    assert storage.write_stats["successful"] == 1


@pytest.mark.asyncio
async def test_write_points_empty_is_noop(influxdb_params):
    """An empty point list must not hit the write API."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = AsyncMock()
    await storage.write_points([])
    storage.write_api.write.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_error_logged_not_raised(influxdb_params):
    """A write failure is caught + counted, never raised — the next cycle retries."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = AsyncMock()
    storage.write_api.write.side_effect = InfluxDBError(message="boom")
    await storage.write_points([Point("test").field("value", 1)])  # must not raise
    assert storage.write_stats["failed"] == 1


@pytest.mark.asyncio
async def test_device_data_queue_add_and_get():
    """Test adding and getting from device data queue."""
    queue = DeviceDataQueue(max_size=10)

    # Add item
    await queue.add_device_data("device1", "monitor1", 100.0, 1234567890)

    # Get item
    item = await queue.get_next_item()

    assert item is not None
    assert item["device_id"] == "device1"
    assert item["watts"] == 100.0


@pytest.mark.asyncio
async def test_device_data_queue_full():
    """Test queue behavior when full."""
    queue = DeviceDataQueue(max_size=1)

    # Fill queue
    await queue.add_device_data("device1", "monitor1", 100.0, 1234567890)

    # Try to add another (should drop)
    await queue.add_device_data("device2", "monitor1", 200.0, 1234567890)

    # Only first item should be retrievable
    item = await queue.get_next_item()
    assert item["device_id"] == "device1"

    # Queue should be empty now
    with patch("app.core.config.QUEUE_TIMEOUT", 0.1):
        item2 = await queue.get_next_item()
        assert item2 is None


@pytest.mark.asyncio
async def test_device_data_queue_name_cache():
    """Test device name caching."""
    queue = DeviceDataQueue()

    # Set and get device name
    await queue.set_device_name("device1", "Test Device")
    name = await queue.get_device_name("device1")

    assert name == "Test Device"

    # Non-existent device
    name2 = await queue.get_device_name("device2")
    assert name2 is None


@pytest.mark.asyncio
async def test_influxdb_storage_init_success(influxdb_params):
    """Test successful InfluxDB storage initialization (no I/O — client opens in start())."""
    storage = InfluxDBStorage(influxdb_params)

    assert storage.bucket == "test-bucket"
    assert storage.org == "test-org"
    # __init__ does no I/O — the async client + queue processor come up in start().
    assert storage.client is None
    assert storage.write_api is None
    assert storage.queue_processor_task is None


@pytest.mark.asyncio
async def test_influxdb_storage_init_validation_error():
    """Test InfluxDB storage initialization with missing parameters."""
    invalid_params = {"url": "http://localhost:8086"}  # Missing required fields

    with pytest.raises(ValueError, match="Missing required InfluxDB parameter: token"):
        InfluxDBStorage(invalid_params)


@pytest.mark.asyncio
async def test_influxdb_storage_init_invalid_url():
    """Test InfluxDB storage initialization with invalid URL."""
    invalid_params = {
        "url": "invalid-url",
        "token": "test-token",
        "org": "test-org",
        "bucket": "test-bucket",
    }

    with pytest.raises(ValueError, match="Invalid InfluxDB URL format"):
        InfluxDBStorage(invalid_params)


@pytest.mark.asyncio
async def test_persist_realtime_data(influxdb_params, mock_influxdb_client):
    """Test persisting realtime sensor data."""
    storage = InfluxDBStorage(influxdb_params)

    # Mock the write_points method
    storage.write_points = AsyncMock()

    await storage.persist_realtime_data(
        monitor_id="monitor1",
        hertz=60.0,
        total_current=10.5,
        total_watts=1200.0,
        epoch=1234567890,
        voltage=[120.0, 120.0],
        devices=[
            {"id": "device1", "name": "Device 1", "w": 100, "icon": "icon1"},
            {
                "id": "device2",
                "name": "Device 2",
                "w": 200,
                "icon": "icon2",
                "sd": {"w": 195, "i": 1.6, "v": 120},
            },
        ],
        channels=[600.0, 600.0],
    )

    storage.write_points.assert_called_once()
    points = storage.write_points.call_args[0][0]

    # Verify we have the expected number of points
    # 1 main + 2 channels + 1 o11y + 2 devices = 6 points
    assert len(points) >= 6


@pytest.mark.asyncio
async def test_persist_device_data_regular(influxdb_params, mock_influxdb_client):
    """Test persisting regular device data."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    device_data = {
        "device": {
            "id": "device1",
            "name": "Test Device",
            "icon": "lightbulb",
            "last_state": "on",
            "last_state_time": "2024-01-01T12:00:00Z",
        },
        "usage": {
            "avg_monthly_KWH": 50.5,
            "yearly_cost": 12000,
        },  # Will be divided by 100
    }

    await storage.persist_device_data("monitor1", device_data)

    storage.write_points.assert_called_once()
    points = storage.write_points.call_args[0][0]
    assert len(points) == 1


@pytest.mark.asyncio
async def test_persist_device_data_always_on(influxdb_params, mock_influxdb_client):
    """Test persisting always-on device data."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    device_data = {
        "device": {"id": "always_on", "name": "Always On", "icon": "always_on"},
        "usage": {
            "avg_monthly_KWH": 100,
            "comparison": {
                "comparison_text": "Higher than average",
                "cohort": {"id": "cohort1", "state": "CA"},
            },
        },
        "always_on": {
            "devices": [{"id": "ao_device1", "w": 10}, {"id": "ao_device2", "w": 20}]
        },
    }

    await storage.persist_device_data("monitor1", device_data)

    # Should write main point and comparison point
    assert storage.write_points.call_count >= 2

    # Check that devices were queued
    assert storage.device_queue.queue.qsize() == 2


@pytest.mark.asyncio
async def test_persist_timeline_data(influxdb_params, mock_influxdb_client):
    """Test persisting timeline event data."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    await storage.persist_timeline_data(
        monitor_id="monitor1",
        device_id="device1",
        device_name="Test Device",
        time=1234567890,
        event_type="on",
        icon="lightbulb",
        body="Device turned on",
        device_state="on",
        user_device_type="light",
        device_transition_from_state="off",
    )

    storage.write_points.assert_called_once()
    points = storage.write_points.call_args[0][0]
    assert len(points) == 1


@pytest.mark.asyncio
async def test_persist_hello_event(influxdb_params, mock_influxdb_client):
    """Test persisting hello event."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    await storage.persist_hello_event("monitor1", True, 1234567890)

    storage.write_points.assert_called_once()
    points = storage.write_points.call_args[0][0]
    assert len(points) == 1


@pytest.mark.asyncio
async def test_persist_monitor_status(influxdb_params, mock_influxdb_client):
    """Test persisting monitor status."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    monitor_status = {
        "monitor_info": {
            "online": True,
            "version": "2.0.0",
            "wifi_strength": -50,
            "ip_address": "192.168.1.100",
        },
        "signals": {"progress": 100, "status": "OK"},
        "device_detection": {
            "in_progress": [{"name": "Device 1", "icon": "icon1", "progress": 50}],
            "found": [{"name": "Device 2", "icon": "icon2", "progress": 100}],
        },
    }

    await storage.persist_monitor_status("monitor1", monitor_status)

    # Should be called twice: once for monitor info, once for device detection
    assert storage.write_points.call_count == 2


@pytest.mark.asyncio
async def test_process_device_queue_batch(influxdb_params, mock_influxdb_client):
    """Test processing device queue in batches."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_points = AsyncMock()

    # Add multiple items to queue
    for i in range(3):
        await storage.device_queue.add_device_data(
            f"device{i}", "monitor1", float(i * 10), 1234567890
        )

    # Set device names
    for i in range(3):
        await storage.device_queue.set_device_name(f"device{i}", f"Device {i}")

    # Process batch
    batch = []
    for _ in range(3):
        item = await storage.device_queue.get_next_item()
        if item:
            batch.append(item)

    await storage._process_device_batch(batch)

    storage.write_points.assert_called_once()
    points = storage.write_points.call_args[0][0]
    assert len(points) == 3


@pytest.mark.asyncio
async def test_shutdown(influxdb_params):
    """Test graceful shutdown: remaining items processed + async client closed."""
    storage = InfluxDBStorage(influxdb_params)
    client = AsyncMock()
    storage.client = client  # simulate a connected client

    # Add some items to queue
    await storage.device_queue.add_device_data("device1", "monitor1", 100.0, 1234567890)
    await storage.device_queue.set_device_name("device1", "Device 1")

    storage._process_device_batch = AsyncMock()

    await storage.close()

    assert storage.is_shutting_down is True
    storage._process_device_batch.assert_called_once()
    client.close.assert_awaited_once()


# --------------------------------------------------- connection + write error branches


@pytest.mark.asyncio
async def test_connect_exits_when_influxdb_is_unreachable(
    influxdb_params, mock_influxdb_client
):
    """An unreachable InfluxDB must fail fast at startup, not silently drop every write."""
    mock_influxdb_client.ping = AsyncMock(side_effect=OSError("connection refused"))
    storage = InfluxDBStorage(influxdb_params)
    with pytest.raises(SystemExit):
        await storage.connect()
    # the half-open client is released rather than leaked
    assert storage.client is None


@pytest.mark.asyncio
async def test_connect_exits_when_the_health_check_reports_unhealthy(
    influxdb_params, mock_influxdb_client
):
    mock_influxdb_client.ping = AsyncMock(return_value=False)
    storage = InfluxDBStorage(influxdb_params)
    with pytest.raises(SystemExit):
        await storage.connect()
    assert storage.client is None


@pytest.mark.asyncio
async def test_connect_starts_the_queue_processor_once(
    influxdb_params, mock_influxdb_client
):
    storage = InfluxDBStorage(influxdb_params)
    await storage.connect()
    first = storage.queue_processor_task
    await storage.connect()
    assert storage.queue_processor_task is first
    storage.is_shutting_down = True
    await storage.close()


def _influx_error(status):
    err = InfluxDBError(message="boom")
    err.response = MagicMock()
    err.response.status = status
    return err


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "401 Unauthorized"),
        (404, "404 Not Found"),
        (500, "InfluxDB write failed"),
    ],
    ids=["unauthorized", "missing-bucket", "other"],
)
def test_write_error_guidance_is_actionable(influxdb_params, caplog, status, expected):
    """A 401/404 is an operator misconfiguration — the log must say which, not just 'failed'."""
    storage = InfluxDBStorage(influxdb_params)
    storage._log_write_error(_influx_error(status))
    assert expected in caplog.text


def test_write_error_guidance_names_the_bucket_and_org(influxdb_params, caplog):
    storage = InfluxDBStorage(influxdb_params)
    storage._log_write_error(_influx_error(401))
    assert "test-bucket" in caplog.text
    assert "test-org" in caplog.text


@pytest.mark.asyncio
async def test_write_points_counts_failures(influxdb_params):
    """A failed batch must be counted, so the periodic summary reports real data loss."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = AsyncMock()
    storage.write_api.write = AsyncMock(side_effect=_influx_error(401))
    await storage.write_points([Point("m").field("v", 1), Point("m").field("v", 2)])
    assert storage.write_stats["failed"] == 2
    assert storage.write_stats["successful"] == 0


@pytest.mark.asyncio
async def test_write_points_without_a_write_api_is_a_noop(influxdb_params):
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = None
    await storage.write_points([Point("m").field("v", 1)])
    assert storage.write_stats["successful"] == 0


@pytest.mark.asyncio
async def test_write_points_survives_a_non_influx_error(influxdb_params):
    """Anything the client raises must be counted and logged, never propagated."""
    storage = InfluxDBStorage(influxdb_params)
    storage.write_api = AsyncMock()
    storage.write_api.write = AsyncMock(side_effect=OSError("socket reset"))
    await storage.write_points([Point("m").field("v", 1)])
    assert storage.write_stats["failed"] == 1


@pytest.mark.asyncio
async def test_close_client_is_idempotent(influxdb_params, mock_influxdb_client):
    storage = InfluxDBStorage(influxdb_params)
    await storage.connect()
    storage.is_shutting_down = True
    await storage._close_client()
    await storage._close_client()
    assert storage.client is None
    assert storage.write_api is None


@pytest.mark.asyncio
async def test_close_client_survives_a_failing_close(influxdb_params):
    """Teardown must continue even if releasing the session raises."""
    storage = InfluxDBStorage(influxdb_params)
    storage.client = MagicMock()
    storage.client.close = AsyncMock(side_effect=OSError("already gone"))
    await storage._close_client()
    assert storage.client is None
