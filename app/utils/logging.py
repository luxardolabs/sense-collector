"""The collector's three per-area loggers: general, api (Sense REST + WebSocket) and storage.

Output is configured ONCE, in app.main, by the fleet's emitted logging module
(app.core.logging_config.configure_logging): one JSON object per line on stdout, or a readable
line with LOG_FORMAT=text for local development. This module only names the loggers and their
levels; AREA_LEVELS is handed to configure_logging(levels=...).

Remote-derived values (Sense device names are user-editable) cannot forge a log line, and the
WebSocket access token cannot be printed, in either format: app.main passes app/core/privacy.py's
redactor to configure_logging.
"""

import logging

from app.core import config

# SENSE_COLLECTOR_LOG_LEVEL_{GENERAL,API,STORAGE}. httpx/httpcore log every request at INFO;
# they stay at WARNING so a poll cycle is not a page of request lines. hpack/h2 log every
# HTTP/2 header block at DEBUG (the REST Bearer token among them); app.main also drops their
# records at the handler, so no level setting can bring them back.
AREA_LEVELS: dict[str, str | int] = {
    "general": config.LOG_LEVEL_GENERAL,
    "api": config.LOG_LEVEL_API,
    "storage": config.LOG_LEVEL_STORAGE,
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "hpack": "WARNING",
    "h2": "WARNING",
}

logger = logging.getLogger("general")
api_logger = logging.getLogger("api")
storage_logger = logging.getLogger("storage")
