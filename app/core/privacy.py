"""The collector's log redactor, passed to configure_logging(redact=...) in app.main.

Two things must never reach a log line, whatever level or format is configured:

- The Sense access token. The realtime WebSocket URL carries it as `?access_token=...`
  (Sense reads it there), and the websockets library logs that request line at DEBUG.
  LOG_LEVEL=DEBUG or a LOG_MODULE_LEVELS override would otherwise print the live token. The
  REST calls send it as `Authorization: Bearer ...`, which an exception message can quote.
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
_BEARER = re.compile(r"(\bbearer\s+)[^\s\"'<>,;}\]]+", re.IGNORECASE)
# C0, DEL and C1, plus the Unicode line and paragraph separators: str.splitlines() and many
# log viewers break lines on U+2028/U+2029/NEL too, and U+009B is an 8-bit CSI. A lone
# surrogate (valid in JSON, not in UTF-8) would make the whole line fail to encode and vanish.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f  \ud800-\udfff]")
# Formatted tracebacks and stacks are multi-line by construction, so their newlines stay and
# only a token in them is scrubbed. An exception message can still quote remote text: the
# logging module writes a text-mode traceback on indented, escaped lines of its own, and a JSON
# record escapes it, so that text cannot start a record of its own in either format.
_MULTILINE = frozenset({"exception", "stack"})


def _escape(match: re.Match[str]) -> str:
    return f"\\x{ord(match.group()):02x}"


def _clean(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = _TOKEN.sub(r"\1[REDACTED]", value)
    value = _BEARER.sub(r"\1[REDACTED]", value)
    return value if key in _MULTILINE else _CONTROL.sub(_escape, value)


def redact(line: Mapping[str, Any]) -> dict[str, Any]:
    """`line` with the access token removed and control characters escaped."""
    return {key: _clean(key, value) for key, value in line.items()}
