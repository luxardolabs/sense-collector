import asyncio
import contextlib
import os
import signal
import sys
from datetime import datetime

from app.collector.client import SenseCollector
from app.core import config
from app.storage.influxdb import InfluxDBStorage
from app.utils.logging import logger

_SENSITIVE = ("PASSWORD", "TOKEN", "USERNAME")


def _obscure(value: str) -> str:
    """Partially mask a secret; fully redact values too short to partially mask."""
    if not value:
        return value
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def validate_environment() -> None:
    """Log the effective config (masking secrets) and fail fast if a required var is missing.

    describe_settings() is the single source of truth for the startup log — no hand-kept var
    list that drifts. Secrets and the account email (PII) are obscured; everything else is shown.
    """
    logger.info("Environment variable settings:")
    for var, value in config.describe_settings().items():
        shown = _obscure(value) if any(s in var for s in _SENSITIVE) else value
        logger.info("  %s: %s", var, shown)

    missing = [v for v in config.REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        raise ValueError("Missing required environment variables")


async def create_collector() -> tuple[SenseCollector, InfluxDBStorage]:
    """Create + initialize the InfluxDBStorage and SenseCollector (opens both clients, auths once)."""
    influxdb_params = {
        "url": config.INFLUXDB_URL,
        "token": config.INFLUXDB_TOKEN,
        "org": config.INFLUXDB_ORG,
        "bucket": config.INFLUXDB_BUCKET,
    }

    influxdb_storage = InfluxDBStorage(influxdb_params)
    # Verify InfluxDB connectivity (async) + start the queue processor.
    await influxdb_storage.connect()

    collector = SenseCollector(
        username=config.API_USERNAME,
        password=config.API_PASSWORD,
        influxdb_storage=influxdb_storage,
    )

    # Open the HTTP client + authenticate ONCE here (fast-fail on bad creds before starting
    # tasks); collect_sense_data() reuses this client + token.
    try:
        logger.info("Attempting to authenticate with Sense API")
        await collector.connect()
        await collector.authenticate()
        logger.info(
            "Successfully authenticated. Monitor ID: %s, User ID: %s",
            collector.monitor_id,
            collector.user_id,
        )
    except Exception:
        await collector.close()
        raise

    return collector, influxdb_storage


async def run_collector_tasks(
    collector: SenseCollector, shutdown_event: asyncio.Event
) -> None:
    """Run the collector's data task, stopping cleanly when shutdown is requested.

    Any non-shutdown task ending is a failure that must propagate, so the process exits
    non-zero and restart-on-failure (docker/k8s) actually restarts it.
    """
    data_task = asyncio.create_task(
        collector.collect_sense_data(shutdown_event), name="Data Collection"
    )
    shutdown_task = asyncio.create_task(shutdown_event.wait(), name="Shutdown")
    tasks = {data_task, shutdown_task}

    failure: BaseException | None = None
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            name = task.get_name()
            try:
                await task
                if name == "Shutdown":
                    logger.info("Shutdown requested, stopping all tasks...")
                else:
                    logger.error(
                        "Task '%s' exited unexpectedly (should run forever)", name
                    )
                    failure = failure or RuntimeError(
                        f"Task '{name}' exited unexpectedly"
                    )
            except Exception as e:
                logger.error("Task '%s' failed with error: %s", name, e)
                if name != "Shutdown":
                    failure = failure or e

        # Cancel + drain whatever is still running.
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    except Exception as e:
        logger.error("Error in task execution: %s", e)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if failure is not None:
        raise failure


async def main() -> None:
    """Main application entry point."""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("Welcome to Sense Collector! Current time: %s", current_time)
    logger.info("Build Version: %s", config.BUILD_VERSION)
    logger.info("Build Timestamp: %s", config.BUILD_TIMESTAMP)

    validate_environment()

    collector: SenseCollector | None = None
    influxdb_storage: InfluxDBStorage | None = None

    shutdown_event = asyncio.Event()

    def _handle_signal(signum: int) -> None:
        logger.info("Received signal %s, initiating graceful shutdown...", signum)
        shutdown_event.set()

    # add_signal_handler is the asyncio-correct way to catch signals on the running loop.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # add_signal_handler is unavailable on some platforms (e.g. Windows).
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _handle_signal, sig)

    try:
        # Create collector and storage instances (opens the clients + authenticates once).
        collector, influxdb_storage = await create_collector()

        # Run all tasks until shutdown or a fatal task failure.
        logger.info("Starting all Sense Collector tasks")
        await run_collector_tasks(collector, shutdown_event)

    except Exception as e:
        logger.error("Fatal error in main: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        # Always flush/close on the way out.
        logger.info("Performing cleanup...")

        if collector:
            try:
                await collector.close()
                logger.info("Collector session closed")
            except Exception as e:
                logger.error("Error closing collector session: %s", e)

        if influxdb_storage:
            try:
                await influxdb_storage.close()
            except Exception as e:
                logger.error("Error shutting down InfluxDB storage: %s", e)

        logger.info("Sense Collector shutdown complete")


if __name__ == "__main__":
    logger.info("Starting Sense Collector")

    # Configure asyncio for production (Python 3.14 baseline — Runner always available).
    try:
        with asyncio.Runner() as runner:
            runner.run(main())
    except ValueError:
        # Raised by validate_environment on a missing required var — exit non-zero cleanly.
        sys.exit(1)
