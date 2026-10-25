import asyncio
import contextlib
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import pytz
from dateutil import parser
from influxdb_client import Point
from influxdb_client.client.exceptions import InfluxDBError
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from influxdb_client.client.write_api_async import WriteApiAsync

from app.core import config
from app.utils.logging import storage_logger

# Expected failures when turning an untrusted payload into points: a missing or renamed key,
# a wrong-shaped value, a short list. Excludes AttributeError and friends on purpose — a
# broken collector must surface, not disappear into a log line.
MALFORMED_PAYLOAD = (KeyError, IndexError, TypeError, ValueError)

# The above plus what an actual InfluxDB write can fail with.
WRITE_ERRORS = (*MALFORMED_PAYLOAD, OSError, InfluxDBError)

_DROPPED = re.compile(r"dropped=(\d+)")


def _tag(value: str) -> str:
    """A tag value line protocol can carry exactly: it cannot end in a backslash (the
    client then appends a space, and InfluxDB stores a name no device has)."""
    return value.rstrip("\\") or value


def _event_precision(time: Any) -> str:
    """Sense stamps timeline events with milliseconds (ISO strings); at second precision
    an on and an off in the same second would share a timestamp and overwrite."""
    return "ms" if isinstance(time, str) else "s"


def _num(value: Any) -> Any:
    """A realtime reading as float, so its InfluxDB field type never depends on spelling.

    InfluxDB fixes a field's type per shard and refuses a later write of the other type, so
    a JSON `60` after `60.0` (or a defaulted `0`) would drop the whole line. Realtime
    readings are floats in every captured payload, so float is the type buckets already
    hold. REST usage fields are not passed through here: Sense sends some of them (cents)
    as whole numbers, which buckets already hold as integers. Booleans, non-numbers and an
    int too large for a float pass through unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    try:
        return float(value)
    except OverflowError:
        return value


class DeviceDataQueue:
    """Manages device data queue with size limits and proper async handling."""

    def __init__(self, max_size: int = 1000):
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_size)
        self.device_name_cache: dict[str, str] = {}
        self.cache_lock = asyncio.Lock()

    async def add_device_data(
        self, device_id: str, monitor_id: str, watts: float, timestamp: int
    ) -> None:
        """Add device data to queue for processing. Drops (does not block) when full."""
        try:
            # put_nowait (not await put) so a full queue drops the newest item instead of
            # blocking the caller forever — await put() never raises QueueFull.
            self.queue.put_nowait(
                {
                    "device_id": device_id,
                    "monitor_id": monitor_id,
                    "watts": watts,
                    "timestamp": timestamp,
                }
            )
        except asyncio.QueueFull:
            storage_logger.warning(
                "Device data queue full, dropping data for device %s", device_id
            )

    async def get_device_name(self, device_id: str) -> str | None:
        """Get cached device name."""
        async with self.cache_lock:
            return self.device_name_cache.get(device_id)

    async def set_device_name(self, device_id: str, name: str) -> None:
        """Cache device name (bounded, FIFO eviction)."""
        async with self.cache_lock:
            # device_id originates from remote payloads — cap the cache so it can't grow
            # without limit. Names are re-populated hourly by fetch_devices, so evicting the
            # oldest is harmless in practice.
            if (
                device_id not in self.device_name_cache
                and len(self.device_name_cache) >= config.DEVICE_CACHE_MAX_SIZE
            ):
                del self.device_name_cache[next(iter(self.device_name_cache))]
            self.device_name_cache[device_id] = name

    async def get_next_item(self) -> dict[str, Any] | None:
        """Get next item from queue with timeout."""
        try:
            return await asyncio.wait_for(
                self.queue.get(), timeout=config.QUEUE_TIMEOUT
            )
        except TimeoutError:
            return None


class InfluxDBStorage:
    """Handles all InfluxDB storage operations with proper error handling and cleanup."""

    def __init__(self, influxdb_params: dict[str, str]):
        # Validate connection parameters
        self._validate_params(influxdb_params)

        self.url = influxdb_params["url"]
        self.token = influxdb_params["token"]
        self.bucket = influxdb_params["bucket"]
        self.org = influxdb_params["org"]

        # Fleet ingestion standard: the asyncio-native client (aiohttp). It must be created
        # inside a running loop, so __init__ only validates config — connect() opens the
        # connection. Every cycle's write is awaited inline, so there is nothing to flush on
        # shutdown.
        self.client: InfluxDBClientAsync | None = None
        self.write_api: WriteApiAsync | None = None

        # Device data queue with proper async handling
        self.device_queue = DeviceDataQueue(max_size=config.QUEUE_SIZE_DEVICE)
        self.is_shutting_down = False
        self.queue_processor_task: asyncio.Task[None] | None = None

        # Queue for timeline events awaiting device names
        self.timeline_event_queue: dict[str, list[dict[str, Any]]] = {}
        self.timeline_queue_lock = asyncio.Lock()

        # Track write statistics
        self.write_stats = {"successful": 0, "failed": 0, "pending": 0}

    async def connect(self) -> None:
        """Open the asyncio-native InfluxDB connection and start the queue processor.

        The async client must be created inside a running loop. ping() (unauthenticated)
        fails fast on an unreachable/unhealthy server; auth/bucket problems instead surface
        on the first write (see write_points) and are retried on the next cycle.
        """
        self.client = InfluxDBClientAsync(
            url=self.url,
            token=self.token,
            org=self.org,
            timeout=config.INFLUXDB_TIMEOUT,
            enable_gzip=config.INFLUXDB_ENABLE_GZIP,
        )
        try:
            healthy = await self.client.ping()
        except Exception as e:
            await self._close_client()
            storage_logger.error(
                "Could not reach InfluxDB at %s: %s: %s", self.url, type(e).__name__, e
            )
            raise SystemExit(1) from None
        if not healthy:
            await self._close_client()
            storage_logger.error("InfluxDB health check failed at %s", self.url)
            raise SystemExit(1)

        self.write_api = self.client.write_api()
        storage_logger.info("Successfully connected to InfluxDB")
        if self.queue_processor_task is None:
            self.queue_processor_task = asyncio.create_task(self.process_device_queue())

    async def _close_client(self) -> None:
        """Close the async client if open (releases the aiohttp session), ignoring errors."""
        if self.client is not None:
            # swallowed-exceptions: best-effort release of the aiohttp session on shutdown;
            # a failure here must not stop the rest of the teardown from running.
            with contextlib.suppress(OSError, InfluxDBError):
                await self.client.close()
            self.client = None
            self.write_api = None

    def _validate_params(self, params: dict[str, str]) -> None:
        """Validate InfluxDB connection parameters."""
        required = ["url", "token", "org", "bucket"]
        for key in required:
            if not params.get(key):
                raise ValueError(f"Missing required InfluxDB parameter: {key}")

        # Validate URL format
        url = params["url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid InfluxDB URL format: {url}")

    async def write_points(self, points: list[Point]) -> None:
        """Write the cycle's points as ONE batched async write, awaited inline.

        Each Point already carries its own timestamp precision (set in the persist methods),
        so no write_precision is passed here. Auth/bucket errors are logged with actionable
        guidance but never crash the collector — the next cycle simply retries.
        """
        if not points or self.write_api is None:
            return

        # A value line protocol cannot carry (a dict or list where Sense should send a
        # number) raises while the client serialises the batch, which would lose every point
        # in it. Such a point is set aside alone.
        writable: list[Point] = []
        for point in points:
            try:
                point.to_line_protocol()
            except (TypeError, ValueError) as e:
                self.write_stats["failed"] += 1
                storage_logger.error("Dropping one unwritable point: %s", e)
            else:
                writable.append(point)
        points = writable
        if not points:
            return

        try:
            # Guard the eager line-protocol serialization — only pay it when DEBUG is on.
            if storage_logger.isEnabledFor(logging.DEBUG):
                for p in points:
                    storage_logger.debug("Writing point: %s", p.to_line_protocol())
            await self.write_api.write(bucket=self.bucket, org=self.org, record=points)
            self.write_stats["successful"] += len(points)
        except InfluxDBError as e:
            dropped = self._dropped_count(e, len(points))
            self.write_stats["failed"] += dropped
            self.write_stats["successful"] += len(points) - dropped
            self._log_write_error(e)
        except Exception as e:
            self.write_stats["failed"] += len(points)
            storage_logger.exception("Error writing to InfluxDB: %s", e)

    @staticmethod
    def _dropped_count(error: InfluxDBError, sent: int) -> int:
        """How many points a failed write lost: a 422 partial write names its `dropped=N`."""
        status = getattr(getattr(error, "response", None), "status", None)
        match = _DROPPED.search(str(getattr(error, "message", "") or error))
        if status == 422 and match:
            return min(int(match.group(1)), sent)
        return sent

    def _log_write_error(self, error: InfluxDBError) -> None:
        """Log an InfluxDB write failure with actionable guidance (auth/bucket)."""
        status = getattr(getattr(error, "response", None), "status", None)
        if status == 401:
            storage_logger.error(
                "InfluxDB rejected the write (401 Unauthorized) — check the token has write "
                "access to bucket '%s' in org '%s'.",
                self.bucket,
                self.org,
            )
        elif status == 404:
            storage_logger.error(
                "InfluxDB write failed (404 Not Found) — bucket '%s' or org '%s' does not exist.",
                self.bucket,
                self.org,
            )
        else:
            storage_logger.error("InfluxDB write failed: %s", error)

    async def process_device_queue(self) -> None:
        """Process device data queue efficiently."""
        storage_logger.info("Starting device queue processor")

        while not self.is_shutting_down:
            try:
                # Process items in batches
                batch: list[dict[str, Any]] = []
                batch_timeout = (
                    config.QUEUE_BATCH_TIMEOUT
                )  # Collect items for configured timeout
                batch_start = time.time()

                while (
                    len(batch) < config.QUEUE_BATCH_SIZE
                    and (time.time() - batch_start) < batch_timeout
                    and not self.is_shutting_down
                ):
                    item = await self.device_queue.get_next_item()
                    if item:
                        batch.append(item)

                if batch:
                    await self._process_device_batch(batch)

            except Exception as e:
                storage_logger.error("Error in device queue processor: %s", e)
                await asyncio.sleep(config.API_WORKER_ERROR_SLEEP)

        storage_logger.info("Device queue processor stopped")

    async def _process_device_batch(self, batch: list[dict[str, Any]]) -> None:
        """Process a batch of device data."""
        points = []
        unnamed: set[str] = set()

        for item in batch:
            device_id = item["device_id"]
            device_name = await self.device_queue.get_device_name(device_id)

            if device_name:
                point = (
                    Point(config.MEASUREMENT_ALWAYS_ON_DEVICES)
                    .tag("monitor_id", item["monitor_id"])
                    .tag("parent_device_id", "always_on")
                    .tag("device_id", device_id)
                    .tag("device_name", _tag(device_name))
                    .field("watts", _num(item["watts"]))
                    .time(item["timestamp"], write_precision="s")
                )
                points.append(point)
            else:
                # Dropped, not re-queued: the next always_on persist sends the reading again,
                # and a re-queued item comes straight back out of a non-empty asyncio.Queue
                # without yielding, so a full batch of unnamed ids spins the event loop.
                unnamed.add(device_id)

        if unnamed:
            storage_logger.warning(
                "Dropped readings for %s always-on device(s) with no cached name: %s",
                len(unnamed),
                ", ".join(sorted(unnamed)[:10]),
            )
        if points:
            await self.write_points(points)
        # Yield once per batch so the queue processor can never monopolise the loop.
        await asyncio.sleep(0)

    async def persist_realtime_data(
        self,
        monitor_id: str,
        hertz: float,
        total_current: float,
        total_watts: float,
        epoch: int,
        voltage: list[float],
        devices: list[Any],  # straight from the Sense payload: entries are not trusted
        channels: list[float],
    ) -> None:
        """Persist real-time sensor data."""
        storage_logger.debug(
            "Persisting realtime data: %.1fW, %s devices, %.1fHz",
            total_watts,
            len(devices),
            hertz,
        )

        current_time = datetime.now(UTC)
        epoch_time = datetime.fromtimestamp(epoch, UTC)
        time_difference = (current_time - epoch_time).total_seconds()

        storage_logger.debug("Time difference: %s", time_difference)

        try:
            points = []

            # Main power data
            main_point = (
                Point(config.MEASUREMENT_MAINS)
                .tag("monitor_id", monitor_id)
                .field("hertz", _num(hertz))
                .field("current", float(total_current))
                .field("watts", _num(total_watts))
                .time(epoch, write_precision="s")
            )
            points.append(main_point)

            # Channel data
            if len(channels) >= 2:
                for i, (ch_watts, ch_voltage) in enumerate(
                    zip(
                        channels[: config.MAX_CHANNELS],
                        voltage[: config.MAX_CHANNELS]
                        if voltage
                        else [config.DEFAULT_VOLTAGE, config.DEFAULT_VOLTAGE],
                        strict=False,
                    )
                ):
                    leg = f"L{i + 1}"
                    points.append(
                        Point(config.MEASUREMENT_MAINS)
                        .tag("monitor_id", monitor_id)
                        .tag("leg", leg)
                        .field("watts", _num(ch_watts))
                        .field("voltage", _num(ch_voltage))
                        .time(epoch, write_precision="s")
                    )

            # Time sync monitoring
            points.append(
                Point(config.MEASUREMENT_O11Y)
                .tag("monitor_id", monitor_id)
                .field("time_difference", time_difference)
                .time(epoch, write_precision="s")
            )

            # Device data
            for device in devices:
                # A null, a string or a list where Sense should send a device object is
                # skipped alone; the frame's other points still go.
                if not isinstance(device, dict):
                    continue
                device_id = device.get("id")
                if not device_id:
                    continue

                device_watts = _num(device.get("w", 0.0))
                # Prefer the friendly name from the device-list cache (fetch_devices ->
                # set_device_name). The realtime payload's `name` is the device ID for
                # some accounts, which would otherwise tag sense_devices with raw IDs.
                cached_name = await self.device_queue.get_device_name(device_id)
                device_name = cached_name or device.get("name") or "Unknown"
                device_icon = device.get("icon", "")
                device_sd = device.get("sd")
                if not isinstance(device_sd, dict):
                    device_sd = {}
                is_plug = any(
                    device_sd.get(key) is not None for key in ["w", "i", "v", "e"]
                )

                device_point = (
                    Point(config.MEASUREMENT_DEVICES)
                    .tag("monitor_id", monitor_id)
                    .tag("device_id", device_id)
                    .tag("device_name", _tag(device_name))
                    .tag("is_plug", str(is_plug).lower())
                    .field("icon", device_icon)
                    .field("watts", device_watts)
                )

                # Add plug-specific fields if available
                if device_sd.get("w") is not None:
                    device_point.field("sd_watts", _num(device_sd["w"]))
                if device_sd.get("i") is not None:
                    device_point.field("sd_current", float(device_sd["i"]))
                if device_sd.get("v") is not None:
                    device_point.field("sd_voltage", _num(device_sd["v"]))
                if device_sd.get("e") is not None:
                    device_point.field("sd_energy", _num(device_sd["e"]))
                if device.get("ao_w") is not None:
                    device_point.field("always_on_watts", _num(device["ao_w"]))
                if device.get("ao_st") is not None:
                    device_point.field("always_on_state", device["ao_st"])

                device_point.time(epoch, write_precision="s")
                points.append(device_point)

            await self.write_points(points)

        except WRITE_ERRORS as e:
            storage_logger.error("Error preparing realtime data points: %s", e)

    async def persist_device_data(
        self, monitor_id: str, device_data: dict[str, Any]
    ) -> None:
        """Persist device details and usage data."""
        try:
            device_info = device_data.get("device", {})
            device_id = device_info.get("id")
            device_name = device_info.get("name", "Unknown")
            icon = device_info.get("icon", "")

            if not device_id:
                storage_logger.warning("Device data missing device ID")
                return

            storage_logger.debug(
                "Persisting device data for %s (%s)", device_name, device_id
            )

            # Cache the device name
            await self.device_queue.set_device_name(device_id, device_name)

            # Process any queued timeline events for this device
            await self.process_queued_timeline_events(device_id, device_name, icon)

            timestamp = int(datetime.now(UTC).timestamp())

            if device_id == "always_on":
                await self.process_always_on_device(
                    device_id, device_name, device_data, timestamp, monitor_id, icon
                )
            else:
                await self.process_regular_device(
                    device_id, device_name, device_data, timestamp, monitor_id, icon
                )

        except MALFORMED_PAYLOAD as e:
            storage_logger.error("Error in persist_device_data: %s", e)

    async def process_regular_device(
        self,
        device_id: str,
        device_name: str,
        device_data: dict[str, Any],
        timestamp: int,
        monitor_id: str,
        icon: str,
    ) -> None:
        """Process regular device data."""
        storage_logger.debug(
            "Processing regular device: %s - %s", device_id, device_name
        )

        device_detail_point = (
            Point(config.MEASUREMENT_DEVICES)
            .tag("device_id", device_id)
            .tag("device_name", _tag(device_name))
            .tag("monitor_id", monitor_id)
            .field("icon", icon)
            .time(timestamp, write_precision="s")
        )

        device_info = device_data.get("device", {})

        # Add device state information
        if device_info.get("last_state") is not None:
            device_detail_point.field("last_state", device_info["last_state"])

        if "last_state_time" in device_info:
            try:
                last_state_timestamp = parser.parse(
                    device_info["last_state_time"]
                ).astimezone(pytz.UTC)
                last_state_timestamp_seconds = int(last_state_timestamp.timestamp())
                device_detail_point.field(
                    "last_state_time", last_state_timestamp_seconds
                )
            except (ValueError, TypeError, OverflowError) as e:
                storage_logger.error("Error parsing last_state_time: %s", e)

        # Add usage data
        usage = device_data.get("usage", {})
        for field, value in usage.items():
            if value is not None:
                if field == "yearly_cost":
                    device_detail_point.field(
                        field, float(value) / config.YEARLY_COST_DIVISOR
                    )
                else:
                    device_detail_point.field(field, value)

        # Add additional info
        if device_data.get("info") is not None:
            device_detail_point.field("info", str(device_data["info"]))

        await self.write_points([device_detail_point])

    async def process_always_on_device(
        self,
        device_id: str,
        device_name: str,
        device_data: dict[str, Any],
        timestamp: int,
        monitor_id: str,
        icon: str,
    ) -> None:
        """Process always-on device data."""
        storage_logger.debug(
            "Processing always_on device: %s - %s", device_id, device_name
        )

        usage = device_data.get("usage", {})
        always_on = device_data.get("always_on", {})
        comparison = usage.get("comparison", {})

        # Main always-on metrics
        device_detail_point = (
            Point(config.MEASUREMENT_ALWAYS_ON)
            .tag("device_id", device_id)
            .tag("device_name", _tag(device_name))
            .tag("monitor_id", monitor_id)
            .field("icon", icon)
        )

        # Add usage fields
        usage_fields = [
            "avg_monthly_KWH",
            "avg_monthly_pct",
            "avg_watts",
            "yearly_KWH",
            "yearly_cost",
            "avg_monthly_cost",
            "current_ao_wattage",
        ]
        for field in usage_fields:
            value = usage.get(field)
            if value is not None:
                device_detail_point.field(field, value)

        device_detail_point.time(timestamp, write_precision="s")
        await self.write_points([device_detail_point])

        # Comparison data
        if comparison:
            comparison_point = (
                Point(config.MEASUREMENT_ALWAYS_ON_COMPARISON)
                .tag("device_id", device_id)
                .tag("monitor_id", monitor_id)
            )

            comparison_fields = [
                "comparison_text",
                "comparison_summary_text",
                "title",
                "count",
                "display_count",
                "cohort_marker",
                "cohort_avg_w",
            ]
            for field in comparison_fields:
                value = comparison.get(field)
                if value is not None:
                    comparison_point.field(field, value)

            # Cohort data
            cohort = comparison.get("cohort", {})
            if cohort:
                cohort_fields = {
                    "id": "cohort_id",
                    "area_code": "cohort_area_code",
                    "state": "cohort_state",
                    "home_size": "cohort_home_size",
                }
                for src, dst in cohort_fields.items():
                    value = cohort.get(src)
                    if value is not None:
                        comparison_point.field(dst, value)

            comparison_point.time(timestamp, write_precision="s")
            await self.write_points([comparison_point])

        # Individual always-on devices
        for device in always_on.get("devices", []):
            device_id = device.get("id")
            device_watts = _num(device.get("w", 0.0))

            if device_id:
                await self.device_queue.add_device_data(
                    device_id, monitor_id, device_watts, timestamp
                )

    async def persist_timeline_data(
        self,
        monitor_id: str,
        device_id: str,
        device_name: str,
        time: int,
        event_type: str,
        icon: str,
        body: str,
        device_state: str,
        user_device_type: str,
        device_transition_from_state: str,
    ) -> None:
        """Persist timeline event data."""
        # If device_name is just the device_id, it means we don't have the real name yet
        # Queue it for later processing when device details are available
        if device_name == device_id:
            await self._queue_timeline_event(
                monitor_id,
                device_id,
                time,
                event_type,
                icon,
                body,
                device_state,
                user_device_type,
                device_transition_from_state,
            )
            return

        timeline_point = (
            Point(config.MEASUREMENT_EVENT)
            .tag("monitor_id", monitor_id)
            .tag("device_id", device_id)
            .tag("device_name", _tag(device_name))
            .field("time", time)
            .field("type", event_type)
            .field("icon", icon)
            .field("body", body)
            .field("device_state", device_state)
            .field("user_device_type", user_device_type)
            .field("device_transition_from_state", device_transition_from_state)
            .time(time, write_precision=_event_precision(time))
        )
        await self.write_points([timeline_point])

    async def _queue_timeline_event(
        self,
        monitor_id: str,
        device_id: str,
        time: int,
        event_type: str,
        icon: str,
        body: str,
        device_state: str,
        user_device_type: str,
        device_transition_from_state: str,
    ) -> None:
        """Queue timeline event for later processing when device name is available."""
        async with self.timeline_queue_lock:
            # Bounded so a feed of never-resolving device_ids can't grow the heap without
            # limit: cap distinct devices (evict oldest, dropping its unresolved events) and
            # cap events per device (drop oldest).
            if (
                device_id not in self.timeline_event_queue
                and len(self.timeline_event_queue) >= config.DEVICE_CACHE_MAX_SIZE
            ):
                oldest = next(iter(self.timeline_event_queue))
                dropped = len(self.timeline_event_queue.pop(oldest))
                storage_logger.warning(
                    "Timeline queue at capacity (%s devices); dropped %s unresolved event(s) "
                    "for device %s",
                    config.DEVICE_CACHE_MAX_SIZE,
                    dropped,
                    oldest,
                )

            events = self.timeline_event_queue.setdefault(device_id, [])
            if len(events) >= config.QUEUE_BATCH_SIZE:
                events.pop(0)

            events.append(
                {
                    "monitor_id": monitor_id,
                    "device_id": device_id,
                    "time": time,
                    "event_type": event_type,
                    "icon": icon,
                    "body": body,
                    "device_state": device_state,
                    "user_device_type": user_device_type,
                    "device_transition_from_state": device_transition_from_state,
                }
            )
            storage_logger.debug(
                "Queued timeline event for device %s - waiting for device name",
                device_id,
            )

    async def process_queued_timeline_events(
        self, device_id: str, device_name: str, device_icon: str
    ) -> None:
        """Process queued timeline events when device details become available."""
        async with self.timeline_queue_lock:
            if device_id in self.timeline_event_queue:
                events = self.timeline_event_queue[device_id]
                storage_logger.debug(
                    "Processing %s queued timeline events for %s (%s)",
                    len(events),
                    device_name,
                    device_id,
                )

                points = []
                for event in events:
                    # Update the body to use the real device name
                    updated_body = event["body"].replace(device_id, device_name)

                    timeline_point = (
                        Point(config.MEASUREMENT_EVENT)
                        .tag("monitor_id", event["monitor_id"])
                        .tag("device_id", device_id)
                        .tag("device_name", _tag(device_name))
                        .field("time", event["time"])
                        .field("type", event["event_type"])
                        .field("icon", device_icon)
                        .field("body", updated_body)
                        .field("device_state", event["device_state"])
                        .field("user_device_type", event["user_device_type"])
                        .field(
                            "device_transition_from_state",
                            event["device_transition_from_state"],
                        )
                        .time(
                            event["time"],
                            write_precision=_event_precision(event["time"]),
                        )
                    )
                    points.append(timeline_point)

                if points:
                    await self.write_points(points)

                # Remove processed events
                del self.timeline_event_queue[device_id]

    async def persist_hello_event(
        self, monitor_id: str, online_status: bool, timestamp: int
    ) -> None:
        """Persist hello/connection event."""
        hello_point = (
            Point(config.MEASUREMENT_HELLO)
            .tag("monitor_id", monitor_id)
            .field("online", online_status)
            .time(timestamp, write_precision="s")
        )
        await self.write_points([hello_point])

    async def persist_data_change_event(
        self,
        monitor_id: str,
        device_id: str,
        user_version: Any,
        guid: str,
        epoch_timestamp: int | None,
        influxdb_timestamp: int,
    ) -> None:
        """Persist data change event."""
        data_change_point = (
            Point(config.MEASUREMENT_DATA_CHANGE)
            .tag("monitor_id", monitor_id)
            .tag("device_id", device_id)
            .field("user_version", user_version)
            .field("guid", guid)
        )

        if epoch_timestamp:
            data_change_point.field("json_timestamp", epoch_timestamp)

        data_change_point.time(influxdb_timestamp, write_precision="s")
        await self.write_points([data_change_point])

    async def persist_device_state(
        self,
        monitor_id: str,
        device_id: str,
        mode: str,
        device_state: str,
        timestamp: int,
    ) -> None:
        """Persist device state event."""
        device_state_point = (
            Point(config.MEASUREMENT_DEVICE_STATE)
            .tag("monitor_id", monitor_id)
            .tag("device_id", device_id)
            .field("mode", mode)
            .field("state", device_state)
            .time(timestamp, write_precision="s")
        )
        await self.write_points([device_state_point])

    async def persist_monitor_status(
        self, monitor_id: str, monitor_status: dict[str, Any]
    ) -> None:
        """Persist monitor status data."""
        storage_logger.debug("Persisting monitor status for monitor_id: %s", monitor_id)

        try:
            timestamp = int(datetime.now(UTC).timestamp())
            signals = monitor_status.get("signals", {})
            monitor_info = monitor_status.get("monitor_info", {})
            wifi_strength = float(
                monitor_info.get("wifi_strength", config.DEFAULT_WIFI_STRENGTH)
            )

            # Monitor info point
            monitor_info_point = Point(config.MEASUREMENT_MONITOR_STATUS).tag(
                "monitor_id", monitor_id
            )

            # Add monitor info fields
            info_fields = [
                "ethernet",
                "online",
                "ip_address",
                "version",
                "ssid",
                "ndt_enabled",
                "mac",
            ]
            for field in info_fields:
                value = monitor_info.get(field)
                if value is not None:
                    monitor_info_point.field(field, value)

            # Add signal fields
            if signals.get("progress") is not None:
                monitor_info_point.field("progress", float(signals["progress"]))
            if signals.get("status") is not None:
                monitor_info_point.field("status", signals["status"])

            # Add WiFi strength if available
            if wifi_strength != 0:
                monitor_info_point.field("wifi_strength", wifi_strength)

            monitor_info_point.time(timestamp, write_precision="s")
            await self.write_points([monitor_info_point])

            # Device detection status
            device_detection = monitor_status.get("device_detection", {})
            points = []

            for status in ["in_progress", "found"]:
                for device in device_detection.get(status, []):
                    point = (
                        Point(config.MEASUREMENT_DEVICE_DETECTION)
                        .tag("monitor_id", monitor_id)
                        .tag("status", status)
                        .tag("name", _tag(device.get("name", "Unknown")))
                        .field("icon", device.get("icon", ""))
                        .field("progress", float(device.get("progress", 0)))
                        .time(timestamp, write_precision="s")
                    )
                    points.append(point)

            if points:
                await self.write_points(points)

        except WRITE_ERRORS as e:
            storage_logger.error("Error in persist_monitor_status: %s", e)

    async def close(self) -> None:
        """Gracefully shut down storage, ensuring all data is written."""
        storage_logger.info("Starting InfluxDB storage shutdown...")
        self.is_shutting_down = True

        # Let the queue processor finish the batch it is collecting (it sees
        # is_shutting_down within one queue timeout); cancel it only if it does not.
        if self.queue_processor_task and not self.queue_processor_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self.queue_processor_task),
                    timeout=config.QUEUE_TIMEOUT + config.QUEUE_BATCH_TIMEOUT + 1,
                )
            except TimeoutError:
                self.queue_processor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.queue_processor_task

        # Process any remaining items in queue
        remaining_items = []
        while True:
            item = await self.device_queue.get_next_item()
            if not item:
                break
            remaining_items.append(item)

        if remaining_items:
            storage_logger.info(
                "Processing %s remaining queue items...", len(remaining_items)
            )
            await self._process_device_batch(remaining_items)

        # Timeline events still waiting for a device name: write them now, under the cached
        # name where there is one and the device id where not, rather than lose them.
        for device_id in list(self.timeline_event_queue):
            events = self.timeline_event_queue[device_id]
            name = await self.device_queue.get_device_name(device_id) or device_id
            icon = events[0].get("icon", "") if events else ""
            await self.process_queued_timeline_events(device_id, name, icon)

        # Every cycle's write is awaited inline, so shutdown just releases the aiohttp session.
        await self._close_client()
        storage_logger.info("InfluxDB client closed")

        # Log final statistics
        storage_logger.info(
            "InfluxDB shutdown complete. Write stats - Successful: %s, Failed: %s",
            self.write_stats["successful"],
            self.write_stats["failed"],
        )
