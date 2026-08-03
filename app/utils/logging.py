import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

# Import config after it's been initialized
from app.core import config


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in [
                "name",
                "msg",
                "args",
                "created",
                "msecs",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "getMessage",
            ]:
                log_data[key] = value

        return json.dumps(log_data)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _neutralize(text: str) -> str:
    """Escape control characters (newlines, ANSI ESC, etc.) as \\xNN.

    Remote-derived fields — notably Sense device names, which are user-editable in the app —
    get f-string'd into console log messages. Without this, a name containing a newline could
    forge a fake log line, or ANSI escapes could manipulate a terminal viewing `docker logs`.
    """
    return _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class ColoredFormatter(logging.Formatter):
    """Colored formatter for console output."""

    COLORS = {
        "DEBUG": "\033[36m",  # Cyan
        "INFO": "\033[32m",  # Green
        "WARNING": "\033[33m",  # Yellow
        "ERROR": "\033[31m",  # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        """Format with colors for console, neutralizing control chars in the message."""
        # Mutate then restore, so other handlers sharing this record (e.g. the JSON file
        # handler) see the pristine record.
        original = (record.msg, record.args, record.levelname)
        record.msg = _neutralize(record.getMessage())
        record.args = None
        if record.levelname in self.COLORS:
            record.levelname = (
                f"{self.COLORS[record.levelname]}{record.levelname}{self.RESET}"
            )
        try:
            return super().format(record)
        finally:
            record.msg, record.args, record.levelname = original


def setup_logger(
    name: str, level: str = "INFO", structured: bool = False
) -> logging.Logger:
    """Set up a logger with rotation and proper formatting."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    # Clear existing handlers
    logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    if structured or config.LOG_STRUCTURED:
        console_formatter: logging.Formatter = StructuredFormatter()
    else:
        # Use colored formatter for console if not structured
        console_format = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
        console_formatter = ColoredFormatter(console_format)

    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler with rotation (if log directory is specified)
    log_dir = config.LOG_DIR
    if log_dir:
        try:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)

            # Create rotating file handler
            log_file = log_path / f"{name}.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=config.LOG_FILE_MAX_BYTES,
                backupCount=config.LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setLevel(level)

            # Always use structured format for files
            file_formatter = StructuredFormatter()
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

        except Exception as e:
            # Log to console if file logging fails
            console_handler.setLevel(logging.WARNING)
            logger.warning("Failed to set up file logging: %s", e)

    return logger


# Create loggers with proper configuration
logger = setup_logger("general", config.LOG_LEVEL_GENERAL)
api_logger = setup_logger("api", config.LOG_LEVEL_API)
storage_logger = setup_logger("storage", config.LOG_LEVEL_STORAGE)


# Context manager for adding extra fields to logs
class LogContext:
    """Context manager for adding extra fields to log records."""

    def __init__(self, logger: logging.Logger, **kwargs: Any) -> None:
        self.logger = logger
        self.extras = kwargs
        self.old_factory: Callable[..., logging.LogRecord] | None = None

    def __enter__(self) -> LogContext:
        self.old_factory = logging.getLogRecordFactory()
        extras = self.extras

        def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            if self.old_factory:
                record = self.old_factory(*args, **kwargs)
            else:
                record = logging.LogRecord(*args, **kwargs)
            for key, value in extras.items():
                setattr(record, key, value)
            return record

        logging.setLogRecordFactory(record_factory)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self.old_factory:
            logging.setLogRecordFactory(self.old_factory)


# Helper function for logging with context
def log_with_context(
    logger: logging.Logger, level: int, message: str, **context: Any
) -> None:
    """Log a message with additional context fields."""
    with LogContext(logger, **context):
        logger.log(level, message)
