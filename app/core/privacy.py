"""The collector's log redactor, passed to configure_logging(redact=...) in app.main.

Two things must never reach a log line, whatever level or format is configured:

- The Sense access token. The realtime WebSocket URL carries it as `?access_token=...`
  (Sense requires it there; removing it caused an outage, SENSECOLLE-21), and the websockets
  library logs that request line at DEBUG. LOG_LEVEL=DEBUG or a LOG_MODULE_LEVELS override
  would otherwise print the live token.
- Control characters from remote data. Sense device names are user-editable; with
  LOG_FORMAT=text a newline in one forges a whole log line, and an ANSI escape drives the
  terminal reading `docker logs`. JSON escapes them on its own; text mode does not.

The module calls this on the envelope and, separately, on the caller's fields, each as a flat
dict; it fails closed if this raises, so it must not.
"""

import re
from collections.abc import Mapping
from typing import Any

_TOKEN = re.compile(r"(access_token=)[^&\s\"'<>]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# Formatted tracebacks and stacks are multi-line by construction and come from the code, not a
# remote payload: their newlines stay, only a token in them is scrubbed.
_MULTILINE = frozenset({"exception", "stack"})


def _escape(match: re.Match[str]) -> str:
    return f"\\x{ord(match.group()):02x}"


def _clean(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = _TOKEN.sub(r"\1[REDACTED]", value)
    return value if key in _MULTILINE else _CONTROL.sub(_escape, value)


def redact(line: Mapping[str, Any]) -> dict[str, Any]:
    """`line` with the access token removed and control characters escaped."""
    return {key: _clean(key, value) for key, value in line.items()}
