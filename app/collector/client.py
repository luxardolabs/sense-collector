import asyncio
import contextlib
import json
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiofiles
import httpx

from app.collector.endpoints import SenseAPIEndpoints
from app.collector.websocket import handle_websocket_connection
from app.core import config
from app.storage.influxdb import InfluxDBStorage
from app.utils.file_validator import FilePathValidator
from app.utils.logging import api_logger
from app.utils.time import convert_to_epoch


async def authenticate_with_sense(
    client: httpx.AsyncClient, username: str, password: str
) -> dict[str, Any]:
    """Authenticate with Sense API and return authentication response.

    Uses the collector's long-lived httpx client with a per-request auth timeout.
    """
    url = SenseAPIEndpoints.AUTHENTICATE
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {"email": username, "password": password}

    try:
        response = await client.post(
            url, headers=headers, data=data, timeout=config.HTTP_AUTH_TIMEOUT
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload
    except httpx.HTTPError as e:
        api_logger.error("Authentication failed: %s", e)
        raise


class DeviceCache:
    """Thread-safe device cache with TTL and size limits."""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 120):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.cache: dict[str, tuple[Any, float]] = {}
        self.access_order: deque[str] = deque(maxlen=max_size)
        self._lock = asyncio.Lock()

    async def get(self, device_id: str) -> Any | None:
        """Get device data from cache if not expired."""
        async with self._lock:
            if device_id in self.cache:
                data, timestamp = self.cache[device_id]
                if time.time() - timestamp < self.ttl_seconds:
                    # Move to end for LRU
                    self.access_order.remove(device_id)
                    self.access_order.append(device_id)
                    return data
                else:
                    # Expired
                    del self.cache[device_id]
                    self.access_order.remove(device_id)
            return None

    async def put(self, device_id: str, data: Any) -> None:
        """Put device data in cache with timestamp."""
        async with self._lock:
            # Evict oldest if at capacity
            if len(self.cache) >= self.max_size and device_id not in self.cache:
                oldest = self.access_order.popleft()
                del self.cache[oldest]

            self.cache[device_id] = (data, time.time())
            if device_id in self.access_order:
                self.access_order.remove(device_id)
            self.access_order.append(device_id)

    async def clear(self) -> None:
        """Clear all cached data."""
        async with self._lock:
            self.cache.clear()
            self.access_order.clear()


class SenseCollector:
    """Main collector class that handles API interactions and data collection."""

    def __init__(
        self, username: str, password: str, influxdb_storage: InfluxDBStorage
    ) -> None:
        self.username = username
        self.password = password
        self.influxdb_storage = influxdb_storage

        # HTTP client with connection pooling
        self.client: httpx.AsyncClient | None = None
        self.headers: dict[str, str] = {}

        # Authentication state
        self.access_token: str | None = None
        self.user_id: str | None = None
        # Empty until authenticate() sets it; always populated before any persist/WS call
        # (the WS connection is opened post-auth), so downstream code takes it as a plain str.
        self.monitor_id: str = ""
        self.auth_time: datetime | None = None

        # Device cache
        self.device_cache = DeviceCache(
            max_size=config.DEVICE_CACHE_MAX_SIZE,
            ttl_seconds=config.DEVICE_CACHE_EXPIRY_SECONDS,
        )

        # API call queue for rate limiting
        self.api_call_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            maxsize=config.QUEUE_SIZE_API
        )
        self.api_workers: list[asyncio.Task[None]] = []

        # Timing control
        self.last_api_call_time: float = 0
        self.last_monitor_status_time: float = 0
        self.last_device_list_time: float = 0

        # Activity tracking for summary logging
        self.activity_stats = {
            "realtime_updates": 0,
            "timeline_events": 0,
            "device_state_changes": 0,
            "data_changes": 0,
            "last_summary_time": time.time(),
        }

        # Shutdown signalling. is_shutting_down is the internal flag; shutdown_event (set by
        # main via a signal handler) lets the periodic loops wake immediately instead of
        # sleeping out their interval.
        self.is_shutting_down = False
        self.shutdown_event: asyncio.Event | None = None

        # Liveness heartbeat (throttled file touch consumed by app.health.check)
        self._last_heartbeat: float = 0.0

        # Rate limiting
        self.api_semaphore = asyncio.Semaphore(config.API_RATE_LIMIT_CONCURRENT)
        self.min_api_interval = config.API_MIN_INTERVAL

    async def connect(self) -> None:
        """Open the ONE long-lived httpx client for the process lifetime (idempotent)."""
        if self.client is not None:
            # Already open — don't leak a second connection pool.
            return
        timeout = httpx.Timeout(
            timeout=config.HTTP_TIMEOUT_TOTAL,
            connect=config.HTTP_TIMEOUT_CONNECT,
            read=config.HTTP_TIMEOUT_READ,
        )
        limits = httpx.Limits(
            max_connections=config.CONN_POOL_LIMIT,
            max_keepalive_connections=config.CONN_POOL_LIMIT_PER_HOST,
        )
        self.client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            http2=True,  # Enable HTTP/2 for better performance
        )

    async def close(self) -> None:
        """Close the long-lived httpx client on shutdown."""
        if self.client:
            await self.client.aclose()
            self.client = None

    def _shutdown_requested(self) -> bool:
        """True once a shutdown has been signalled (internal flag or the main-loop event)."""
        return self.is_shutting_down or (
            self.shutdown_event is not None and self.shutdown_event.is_set()
        )

    async def _interruptible_sleep(self, timeout: float) -> None:
        """Sleep up to `timeout`s, waking immediately when shutdown is signalled."""
        if self.shutdown_event is None:
            await asyncio.sleep(timeout)
            return
        # TimeoutError is the normal case — slept the full interval, loop again.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=timeout)

    async def authenticate(self) -> None:
        """Authenticate with Sense API and store credentials (via the shared client)."""
        try:
            if self.client is None:
                await self.connect()
            assert self.client is not None
            response = await authenticate_with_sense(
                self.client, self.username, self.password
            )

            self.access_token = response["access_token"]
            self.user_id = response["user_id"]
            self.monitor_id = response["monitors"][0]["id"]
            self.auth_time = datetime.now(UTC)

            self.headers = {
                "Authorization": f"Bearer {self.access_token}",
                "X-Sense-Monitor-Id": str(self.monitor_id),
                "User-Agent": config.API_USER_AGENT,
            }

            api_logger.info(
                "Authenticated successfully. Monitor ID: %s", self.monitor_id
            )

        except Exception as e:
            api_logger.error("Authentication failed: %s", e)
            raise

    async def check_token_renewal(self) -> None:
        """Check if access token needs renewal."""
        if self.auth_time:
            time_since_auth = (datetime.now(UTC) - self.auth_time).total_seconds()
            if time_since_auth > config.TOKEN_RENEW_INTERVAL:
                api_logger.info("Access token expired, re-authenticating...")
                await self.authenticate()

    async def collect_sense_data(
        self, shutdown_event: asyncio.Event | None = None
    ) -> None:
        """Main method to start collecting data from Sense."""
        self.shutdown_event = shutdown_event
        try:
            # Session/auth are normally done once at startup (create_collector). Guard so we
            # don't open a second httpx client or double-hit the rate-limited auth endpoint.
            if self.client is None:
                await self.connect()
            if self.access_token is None:
                await self.authenticate()

            # Start API workers
            for i in range(config.API_WORKER_COUNT):
                worker = asyncio.create_task(self.api_worker(i))
                self.api_workers.append(worker)

            # Fetch initial device list
            await self.fetch_devices()

            # Connect to WebSocket for real-time data
            ws_url = SenseAPIEndpoints.WEBSOCKET.format(
                monitor_id=self.monitor_id, access_token=self.access_token
            )

            # Start periodic tasks
            monitor_task = asyncio.create_task(self.periodic_monitor_status())
            device_task = asyncio.create_task(self.periodic_device_fetch())

            try:
                # Run WebSocket connection
                # The access token is already carried in the WS URL (Sense's protocol), so
                # don't ALSO send it as an Authorization header — that just doubles where the
                # secret lives (and URLs leak through proxies far more than headers).
                ws_headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() != "authorization"
                }
                await handle_websocket_connection(
                    ws_url, ws_headers, self.process_websocket_data
                )
            finally:
                # Cancel periodic tasks
                monitor_task.cancel()
                device_task.cancel()
                try:
                    await monitor_task
                    await device_task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            api_logger.error("Error in collect_sense_data: %s", e)
            raise
        finally:
            await self.shutdown()

    async def api_worker(self, worker_id: int) -> None:
        """Worker to process API calls from the queue."""
        api_logger.info("API worker %s started", worker_id)

        while not self.is_shutting_down:
            try:
                # Get item from queue with timeout
                try:
                    item = await asyncio.wait_for(
                        self.api_call_queue.get(), timeout=config.QUEUE_TIMEOUT
                    )
                except TimeoutError:
                    continue

                if item is None:  # Shutdown signal
                    break

                device_id = item.get("device_id")
                if device_id:
                    # Check cache first
                    cached_data = await self.device_cache.get(device_id)
                    if cached_data:
                        await self.influxdb_storage.persist_device_data(
                            self.monitor_id, cached_data
                        )
                    else:
                        # Fetch from API
                        await self.fetch_device_data(device_id)

            except Exception as e:
                api_logger.error("Error in API worker %s: %s", worker_id, e)
                await asyncio.sleep(config.API_WORKER_ERROR_SLEEP)

        api_logger.info("API worker %s stopped", worker_id)

    def _touch_heartbeat(self) -> None:
        """Update the liveness heartbeat file (throttled). Any WebSocket message = alive.

        The Docker healthcheck (app.health.check) asserts this file stays fresh, so a
        crashed/deadlocked collector or a dead WebSocket is actually detected.
        """
        now = time.time()
        if now - self._last_heartbeat < config.HEALTH_HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        try:
            Path(config.HEALTH_HEARTBEAT_FILE).touch()
        except Exception as e:
            api_logger.debug("Failed to update heartbeat file: %s", e)

    async def process_websocket_data(self, data: dict[str, Any]) -> None:
        """Process data received from WebSocket."""
        # Any inbound frame means the collector is alive and the WS is delivering data.
        self._touch_heartbeat()
        try:
            message_type = data.get("type")

            if message_type == "realtime_update":
                payload = data.get("payload", {})
                await self.handle_realtime_update(payload)

            elif message_type == "new_timeline_event":
                payload = data.get("payload", {})
                await self.handle_timeline_event(payload)

            elif message_type == "device_states_changed":
                payload = data.get("payload", {})
                await self.handle_device_state_change(payload)

            elif message_type == "data_change":
                payload = data.get("payload", {})
                await self.handle_data_change(payload)

            elif message_type == "device_states":
                payload = data.get("payload", {})
                await self.handle_device_states(payload)

            elif message_type == "hello":
                payload = data.get("payload", {})
                await self.handle_hello(payload)

            else:
                api_logger.debug("Unhandled message type: %s", message_type)

        except Exception as e:
            api_logger.error("Error processing WebSocket data: %s", e)

    async def make_api_request(
        self, url: str, max_retries: int | None = None
    ) -> dict[str, Any] | list[Any] | None:
        """Make an API request with rate limiting and retry logic."""
        if max_retries is None:
            max_retries = config.API_RETRY_MAX

        # Rate limiting
        async with self.api_semaphore:
            # Ensure minimum interval between API calls
            current_time = time.time()
            time_since_last = current_time - self.last_api_call_time
            if time_since_last < self.min_api_interval:
                await asyncio.sleep(self.min_api_interval - time_since_last)

            self.last_api_call_time = time.time()

            retry_count = 0

            while retry_count <= max_retries:
                try:
                    if not self.client:
                        api_logger.error("HTTP client not initialized")
                        return None
                    response = await self.client.get(url, headers=self.headers)

                    if response.status_code == 429:  # Rate limited
                        retry_after = int(
                            response.headers.get(
                                "Retry-After",
                                config.API_RETRY_BACKOFF_BASE**retry_count,
                            )
                        )
                        api_logger.warning("Rate limited, waiting %ss", retry_after)
                        await asyncio.sleep(retry_after)
                        retry_count += 1
                        continue

                    response.raise_for_status()
                    payload: dict[str, Any] | list[Any] = response.json()
                    return payload

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 401:
                        # Token expired
                        api_logger.info("Token expired, re-authenticating...")
                        await self.authenticate()
                        retry_count += 1
                        continue
                    else:
                        api_logger.error("HTTP error: %s", e)
                        retry_count += 1

                except Exception as e:
                    api_logger.error("API request error: %s", e)
                    retry_count += 1

                if retry_count <= max_retries:
                    wait_time = config.API_RETRY_BACKOFF_BASE**retry_count
                    await asyncio.sleep(wait_time)

            api_logger.error("Failed to fetch %s after %s retries", url, max_retries)
            return None

    async def periodic_monitor_status(self) -> None:
        """Periodically fetch monitor status."""
        while not self._shutdown_requested():
            try:
                current_time = time.time()
                if (
                    current_time - self.last_monitor_status_time
                    >= config.MONITOR_STATUS_INTERVAL
                ):
                    await self.fetch_monitor_status()
                    self.last_monitor_status_time = current_time

                await self._interruptible_sleep(config.MONITOR_STATUS_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                api_logger.error("Error in periodic monitor status: %s", e)
                await self._interruptible_sleep(config.API_WORKER_ERROR_SLEEP)

    async def periodic_device_fetch(self) -> None:
        """Periodically fetch device list."""
        while not self._shutdown_requested():
            try:
                current_time = time.time()
                if (
                    current_time - self.last_device_list_time
                    >= config.DEVICE_LIST_INTERVAL
                ):
                    await self.fetch_devices()
                    self.last_device_list_time = current_time

                await self._interruptible_sleep(config.DEVICE_LIST_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                api_logger.error("Error in periodic device fetch: %s", e)
                await self._interruptible_sleep(config.API_WORKER_ERROR_SLEEP)

    async def fetch_devices(self) -> None:
        """Fetch list of devices from Sense API."""
        try:
            await self.check_token_renewal()

            url = SenseAPIEndpoints.DEVICES.format(monitor_id=self.monitor_id)
            response = await self.make_api_request(url)

            if response:
                # Handle response as list directly (not dict with 'devices' key)
                devices: list[dict[str, Any]] = (
                    response
                    if isinstance(response, list)
                    else response.get("devices", [])
                )
                api_logger.info("Fetched %s devices", len(devices))

                # Store device names in cache
                for device in devices:
                    device_id = device.get("id")
                    device_name = device.get("name", "Unknown")
                    if device_id:
                        # Update device name cache in InfluxDB storage
                        await self.influxdb_storage.device_queue.set_device_name(
                            device_id, device_name
                        )

                # Export to file if enabled
                if config.OUTPUT_RECEIVED_DATA:
                    await self.export_device_list(devices)

        except Exception as e:
            api_logger.error("Error fetching devices: %s", e)

    async def export_device_list(self, devices: list[dict[str, Any]]) -> None:
        """Export device list to JSON file."""
        try:
            safe_path = FilePathValidator.get_safe_export_path(
                config.EXPORT_FOLDER, "device_list", ".json"
            )

            if not safe_path:
                api_logger.error("Cannot create safe path for device list export")
                return

            async with aiofiles.open(safe_path, "w") as f:
                await f.write(json.dumps(devices, indent=2))

            api_logger.info("Exported %s devices to %s", len(devices), safe_path)

        except Exception as e:
            api_logger.error("Error exporting device list: %s", e)

    async def fetch_device_data(self, device_id: str) -> None:
        """Fetch detailed data for a specific device."""
        try:
            await self.check_token_renewal()

            url = SenseAPIEndpoints.DEVICE_DETAILS.format(
                monitor_id=self.monitor_id, device_id=device_id
            )
            response = await self.make_api_request(url)

            if isinstance(response, dict):
                # Cache the data
                await self.device_cache.put(device_id, response)

                # Persist to InfluxDB
                await self.influxdb_storage.persist_device_data(
                    self.monitor_id, response
                )

                # Export if enabled
                if config.OUTPUT_RECEIVED_DATA and isinstance(response, dict):
                    await self.export_device_data(device_id, response)

        except Exception as e:
            api_logger.error("Error fetching data for device %s: %s", device_id, e)

    async def export_device_data(self, device_id: str, data: dict[str, Any]) -> None:
        """Export device data to JSON file."""
        try:
            safe_path = FilePathValidator.get_safe_export_path(
                config.EXPORT_FOLDER, device_id, ".json"
            )

            if not safe_path:
                api_logger.error("Cannot create safe path for device %s", device_id)
                return

            async with aiofiles.open(safe_path, "w") as f:
                await f.write(json.dumps(data, indent=2))

        except Exception as e:
            api_logger.error("Error exporting device data: %s", e)

    async def fetch_monitor_status(self) -> None:
        """Fetch monitor status from Sense API."""
        try:
            await self.check_token_renewal()

            url = SenseAPIEndpoints.MONITOR_STATUS.format(monitor_id=self.monitor_id)
            response = await self.make_api_request(url)

            if isinstance(response, dict):
                await self.influxdb_storage.persist_monitor_status(
                    self.monitor_id, response
                )

        except Exception as e:
            api_logger.error("Error fetching monitor status: %s", e)

    async def handle_realtime_update(self, payload: dict[str, Any]) -> None:
        """Handle real-time power usage updates."""
        required_keys = ["hz", "c", "w", "epoch"]
        if not all(key in payload for key in required_keys):
            api_logger.error("Missing required keys in realtime update")
            return

        try:
            # Track activity and log less frequently for realtime updates
            total_watts = payload["w"]
            device_count = len(payload.get("devices", []))
            self.activity_stats["realtime_updates"] += 1

            # Only log every 50th realtime update to avoid spam
            if self.activity_stats["realtime_updates"] % 50 == 0:
                api_logger.info(
                    "Processing realtime update #%s: %.1fW total, %s devices",
                    self.activity_stats["realtime_updates"],
                    total_watts,
                    device_count,
                )

            await self._log_periodic_summary()

            await self.influxdb_storage.persist_realtime_data(
                monitor_id=self.monitor_id,
                hertz=payload["hz"],
                total_current=payload["c"],
                total_watts=total_watts,
                epoch=payload["epoch"],
                voltage=payload.get("voltage", []),
                devices=payload.get("devices", []),
                channels=payload.get("channels", []),
            )
        except Exception as e:
            api_logger.error("Error persisting realtime data: %s", e)

    async def handle_timeline_event(self, payload: dict[str, Any]) -> None:
        """Handle new timeline events."""
        items_added = payload.get("items_added", [])

        if items_added:
            api_logger.info("Processing %s timeline events", len(items_added))
            self.activity_stats["timeline_events"] += len(items_added)

        for item in items_added:
            device_id = item.get("device_id")
            device_name = item.get("device_name", "Unknown")
            event_type = item.get("type", "unknown")

            if device_id:
                api_logger.info(
                    "Timeline event: %s (%s) - %s", device_name, device_id, event_type
                )
                # Persist timeline event to InfluxDB
                await self.process_timeline_item(item)
                # Queue device data fetch
                try:
                    self.api_call_queue.put_nowait({"device_id": device_id})
                except asyncio.QueueFull:
                    api_logger.warning("Queue full, skipping device %s", device_id)
            else:
                api_logger.warning("Timeline item missing device_id")

    async def process_timeline_item(self, item: dict[str, Any]) -> None:
        """Process individual timeline item."""
        try:
            device_id = item.get("device_id", "unknown")

            # Get device name and icon from cache
            cached_data = await self.device_cache.get(device_id)
            if cached_data and "device" in cached_data:
                device_name = cached_data["device"].get("name", device_id)
                icon = cached_data["device"].get("icon", item.get("icon", ""))
            else:
                device_name = device_id  # Will trigger queueing
                icon = item.get("icon", "")

            await self.influxdb_storage.persist_timeline_data(
                monitor_id=self.monitor_id,
                device_id=device_id,
                device_name=device_name,
                time=item.get("time", 0),
                event_type=item.get("type", ""),
                icon=icon,
                body=item.get("body", ""),
                device_state=item.get("device_state", ""),
                user_device_type=item.get("user_device_type", ""),
                device_transition_from_state=item.get(
                    "device_transition_from_state", ""
                ),
            )
        except Exception as e:
            api_logger.error("Error processing timeline item: %s", e)

    async def _persist_device_states(self, states: list[Any]) -> None:
        """Queue a detail fetch + persist each device state. Shared by the two state handlers
        (Sense sends both a 'device_states' message with a `states` list and a
        'device_states_changed' message with a `device_states` list — same per-item shape)."""
        timestamp = int(datetime.now(UTC).timestamp())
        for state in states:
            if not isinstance(state, dict):
                api_logger.warning("Expected state to be a dict, got %s", type(state))
                continue
            device_id = state.get("device_id")
            if not device_id:
                api_logger.warning("Device state missing device_id")
                continue
            mode = state.get("mode", "")
            device_state = state.get("state", "")
            api_logger.debug(
                "Device state: %s - mode:%s, state:%s", device_id, mode, device_state
            )

            try:
                self.api_call_queue.put_nowait({"device_id": device_id})
            except asyncio.QueueFull:
                api_logger.warning(
                    "Queue full, skipping device fetch for %s", device_id
                )

            try:
                await self.influxdb_storage.persist_device_state(
                    monitor_id=self.monitor_id,
                    device_id=device_id,
                    mode=mode,
                    device_state=device_state,
                    timestamp=timestamp,
                )
            except Exception as e:
                api_logger.error(
                    "Error persisting device state for %s: %s", device_id, e
                )

    async def handle_device_state_change(self, payload: dict[str, Any]) -> None:
        """Handle 'device_states_changed' messages (payload key: device_states)."""
        states = payload.get("device_states", [])
        if states:
            api_logger.info("Processing %s device state changes", len(states))
            self.activity_stats["device_state_changes"] += len(states)
        await self._persist_device_states(states)

    async def handle_device_states(self, payload: dict[str, Any]) -> None:
        """Handle 'device_states' messages (payload key: states)."""
        try:
            states = payload.get("states", [])
            if not isinstance(states, list):
                api_logger.warning("Expected states to be a list, got %s", type(states))
                return
            if not states:
                api_logger.debug("Received device_states message with no states")
                return

            update_type = payload.get("update_type", "unknown")
            api_logger.debug(
                "Processing %s device states (update_type: %s)",
                len(states),
                update_type,
            )
            self.activity_stats["device_state_changes"] += len(states)
            await self._persist_device_states(states)
        except Exception as e:
            api_logger.error("Error handling device states: %s", e)

    async def handle_data_change(self, payload: dict[str, Any]) -> None:
        """Handle data change events."""
        try:
            user_version = payload.get("user_version", {})
            device_id = payload.get("device_id", "unknown")

            api_logger.info(
                "Data change event for device %s: version %s", device_id, user_version
            )
            self.activity_stats["data_changes"] += 1

            # Parse timestamp
            timestamp_str = payload.get("timestamp")
            epoch_timestamp = None
            if timestamp_str:
                try:
                    epoch_timestamp = convert_to_epoch(timestamp_str)
                except Exception as e:
                    api_logger.warning("Failed to parse timestamp: %s", e)

            # Handle user_version properly - preserve integer type when possible
            if isinstance(user_version, dict):
                # If it's a dict, extract the version field and preserve its type
                version_value = user_version.get("version", "")
            else:
                # user_version is a primitive type (int, str, etc.) - preserve original type
                version_value = user_version

            await self.influxdb_storage.persist_data_change_event(
                monitor_id=self.monitor_id,
                device_id=device_id,
                user_version=version_value,
                guid=payload.get("guid", ""),
                epoch_timestamp=epoch_timestamp,
                influxdb_timestamp=int(datetime.now(UTC).timestamp()),
            )
        except Exception as e:
            api_logger.error("Error handling data change: %s", e)

    async def handle_hello(self, payload: dict[str, Any]) -> None:
        """Handle hello/connection events."""
        try:
            online = payload.get("online", False)
            timestamp = int(datetime.now(UTC).timestamp())

            await self.influxdb_storage.persist_hello_event(
                self.monitor_id, online, timestamp
            )

            api_logger.info("Hello event - Monitor online: %s", online)

        except Exception as e:
            api_logger.error("Error handling hello event: %s", e)

    async def shutdown(self) -> None:
        """Gracefully shutdown the collector."""
        api_logger.info("Shutting down Sense collector...")
        self.is_shutting_down = True

        # Signal workers to stop
        for _ in self.api_workers:
            await self.api_call_queue.put(None)

        # Wait for workers to finish
        if self.api_workers:
            await asyncio.gather(*self.api_workers, return_exceptions=True)

        # Clear cache
        await self.device_cache.clear()

        # Close HTTP session
        await self.close()

        api_logger.info("Sense collector shutdown complete")

    async def _log_periodic_summary(self) -> None:
        """Log periodic summary of activity."""
        current_time = time.time()
        time_since_last = current_time - self.activity_stats["last_summary_time"]

        # Log summary every 5 minutes (300 seconds)
        if time_since_last >= 300:
            total_events = (
                self.activity_stats["realtime_updates"]
                + self.activity_stats["timeline_events"]
                + self.activity_stats["device_state_changes"]
                + self.activity_stats["data_changes"]
            )

            # Get InfluxDB stats
            influx_stats = self.influxdb_storage.write_stats

            api_logger.info(
                "Activity Summary (last %.1f min): Realtime: %s, Timeline: %s, "
                "State changes: %s, Data changes: %s, Total events: %s, "
                "InfluxDB writes: %s success / %s failed",
                time_since_last / 60,
                self.activity_stats["realtime_updates"],
                self.activity_stats["timeline_events"],
                self.activity_stats["device_state_changes"],
                self.activity_stats["data_changes"],
                total_events,
                influx_stats["successful"],
                influx_stats["failed"],
            )

            # Reset counters for next period
            self.activity_stats = {
                "realtime_updates": 0,
                "timeline_events": 0,
                "device_state_changes": 0,
                "data_changes": 0,
                "last_summary_time": current_time,
            }
