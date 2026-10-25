"""Where the liveness heartbeat lives, decided the same way by the collector and the healthcheck.

They are separate processes, so neither can tell the other where it put the file. Both call
default_heartbeat_file() with the configured export folder: when that folder is writable the
heartbeat sits in it (one per instance, and on its volume under a read-only root), and when it
is not, both fall back to one fixed path instead of the collector's temp export dir, which the
healthcheck could never find. No environment is read here, and nothing runs at import.
"""

import os
from pathlib import Path

FALLBACK_HEARTBEAT_FILE = "/tmp/sense-collector.heartbeat"


def default_heartbeat_file(export_folder: str) -> str:
    """`<export_folder>/.heartbeat` if this user can write there, else the fixed fallback."""
    folder = Path(export_folder)
    if folder.is_dir() and os.access(folder, os.W_OK | os.X_OK):
        return str(folder / ".heartbeat")
    return FALLBACK_HEARTBEAT_FILE
