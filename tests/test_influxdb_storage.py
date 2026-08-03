import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, UTC

# Add the src/app directory to Python path

from app.storage.influxdb import InfluxDBStorage, DeviceDataQueue
from influxdb_client import Point
from influxdb_client.client.exceptions import InfluxDBError


class TestDeviceDataQueue:
    """Test the device data queue functionality."""

    @pytest.mark.asyncio
    async def test_queue_operations(self):
        """Test basic queue operations."""
        queue = DeviceDataQueue(max_size=3)

        # Add items
        await queue.add_device_data("device1", "monitor1", 100.5, 1234567890)
        await queue.add_device_data("device2", "monitor1", 200.0, 1234567891)

        # Get items
        item1 = await queue.get_next_item()
        assert item1["device_id"] == "device1"
        assert item1["watts"] == 100.5

        item2 = await queue.get_next_item()
        assert item2["device_id"] == "device2"

        # Test timeout on empty queue
        item3 = await queue.get_next_item()
        assert item3 is None

    @pytest.mark.asyncio
    async def test_queue_full(self):
        """Test queue behavior when full."""
        queue = DeviceDataQueue(max_size=2)

        # Fill queue
        await queue.add_device_data("device1", "monitor1", 100.0, 1234567890)
        await queue.add_device_data("device2", "monitor1", 200.0, 1234567891)

        # Try to add to full queue (should log warning)
        with patch("app.storage.influxdb.storage_logger") as mock_logger:
            await queue.add_device_data("device3", "monitor1", 300.0, 1234567892)
            mock_logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_device_name_cache(self):
        """Test device name caching."""
        queue = DeviceDataQueue()

        # Test empty cache
        name = await queue.get_device_name("device1")
        assert name is None

        # Set and get name
        await queue.set_device_name("device1", "Living Room Light")
        name = await queue.get_device_name("device1")
        assert name == "Living Room Light"


class TestWriteApi:
    """Test the async write path (one awaited batch per cycle)."""

    @pytest.fixture
    def influxdb_params(self):
        return {"url": "http://localhost:8086", "token": "t", "org": "o", "bucket": "b"}

    @pytest.mark.asyncio
    async def test_write_points_writes_batch(self, influxdb_params):
        storage = InfluxDBStorage(influxdb_params)
        storage.write_api = AsyncMock()
        await storage.write_points([Point("test").field("value", 1)])
        storage.write_api.write.assert_awaited_once()
        assert storage.write_stats["successful"] == 1

    @pytest.mark.asyncio
    async def test_write_error_not_raised(self, influxdb_params):
        storage = InfluxDBStorage(influxdb_params)
        storage.write_api = AsyncMock()
        storage.write_api.write.side_effect = InfluxDBError(message="boom")
        await storage.write_points([Point("test").field("value", 1)])  # must not raise
        assert storage.write_stats["failed"] == 1


class TestInfluxDBStorage:
    """Test the main InfluxDB storage class."""

    @pytest.fixture
    def influxdb_params(self):
        """Standard InfluxDB parameters."""
        return {
            "url": "http://localhost:8086",
            "token": "test_token",
            "org": "test_org",
            "bucket": "test_bucket",
        }

    @pytest.mark.asyncio
    async def test_validate_params(self, influxdb_params):
        """Test parameter validation (__init__ does no I/O — the client opens in start())."""
        storage = InfluxDBStorage(influxdb_params)
        assert storage.bucket == influxdb_params["bucket"]

        # Missing required param — raised in _validate_params.
        invalid_params = influxdb_params.copy()
        del invalid_params["url"]
        with pytest.raises(ValueError, match="Missing required InfluxDB parameter: url"):
            InfluxDBStorage(invalid_params)

        # Invalid URL
        invalid_params = influxdb_params.copy()
        invalid_params["url"] = "not_a_url"
        with pytest.raises(ValueError, match="Invalid InfluxDB URL format"):
            InfluxDBStorage(invalid_params)

    @pytest.mark.asyncio
    async def test_persist_realtime_data(self, influxdb_params):
        """Test persisting realtime data."""
        storage = InfluxDBStorage(influxdb_params)
        storage.write_points = AsyncMock()

        epoch = int(datetime.now(UTC).timestamp())
        devices = [
            {"id": "device1", "name": "Test Device", "w": 100, "icon": "lightbulb"},
            {
                "id": "device2",
                "name": "Test Device 2",
                "w": 200,
                "icon": "plug",
                "sd": {"w": 195, "i": 1.6, "v": 119.5},
            },
        ]

        await storage.persist_realtime_data(
            "test_monitor", 60.0, 15.5, 1860.0, epoch, [120.1, 119.9], devices, [930.0, 930.0]
        )

        storage.write_points.assert_called_once()
        points = storage.write_points.call_args[0][0]
        # 1 main + 2 channels + 1 o11y + 2 devices = 6 points
        assert len(points) == 6

    @pytest.mark.asyncio
    async def test_shutdown(self, influxdb_params):
        """Test graceful shutdown: async client closed, no buffer to flush."""
        storage = InfluxDBStorage(influxdb_params)
        client = AsyncMock()
        storage.client = client

        await storage.device_queue.add_device_data("device1", "monitor1", 100.0, 1234567890)

        await storage.close()

        assert storage.is_shutting_down is True
        client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_device_queue_processing(self, influxdb_params):
        """Test device queue batch processing."""
        storage = InfluxDBStorage(influxdb_params)
        storage.write_points = AsyncMock()

        # Set device names in cache
        await storage.device_queue.set_device_name("device1", "Device One")
        await storage.device_queue.set_device_name("device2", "Device Two")

        # Add items to queue
        await storage.device_queue.add_device_data("device1", "monitor1", 100.0, 1234567890)
        await storage.device_queue.add_device_data("device2", "monitor1", 200.0, 1234567891)

        # Process batch manually
        batch = []
        for _ in range(2):
            item = await storage.device_queue.get_next_item()
            if item:
                batch.append(item)

        await storage._process_device_batch(batch)

        # Verify points were written
        storage.write_points.assert_called_once()
        points = storage.write_points.call_args[0][0]
        assert len(points) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
