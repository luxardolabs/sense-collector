from datetime import UTC, datetime


def convert_to_epoch(timestamp_str: str) -> int | None:
    timestamp_format = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        # The trailing 'Z' is matched literally, not parsed, so the result is naive.
        # Stamp it as UTC explicitly — otherwise .timestamp() would treat it as local time
        # and be off by the container's UTC offset (TZ is usually not UTC).
        datetime_obj = datetime.strptime(timestamp_str, timestamp_format).replace(
            tzinfo=UTC
        )
        return int(datetime_obj.timestamp())
    except ValueError:
        return None
