#!/usr/bin/env python3
"""Docker health check script for Sense Collector.

Standalone script used as the Docker HEALTHCHECK (see the Dockerfile). It asserts the
running collector's **heartbeat file** is fresh: the main loop touches that file (throttled)
as WebSocket data flows (see SenseCollector._touch_heartbeat). If the collector has crashed,
deadlocked, lost the WebSocket, or is stuck re-authing, the heartbeat stops updating and this
check reports unhealthy.

Exit codes:
    0 - Healthy (heartbeat fresh)
    1 - Unhealthy (heartbeat missing or stale)

Configuration:
    SENSE_COLLECTOR_HEALTH_CHECK_MAX_AGE:     Max heartbeat age in seconds (default 120).
    SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE:    Heartbeat file path (default <export>/.heartbeat).
    SENSE_COLLECTOR_EXPORT_FOLDER:            Output dir the default heartbeat path lives under.
"""

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

MAX_AGE_SECONDS = int(os.getenv("SENSE_COLLECTOR_HEALTH_CHECK_MAX_AGE", "120"))
OUTPUT_DIR = os.getenv("SENSE_COLLECTOR_EXPORT_FOLDER", "output")
HEARTBEAT_FILE = os.getenv(
    "SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE", str(Path(OUTPUT_DIR) / ".heartbeat")
)


def check_heartbeat() -> tuple[bool, str]:
    """Verify the collector is alive by checking the heartbeat file's freshness."""
    hb = Path(HEARTBEAT_FILE)
    if not hb.exists():
        # Expected briefly at startup (before the first WebSocket message) — the Dockerfile's
        # HEALTHCHECK --start-period covers that window.
        return False, f"no heartbeat file at {HEARTBEAT_FILE} yet"

    age = datetime.now() - datetime.fromtimestamp(hb.stat().st_mtime)
    if age > timedelta(seconds=MAX_AGE_SECONDS):
        return (
            False,
            f"heartbeat is {age.total_seconds():.0f}s old (max allowed: {MAX_AGE_SECONDS}s)",
        )
    return True, f"healthy - heartbeat {age.total_seconds():.0f}s ago"


def main() -> None:
    """Execute the health check and exit 0 (healthy) or 1 (unhealthy)."""
    is_healthy, message = check_heartbeat()

    version = os.getenv("SENSE_COLLECTOR_VERSION", "unknown")
    build_timestamp = os.getenv("SENSE_COLLECTOR_BUILD_TIMESTAMP", "unknown")

    print(f"Health check: {'HEALTHY' if is_healthy else 'UNHEALTHY'}")
    print(f"Version: {version} (Built: {build_timestamp})")
    print(f"  - Heartbeat: {message}")

    sys.exit(0 if is_healthy else 1)


if __name__ == "__main__":
    main()
