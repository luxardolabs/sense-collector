import asyncio
import contextlib
import logging
import os
import signal
import sys
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from app.collector.client import SenseCollector
from app.core import config
from app.core.logging_config import configure_logging
from app.core.privacy import redact
from app.storage.influxdb import InfluxDBStorage
from app.utils.logging import AREA_LEVELS, logger

_SENSITIVE = ("PASSWORD", "TOKEN", "USERNAME")
# Names that match a marker but hold no secret (an interval in seconds).
_NOT_SECRET = frozenset({"SENSE_COLLECTOR_API_TOKEN_RENEW"})


def _obscure(value: str) -> str:
    """Whether a secret is set, and nothing else: no character, no length."""
    return "<set>" if value else "<unset>"


def _without_userinfo(url: str) -> str:
    """`url` with any user:password@ removed, for logging; never raises."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable URL>"
    _, at, hostport = parts.netloc.rpartition("@")
    shown = urlunsplit(parts._replace(netloc=hostport)) if at else url
    if "@" in shown:
        # Userinfo urlsplit did not see: no scheme (`user:pw@host` read as a path), or an
        # unencoded `/`, `?` or `#` in the password ending the authority early.
        return "<URL with credentials>"
    return shown


def _url_problem(url: str, authenticates_with: str) -> str | None:
    """Why a configured URL cannot be used, or None.

    No URL here takes credentials (each service authenticates another way), and a
    user:password URL would be printed by the startup log and by every error line that
    names the URL.
    """
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return "is not a valid URL"
    # Any `@` is refused, not only one urlsplit reads as userinfo: without a scheme it reads
    # `user:pw@host` as a path, and an unencoded `/`, `?` or `#` in the password ends the
    # authority early (`http://admin:2024/pw@host`). None of these URLs needs an `@`.
    if "@" in url:
        return f"must not contain credentials: {authenticates_with}"
    return None


def _not_http2_internals(record: logging.LogRecord) -> bool:
    """False for hpack/h2 records: hpack logs each encoded header block, which carries the
    REST `Authorization: Bearer` token Huffman-coded, out of the token regex's sight."""
    return record.name.split(".", 1)[0] not in ("hpack", "h2")


def setup_logging() -> None:
    """The fleet's one logging setup (JSON lines on stdout), with this collector's levels.

    `redact` keeps the WebSocket access token and remote control characters out of every
    line, at any LOG_LEVEL and in either LOG_FORMAT (app/core/privacy.py). The handler filter
    drops the HTTP/2 libraries' records whatever LOG_LEVEL or LOG_MODULE_LEVELS say.
    """
    configure_logging(
        service="sense-collector",
        version=config.BUILD_VERSION,
        levels=AREA_LEVELS,
        redact=redact,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_not_http2_internals)


def validate_environment() -> None:
    """Log the effective config (masking secrets) and fail fast if a required var is missing.

    describe_settings() is the single source of truth for the startup log — no hand-kept var
    list that drifts. Secrets and the account email (PII) are obscured; everything else is shown.
    """
    logger.info("Environment variable settings:")
    for var, value in config.describe_settings().items():
        if var not in _NOT_SECRET and any(s in var for s in _SENSITIVE):
            shown = _obscure(value)
        elif var.endswith("_URL"):
            shown = _without_userinfo(value)
        else:
            shown = value
        logger.info("  %s: %s", var, shown)

    missing = [v for v in config.REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        raise ValueError("Missing required environment variables")

    if config.OUTPUT_RECEIVED_DATA and config.EXPORT_FOLDER_UNWRITABLE:
        logger.error(
            "SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA is on but the export folder %s is not "
            "writable by this user (uid %s): create it and give it to that user, or turn "
            "exports off. Exports to a temp dir would vanish with the container.",
            config.EXPORT_FOLDER_UNWRITABLE,
            os.getuid(),
        )
        raise ValueError("The export folder is not writable")

    sense_login = "Sense authenticates with SENSE_COLLECTOR_API_USERNAME/PASSWORD"
    for var, url, authenticates_with in (
        (
            "SENSE_COLLECTOR_INFLUXDB_URL",
            config.INFLUXDB_URL,
            "InfluxDB 2.x authenticates with SENSE_COLLECTOR_INFLUXDB_TOKEN",
        ),
        ("SENSE_COLLECTOR_API_BASE_URL", config.API_BASE_URL, sense_login),
        ("SENSE_COLLECTOR_WS_BASE_URL", config.WS_BASE_URL, sense_login),
    ):
        problem = _url_problem(url, authenticates_with)
        if problem:
            logger.error("%s %s.", var, problem)
            raise ValueError(f"{var} {problem}")

    # A stack that reads the real Sense cloud holds a household's data, so its InfluxDB
    # token must be its own: the demo/e2e tokens are public in this repository.
    # INFLUX_TOKEN is the bundled InfluxDB's admin token (the dev stacks' env file sets it).
    tokens = {
        config.INFLUXDB_TOKEN.strip(),
        os.environ.get("INFLUX_TOKEN", "").strip(),
    }
    if tokens & config.PUBLIC_INFLUXDB_TOKENS and config.is_sense_cloud(
        config.API_BASE_URL
    ):
        logger.error(
            "SENSE_COLLECTOR_INFLUXDB_TOKEN or INFLUX_TOKEN is a token committed in this "
            "public repository (.env.demo / .env.e2e) and this stack reads the real Sense "
            "cloud. Give the InfluxDB a token of its own (and rotate it there) before starting."
        )
        raise ValueError("Refusing to write real data with a public InfluxDB token")


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
    # Create aware in UTC, convert to the container's zone only for display (the log
    # line an operator reads). A bare datetime.now() is naive-local: no offset, so the
    # same value means a different instant to anything that consumes it.
    current_time = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
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

    # swallowed-exceptions: top-level fatal handler — it logs the full
    # traceback and exits non-zero, which IS handling; the container restarts.
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
            # swallowed-exceptions: teardown must not abort the rest of
            # cleanup; InfluxDB still needs closing after this.
            except Exception as e:
                logger.error("Error closing collector session: %s", e)

        if influxdb_storage:
            try:
                await influxdb_storage.close()
            # swallowed-exceptions: last step of teardown on the way out of the process;
            # there is nothing left to protect and the exit path must not raise.
            except Exception as e:
                logger.error("Error shutting down InfluxDB storage: %s", e)

        logger.info("Sense Collector shutdown complete")


if __name__ == "__main__":
    # First, before any line is logged.
    setup_logging()
    logger.info("Starting Sense Collector")

    # Configure asyncio for production (Python 3.14 baseline — Runner always available).
    try:
        with asyncio.Runner() as runner:
            runner.run(main())
    except ValueError:
        # Raised by validate_environment on a missing required var — exit non-zero cleanly.
        sys.exit(1)
