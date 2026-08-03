import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import aiofiles
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException

from app.core import config
from app.utils.file_validator import FilePathValidator
from app.utils.logging import api_logger


class WebSocketHandler:
    """Handles WebSocket connection with automatic reconnection and health monitoring."""

    def __init__(
        self,
        ws_url: str,
        headers: dict[str, str],
        process_data_callback: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.ws_url = ws_url
        self.headers = headers
        self.process_data_callback = process_data_callback
        self.ws: ClientConnection | None = None
        self.last_message_time = time.time()
        self.connection_start_time = time.time()
        self.reconnect_count = 0
        self.is_shutting_down = False

    async def connect(self) -> bool:
        """Establish WebSocket connection with proper error handling."""
        try:
            self.ws = await websockets.connect(
                self.ws_url,
                additional_headers=self.headers,
                ping_interval=config.WS_HEARTBEAT_INTERVAL,
                ping_timeout=config.WS_HEARTBEAT_TIMEOUT,
            )
            self.connection_start_time = time.time()
            self.last_message_time = time.time()
            api_logger.info(
                "WebSocket connected successfully (attempt #%s)",
                self.reconnect_count + 1,
            )
            return True
        except Exception as e:
            api_logger.error("Failed to connect WebSocket: %s", e)
            return False

    async def send_ping(self) -> bool:
        """Send ping message to keep connection alive."""
        # websockets 16 dropped the `.open` attribute; just attempt the send and let a
        # closed connection raise (caught below) rather than pre-checking state.
        if self.ws is None:
            return False
        try:
            await self.ws.send(json.dumps({"type": "ping"}))
            api_logger.debug("Ping sent successfully")
            return True
        except Exception as e:
            api_logger.error("Failed to send ping: %s", e)
            return False

    async def handle_message(self, msg: str) -> bool:
        """Process incoming WebSocket message. Returns False if connection should close."""
        try:
            data = json.loads(msg)

            # Write to file asynchronously if enabled
            if config.OUTPUT_RECEIVED_DATA:
                await self._write_received_data_async(data)

            # Process the data
            await self.process_data_callback(data)
            self.last_message_time = time.time()

            # Check if this is a disconnection message from Sense
            if (
                data.get("type") == "error"
                and "disconnect" in str(data.get("message", "")).lower()
            ):
                api_logger.warning("Received disconnection notice from Sense")
                return False

            return True

        except json.JSONDecodeError as e:
            api_logger.error("Failed to decode WebSocket message: %s", e)
            return True
        except Exception as e:
            api_logger.error("Error processing WebSocket message: %s", e)
            return True

    async def _write_received_data_async(self, data: dict[str, Any]) -> None:
        """Write received data to file asynchronously (newline-delimited JSON: .jsonl)."""
        try:
            # Use a fixed, safe filename for received data. Raw dumps are newline-delimited
            # JSON (one object per line) — the fleet standard's .jsonl convention.
            safe_path = FilePathValidator.get_safe_export_path(
                config.EXPORT_FOLDER, "received_data", ".jsonl"
            )

            if not safe_path:
                api_logger.error("Cannot create safe path for received data")
                return

            async with aiofiles.open(safe_path, "a") as f:
                await f.write(json.dumps(data) + "\n")
        except Exception as e:
            api_logger.error("Failed to write received data to file: %s", e)

    async def monitor_connection_health(self) -> bool:
        """Monitor connection health and trigger reconnection if needed."""
        while not self.is_shutting_down:
            try:
                current_time = time.time()
                time_since_last_message = current_time - self.last_message_time
                connection_duration = current_time - self.connection_start_time

                # Check if we haven't received data in a while
                if time_since_last_message > config.WS_HEARTBEAT_TIMEOUT:
                    api_logger.warning(
                        "No data received for %.1fs, reconnecting...",
                        time_since_last_message,
                    )
                    return False

                # Send periodic pings
                if (
                    time_since_last_message > config.WS_HEARTBEAT_INTERVAL
                    and not await self.send_ping()
                ):
                    api_logger.warning("Ping failed, reconnecting...")
                    return False

                # Log connection health periodically
                if int(connection_duration) % config.WS_HEALTH_LOG_INTERVAL == 0:
                    api_logger.info(
                        "Connection healthy - Duration: %.1fm, Last message: %.1fs ago",
                        connection_duration / 60,
                        time_since_last_message,
                    )

                await asyncio.sleep(config.WS_HEALTH_CHECK_INTERVAL)

            except Exception as e:
                api_logger.error("Error in connection health monitor: %s", e)
                return False

        return True

    async def run(self) -> None:
        """Main loop to handle WebSocket connection with automatic reconnection."""
        backoff_delay = config.WS_RECONNECT_DELAY_INITIAL

        while not self.is_shutting_down:
            message_task: asyncio.Task[bool] | None = None
            health_task: asyncio.Task[bool] | None = None
            try:
                if await self.connect():
                    # Reset backoff on successful connection
                    backoff_delay = config.WS_RECONNECT_DELAY_INITIAL

                    message_task = asyncio.create_task(self._handle_messages())
                    health_task = asyncio.create_task(self.monitor_connection_health())

                    # Wait for either task to complete
                    done, _ = await asyncio.wait(
                        {message_task, health_task}, return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in done:
                        try:
                            should_continue = await task
                            if not should_continue:
                                api_logger.info(
                                    "Connection closed, preparing to reconnect..."
                                )
                        except Exception as e:
                            api_logger.error("Task error: %s", e)

            except asyncio.CancelledError:
                # Shutdown/cancel: stop cleanly and skip the reconnection delay below
                # (the re-raised CancelledError never reaches it).
                self.is_shutting_down = True
                raise

            except Exception as e:
                api_logger.error("Unexpected error in WebSocket handler: %s", e)

            finally:
                # Always cancel + await the child tasks so they never leak, including on the
                # cancel path.
                cleanup_tasks = [
                    t for t in (message_task, health_task) if t is not None
                ]
                for t in cleanup_tasks:
                    if not t.done():
                        t.cancel()
                if cleanup_tasks:
                    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                if self.ws is not None:
                    with contextlib.suppress(Exception):
                        await self.ws.close()
                    self.ws = None

            # Reconnection backoff lives outside try/finally and is skipped on shutdown/cancel,
            # so SIGTERM doesn't stall up to WS_RECONNECT_DELAY_CAP.
            if not self.is_shutting_down:
                await self._handle_reconnection_delay(backoff_delay)
                backoff_delay = min(backoff_delay * 2, config.WS_RECONNECT_DELAY_CAP)
                self.reconnect_count += 1

    async def _handle_messages(self) -> bool:
        """Handle incoming WebSocket messages."""
        if not self.ws:
            return False

        try:
            async for msg in self.ws:
                msg_str = msg.decode("utf-8") if isinstance(msg, bytes) else msg
                if not await self.handle_message(msg_str):
                    return False

                if self.is_shutting_down:
                    return False

        except WebSocketException as e:
            api_logger.error("WebSocket error: %s", e)
            return False
        except Exception as e:
            api_logger.error("Error in message handler: %s", e)
            return False

        return False

    async def _handle_reconnection_delay(self, delay: float) -> None:
        """Handle reconnection delay with logging."""
        next_reconnect_time = datetime.now() + timedelta(seconds=delay)
        api_logger.info(
            "Reconnecting in %ss at %s",
            delay,
            next_reconnect_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await asyncio.sleep(delay)

    async def shutdown(self) -> None:
        """Gracefully shutdown the WebSocket connection."""
        api_logger.info("Shutting down WebSocket handler...")
        self.is_shutting_down = True

        if self.ws:
            await self.ws.close()


async def handle_websocket_connection(
    ws_url: str,
    headers: dict[str, str],
    process_data_callback: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """Construct a WebSocketHandler and run its connect/reconnect loop."""
    handler = WebSocketHandler(ws_url, headers, process_data_callback)
    await handler.run()
