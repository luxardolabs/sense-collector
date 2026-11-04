from urllib.parse import quote

from app.core import config

# RFC 3986 query characters a value can carry unencoded, minus `&` (the pair separator):
# sub-delims, ":", "@", "/" and "?". quote() always keeps letters, digits and "_.-~".
_QUERY_VALUE_SAFE = "!$'()*+,;=:@/?"


class SenseAPIEndpoints:
    """Centralized API endpoints for Sense API."""

    BASE_URL = config.API_BASE_URL
    AUTHENTICATE = f"{BASE_URL}/authenticate"
    AUTHENTICATE_MFA = f"{BASE_URL}/authenticate/mfa"
    DEVICES = BASE_URL + "/app/monitors/{monitor_id}/devices"
    DEVICE_DETAILS = BASE_URL + "/app/monitors/{monitor_id}/devices/{device_id}"
    MONITOR_STATUS = BASE_URL + "/app/monitors/{monitor_id}/status"
    TIMELINE = BASE_URL + "/users/{user_id}/timeline"
    # WS host comes from config.WS_BASE_URL (SENSE_COLLECTOR_WS_BASE_URL, default
    # wss://clientrt.sense.com) so the dev/demo/e2e stacks can target the emulator.
    WEBSOCKET = (
        config.WS_BASE_URL
        + "/monitors/{monitor_id}/realtimefeed?access_token={access_token}"
    )

    @staticmethod
    def realtime_feed_url(monitor_id: object, access_token: str) -> str:
        """The realtime feed URL, with the token as ONE query value.

        Every character a query value may carry raw is sent as-is, so a token that works
        unencoded is sent byte for byte. Only the characters that would end or corrupt the
        value (`&`, `#`, `%`, whitespace, non-ASCII) are percent-encoded, which also keeps
        the whole token inside what the log redactor scrubs (it stops at a raw `&`).
        """
        return SenseAPIEndpoints.WEBSOCKET.format(
            monitor_id=monitor_id,
            access_token=quote(access_token, safe=_QUERY_VALUE_SAFE),
        )
