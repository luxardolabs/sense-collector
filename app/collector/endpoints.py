from app.core import config


class SenseAPIEndpoints:
    """Centralized API endpoints for Sense API."""

    BASE_URL = config.API_BASE_URL
    AUTHENTICATE = f"{BASE_URL}/authenticate"
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
