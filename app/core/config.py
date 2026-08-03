import logging
import os
from pathlib import Path


class ConfigValidator:
    """Validates configuration values."""

    @staticmethod
    def validate_int(
        value: str,
        min_val: int | None = None,
        max_val: int | None = None,
        default: int | None = None,
    ) -> int:
        """Validate and convert string to integer with bounds checking."""
        try:
            num = int(value)
            if min_val is not None and num < min_val:
                raise ValueError(f"Value {num} is below minimum {min_val}")
            if max_val is not None and num > max_val:
                raise ValueError(f"Value {num} exceeds maximum {max_val}")
            return num
        except (ValueError, TypeError) as e:
            if default is not None:
                logging.warning(
                    "Invalid integer value '%s', using default %s: %s",
                    value,
                    default,
                    e,
                )
                return default
            raise ValueError(f"Invalid integer value: {value}") from e

    @staticmethod
    def validate_float(
        value: str,
        min_val: float | None = None,
        max_val: float | None = None,
        default: float | None = None,
    ) -> float:
        """Validate and convert string to float with bounds checking."""
        try:
            num = float(value)
            if min_val is not None and num < min_val:
                raise ValueError(f"Value {num} is below minimum {min_val}")
            if max_val is not None and num > max_val:
                raise ValueError(f"Value {num} exceeds maximum {max_val}")
            return num
        except (ValueError, TypeError) as e:
            if default is not None:
                logging.warning(
                    "Invalid float value '%s', using default %s: %s", value, default, e
                )
                return default
            raise ValueError(f"Invalid float value: {value}") from e

    @staticmethod
    def validate_bool(value: str, default: bool | None = None) -> bool:
        """Validate and convert string to boolean."""
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        elif value.lower() in ("false", "0", "no", "off"):
            return False
        elif default is not None:
            logging.warning(
                "Invalid boolean value '%s', using default %s", value, default
            )
            return default
        else:
            raise ValueError(f"Invalid boolean value: {value}")

    @staticmethod
    def validate_log_level(value: str, default: str = "INFO") -> str:
        """Validate log level string."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        upper_value = value.upper()
        if upper_value in valid_levels:
            return upper_value
        else:
            logging.warning("Invalid log level '%s', using default %s", value, default)
            return default


def get_env_int(
    key: str, default: int, min_val: int | None = None, max_val: int | None = None
) -> int:
    """Get integer from environment with validation."""
    value = os.getenv(key, str(default))
    return ConfigValidator.validate_int(value, min_val, max_val, default)


def get_env_float(
    key: str, default: float, min_val: float | None = None, max_val: float | None = None
) -> float:
    """Get float from environment with validation."""
    value = os.getenv(key, str(default))
    return ConfigValidator.validate_float(value, min_val, max_val, default)


def get_env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment with validation."""
    value = os.getenv(key, str(default).lower())
    return ConfigValidator.validate_bool(value, default)


def get_env_log_level(key: str, default: str = "INFO") -> str:
    """Get log level from environment with validation."""
    value = os.getenv(key, default)
    return ConfigValidator.validate_log_level(value, default)


# Build Information
BUILD_VERSION = os.getenv("SENSE_COLLECTOR_BUILD_VERSION", "dev")
BUILD_TIMESTAMP = os.getenv("SENSE_COLLECTOR_BUILD_TIMESTAMP", "unknown")

# Credentials + InfluxDB connection (required — presence is validated in
# app.main.validate_environment; describe_settings() masks them in the startup log).
API_USERNAME = os.getenv("SENSE_COLLECTOR_API_USERNAME", "")
API_PASSWORD = os.getenv("SENSE_COLLECTOR_API_PASSWORD", "")
INFLUXDB_URL = os.getenv("SENSE_COLLECTOR_INFLUXDB_URL", "")
INFLUXDB_TOKEN = os.getenv("SENSE_COLLECTOR_INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.getenv("SENSE_COLLECTOR_INFLUXDB_ORG", "")
INFLUXDB_BUCKET = os.getenv("SENSE_COLLECTOR_INFLUXDB_BUCKET", "")

# API URLs and Endpoints
API_BASE_URL = os.getenv(
    "SENSE_COLLECTOR_API_BASE_URL", "https://api.sense.com/apiservice/api/v1"
)
WS_BASE_URL = os.getenv("SENSE_COLLECTOR_WS_BASE_URL", "wss://clientrt.sense.com")

# API Headers and Versions
API_CLIENT_VERSION = os.getenv("SENSE_COLLECTOR_CLIENT_VERSION", "3.0.0")
API_PROTOCOL_VERSION = os.getenv("SENSE_COLLECTOR_PROTOCOL_VERSION", "3")
API_USER_AGENT = os.getenv("SENSE_COLLECTOR_USER_AGENT", "okhttp/3.8.0")
API_USER_AGENT_INTERNAL = os.getenv(
    "SENSE_COLLECTOR_USER_AGENT_INTERNAL", "SenseCollector/3.0"
)

# HTTP Configuration
HTTP_TIMEOUT_TOTAL = get_env_int(
    "SENSE_COLLECTOR_HTTP_TIMEOUT_TOTAL", 60, min_val=10, max_val=300
)
HTTP_TIMEOUT_CONNECT = get_env_int(
    "SENSE_COLLECTOR_HTTP_TIMEOUT_CONNECT", 10, min_val=5, max_val=60
)
HTTP_TIMEOUT_READ = get_env_int(
    "SENSE_COLLECTOR_HTTP_TIMEOUT_READ", 30, min_val=10, max_val=120
)
HTTP_AUTH_TIMEOUT = get_env_int(
    "SENSE_COLLECTOR_HTTP_AUTH_TIMEOUT", 30, min_val=10, max_val=60
)

# Connection Pool Configuration
CONN_POOL_LIMIT = get_env_int(
    "SENSE_COLLECTOR_CONN_POOL_LIMIT", 100, min_val=10, max_val=500
)
CONN_POOL_LIMIT_PER_HOST = get_env_int(
    "SENSE_COLLECTOR_CONN_POOL_LIMIT_PER_HOST", 30, min_val=5, max_val=100
)
CONN_POOL_TTL_DNS = get_env_int(
    "SENSE_COLLECTOR_CONN_POOL_TTL_DNS", 300, min_val=60, max_val=3600
)

# WebSocket Configuration with validation
WS_HEARTBEAT_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_WS_HEARTBEAT_INTERVAL", 10, min_val=1, max_val=300
)
WS_HEARTBEAT_TIMEOUT = get_env_int(
    "SENSE_COLLECTOR_WS_HEARTBEAT_TIMEOUT", 30, min_val=5, max_val=600
)
WS_RECONNECT_DELAY_INITIAL = get_env_int(
    "SENSE_COLLECTOR_WS_RECONNECT_DELAY_INITIAL", 5, min_val=1, max_val=60
)
WS_RECONNECT_DELAY_CAP = get_env_int(
    "SENSE_COLLECTOR_WS_RECONNECT_DELAY_CAP", 60, min_val=10, max_val=3600
)
WS_HEALTH_LOG_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_WS_HEALTH_LOG_INTERVAL", 300, min_val=60, max_val=3600
)
WS_HEALTH_CHECK_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_WS_HEALTH_CHECK_INTERVAL", 1, min_val=1, max_val=10
)

# Queue Configuration
QUEUE_SIZE_API = get_env_int(
    "SENSE_COLLECTOR_QUEUE_SIZE_API", 1000, min_val=100, max_val=10000
)
QUEUE_SIZE_DEVICE = get_env_int(
    "SENSE_COLLECTOR_QUEUE_SIZE_DEVICE", 1000, min_val=100, max_val=10000
)
QUEUE_TIMEOUT = get_env_float(
    "SENSE_COLLECTOR_QUEUE_TIMEOUT", 1.0, min_val=0.1, max_val=10.0
)
QUEUE_BATCH_SIZE = get_env_int(
    "SENSE_COLLECTOR_QUEUE_BATCH_SIZE", 100, min_val=10, max_val=1000
)
QUEUE_BATCH_TIMEOUT = get_env_float(
    "SENSE_COLLECTOR_QUEUE_BATCH_TIMEOUT", 1.0, min_val=0.1, max_val=10.0
)

# Device Configuration with validation
DEVICE_CACHE_EXPIRY_SECONDS = get_env_int(
    "SENSE_COLLECTOR_DEVICE_CACHE_EXPIRY_SECONDS", 900, min_val=300, max_val=3600
)
DEVICE_CACHE_MAX_SIZE = get_env_int(
    "SENSE_COLLECTOR_DEVICE_CACHE_MAX_SIZE", 500, min_val=50, max_val=5000
)
DEVICE_MAX_CONCURRENT_LOOKUPS = get_env_int(
    "SENSE_COLLECTOR_DEVICE_MAX_CONCURRENT_LOOKUPS", 4, min_val=1, max_val=20
)
DEVICE_LOOKUP_DELAY_SECONDS = get_env_float(
    "SENSE_COLLECTOR_DEVICE_LOOKUP_DELAY_SECONDS", 0.5, min_val=0.1, max_val=10.0
)

# API Rate Limiting
API_RATE_LIMIT_CONCURRENT = get_env_int(
    "SENSE_COLLECTOR_API_RATE_LIMIT_CONCURRENT", 10, min_val=1, max_val=50
)
API_MIN_INTERVAL = get_env_float(
    "SENSE_COLLECTOR_API_MIN_INTERVAL", 0.1, min_val=0.01, max_val=5.0
)
API_RETRY_MAX = get_env_int("SENSE_COLLECTOR_API_RETRY_MAX", 3, min_val=1, max_val=10)
API_RETRY_BACKOFF_BASE = get_env_int(
    "SENSE_COLLECTOR_API_RETRY_BACKOFF_BASE", 2, min_val=2, max_val=10
)

# API Configuration with validation
TOKEN_RENEW_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_API_TOKEN_RENEW", 43200, min_val=3600, max_val=86400
)  # 1-24 hours

# Fetch Intervals
MONITOR_STATUS_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_MONITOR_STATUS_INTERVAL", 60, min_val=30, max_val=3600
)
DEVICE_LIST_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_DEVICE_LIST_INTERVAL", 3600, min_val=300, max_val=86400
)

# API Worker Configuration
API_WORKER_COUNT = get_env_int(
    "SENSE_COLLECTOR_API_WORKER_COUNT", 3, min_val=1, max_val=10
)
API_WORKER_ERROR_SLEEP = get_env_int(
    "SENSE_COLLECTOR_API_WORKER_ERROR_SLEEP", 1, min_val=1, max_val=60
)
MONITOR_STATUS_CHECK_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_MONITOR_STATUS_CHECK_INTERVAL", 10, min_val=5, max_val=60
)
DEVICE_LIST_CHECK_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_DEVICE_LIST_CHECK_INTERVAL", 30, min_val=10, max_val=300
)

# InfluxDB Configuration
INFLUXDB_TIMEOUT = get_env_int(
    "SENSE_COLLECTOR_INFLUXDB_TIMEOUT", 30000, min_val=5000, max_val=120000
)  # ms
INFLUXDB_ENABLE_GZIP = get_env_bool("SENSE_COLLECTOR_INFLUXDB_ENABLE_GZIP", True)
# No batch_size/flush_interval/retry knobs: the fleet standard uses the asyncio-native
# client and writes one batch per poll cycle (each write awaited inline; a failed cycle
# just retries on the next one).

# InfluxDB Measurement Names (customizable)
MEASUREMENT_MAINS = os.getenv("SENSE_COLLECTOR_MEASUREMENT_MAINS", "sense_mains")
MEASUREMENT_O11Y = os.getenv("SENSE_COLLECTOR_MEASUREMENT_O11Y", "sense_o11y")
MEASUREMENT_DEVICES = os.getenv("SENSE_COLLECTOR_MEASUREMENT_DEVICES", "sense_devices")
MEASUREMENT_ALWAYS_ON = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON", "sense_always_on"
)
MEASUREMENT_ALWAYS_ON_COMPARISON = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON_COMPARISON", "sense_always_on_comparison"
)
MEASUREMENT_ALWAYS_ON_DEVICES = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_ALWAYS_ON_DEVICES", "sense_always_on_devices"
)
MEASUREMENT_EVENT = os.getenv("SENSE_COLLECTOR_MEASUREMENT_EVENT", "sense_event")
MEASUREMENT_HELLO = os.getenv("SENSE_COLLECTOR_MEASUREMENT_HELLO", "hello_event")
MEASUREMENT_DATA_CHANGE = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_DATA_CHANGE", "data_change_event"
)
MEASUREMENT_DEVICE_STATE = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_DEVICE_STATE", "device_state_event"
)
MEASUREMENT_MONITOR_STATUS = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_MONITOR_STATUS", "sense_monitor_status"
)
MEASUREMENT_DEVICE_DETECTION = os.getenv(
    "SENSE_COLLECTOR_MEASUREMENT_DEVICE_DETECTION", "sense_device_detection"
)

# Data Processing Configuration
MAX_CHANNELS = get_env_int("SENSE_COLLECTOR_MAX_CHANNELS", 2, min_val=1, max_val=10)
YEARLY_COST_DIVISOR = get_env_int(
    "SENSE_COLLECTOR_YEARLY_COST_DIVISOR", 100, min_val=1, max_val=1000
)
DEFAULT_VOLTAGE = get_env_float(
    "SENSE_COLLECTOR_DEFAULT_VOLTAGE", 0.0, min_val=0.0, max_val=240.0
)
DEFAULT_WIFI_STRENGTH = get_env_float(
    "SENSE_COLLECTOR_DEFAULT_WIFI_STRENGTH", 0.0, min_val=-100.0, max_val=0.0
)

# Logging Configuration with validation
LOG_LEVEL_API = get_env_log_level("SENSE_COLLECTOR_LOG_LEVEL_API", "INFO")
LOG_LEVEL_STORAGE = get_env_log_level("SENSE_COLLECTOR_LOG_LEVEL_STORAGE", "INFO")
LOG_LEVEL_GENERAL = get_env_log_level("SENSE_COLLECTOR_LOG_LEVEL_GENERAL", "INFO")
LOG_STRUCTURED = get_env_bool("SENSE_COLLECTOR_STRUCTURED_LOGS", False)
LOG_DIR = os.getenv("SENSE_COLLECTOR_LOG_DIR", "")  # Empty means console only
LOG_FILE_MAX_BYTES = get_env_int(
    "SENSE_COLLECTOR_LOG_FILE_MAX_BYTES", 10485760, min_val=1048576, max_val=104857600
)  # 1MB-100MB
LOG_FILE_BACKUP_COUNT = get_env_int(
    "SENSE_COLLECTOR_LOG_FILE_BACKUP_COUNT", 5, min_val=1, max_val=20
)

# Export Configuration with validation
OUTPUT_RECEIVED_DATA = get_env_bool("SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA", False)
EXPORT_FOLDER = os.getenv("SENSE_COLLECTOR_EXPORT_FOLDER", "output")

# File naming patterns
EXPORT_FILE_RECEIVED_DATA = os.getenv(
    "SENSE_COLLECTOR_EXPORT_FILE_RECEIVED_DATA", "received_data"
)
EXPORT_FILE_DEVICE_PREFIX = os.getenv(
    "SENSE_COLLECTOR_EXPORT_FILE_DEVICE_PREFIX", "device_"
)
EXPORT_FILE_EXTENSION = os.getenv("SENSE_COLLECTOR_EXPORT_FILE_EXTENSION", ".json")

# File Path Security
FILE_MAX_DEVICE_ID_LENGTH = get_env_int(
    "SENSE_COLLECTOR_FILE_MAX_DEVICE_ID_LENGTH", 100, min_val=20, max_val=255
)

# Validate export folder
try:
    export_path = Path(EXPORT_FOLDER)
    export_path.mkdir(parents=True, exist_ok=True)

    # Test write permissions
    test_file = export_path / ".write_test"
    try:
        test_file.touch()
        test_file.unlink()
    except Exception as e:
        raise PermissionError(
            f"Export folder {EXPORT_FOLDER} is not writable: {e}"
        ) from e

except Exception as e:
    logging.exception("Failed to create or validate export folder: %s", e)
    # Use temp directory as fallback
    import tempfile

    EXPORT_FOLDER = tempfile.mkdtemp(prefix="sense_collector_")
    logging.warning("Using temporary export folder: %s", EXPORT_FOLDER)

# Health heartbeat — the collector touches this file (throttled) as WebSocket data flows;
# the Docker healthcheck (app/health/check.py) asserts it stays fresh. Defined after
# EXPORT_FOLDER is finalized so the default path lands in the real (or fallback) output dir.
HEALTH_HEARTBEAT_FILE = os.getenv(
    "SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE", os.path.join(EXPORT_FOLDER, ".heartbeat")
)
HEALTH_HEARTBEAT_INTERVAL = get_env_int(
    "SENSE_COLLECTOR_HEALTH_HEARTBEAT_INTERVAL", 15, min_val=1, max_val=300
)

# Required environment variables
REQUIRED_ENV_VARS = [
    "SENSE_COLLECTOR_API_USERNAME",
    "SENSE_COLLECTOR_API_PASSWORD",
    "SENSE_COLLECTOR_INFLUXDB_URL",
    "SENSE_COLLECTOR_INFLUXDB_TOKEN",
    "SENSE_COLLECTOR_INFLUXDB_ORG",
    "SENSE_COLLECTOR_INFLUXDB_BUCKET",
]


def describe_settings() -> dict[str, str]:
    """Effective config keyed by env-var name — the single source of truth for the startup
    log (app.main logs this, masking PASSWORD/TOKEN/USERNAME). No hand-kept list that drifts.
    """
    return {
        "SENSE_COLLECTOR_API_USERNAME": API_USERNAME,
        "SENSE_COLLECTOR_API_PASSWORD": API_PASSWORD,
        "SENSE_COLLECTOR_INFLUXDB_URL": INFLUXDB_URL,
        "SENSE_COLLECTOR_INFLUXDB_TOKEN": INFLUXDB_TOKEN,
        "SENSE_COLLECTOR_INFLUXDB_ORG": INFLUXDB_ORG,
        "SENSE_COLLECTOR_INFLUXDB_BUCKET": INFLUXDB_BUCKET,
        "SENSE_COLLECTOR_API_BASE_URL": API_BASE_URL,
        "SENSE_COLLECTOR_WS_BASE_URL": WS_BASE_URL,
        "SENSE_COLLECTOR_CLIENT_VERSION": API_CLIENT_VERSION,
        "SENSE_COLLECTOR_WS_HEARTBEAT_INTERVAL": str(WS_HEARTBEAT_INTERVAL),
        "SENSE_COLLECTOR_WS_HEARTBEAT_TIMEOUT": str(WS_HEARTBEAT_TIMEOUT),
        "SENSE_COLLECTOR_DEVICE_CACHE_EXPIRY_SECONDS": str(DEVICE_CACHE_EXPIRY_SECONDS),
        "SENSE_COLLECTOR_DEVICE_MAX_CONCURRENT_LOOKUPS": str(
            DEVICE_MAX_CONCURRENT_LOOKUPS
        ),
        "SENSE_COLLECTOR_API_TOKEN_RENEW": str(TOKEN_RENEW_INTERVAL),
        "SENSE_COLLECTOR_MONITOR_STATUS_INTERVAL": str(MONITOR_STATUS_INTERVAL),
        "SENSE_COLLECTOR_DEVICE_LIST_INTERVAL": str(DEVICE_LIST_INTERVAL),
        "SENSE_COLLECTOR_INFLUXDB_TIMEOUT": str(INFLUXDB_TIMEOUT),
        "SENSE_COLLECTOR_INFLUXDB_ENABLE_GZIP": str(INFLUXDB_ENABLE_GZIP),
        "SENSE_COLLECTOR_EXPORT_FOLDER": EXPORT_FOLDER,
        "SENSE_COLLECTOR_OUTPUT_RECEIVED_DATA": str(OUTPUT_RECEIVED_DATA),
        "SENSE_COLLECTOR_LOG_LEVEL_API": LOG_LEVEL_API,
        "SENSE_COLLECTOR_LOG_LEVEL_STORAGE": LOG_LEVEL_STORAGE,
        "SENSE_COLLECTOR_LOG_LEVEL_GENERAL": LOG_LEVEL_GENERAL,
        "SENSE_COLLECTOR_STRUCTURED_LOGS": str(LOG_STRUCTURED),
        "SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE": HEALTH_HEARTBEAT_FILE,
    }
