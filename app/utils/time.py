from datetime import datetime


def convert_to_epoch(timestamp_str: str) -> int | None:
    """A Sense ISO-8601 timestamp as whole epoch seconds, or None when it is not one.

    Sense writes UTC as a trailing `Z`, with or without a fraction of a second
    (`2026-01-01T00:00:00Z`, `2026-01-01T00:00:00.123Z`); an explicit offset reads too.
    A value with no date and time, or with no zone, names no single instant and is None:
    reading it as the container's local time would be off by the host's UTC offset.
    """
    try:
        parsed = datetime.fromisoformat(timestamp_str)
    except ValueError:
        return None
    if "T" not in timestamp_str or parsed.tzinfo is None:
        return None
    return int(parsed.timestamp())
