import re
from pathlib import Path

from app.core import config
from app.utils.logging import logger


class FilePathValidator:
    """Validates file paths to prevent security vulnerabilities."""

    # Patterns that could indicate path traversal attempts
    DANGEROUS_PATTERNS = [
        r"\.\.",  # Parent directory references
        r"^/",  # Absolute paths
        r"^~",  # Home directory expansion
        r"[\x00-\x1f\x7f]",  # Control characters
        r"[<>:|?*]",  # Invalid filename characters on most systems
    ]

    # Maximum reasonable length for a device ID
    MAX_DEVICE_ID_LENGTH = config.FILE_MAX_DEVICE_ID_LENGTH

    @classmethod
    def is_safe_device_id(cls, device_id: str) -> bool:
        """
        Validate that a device ID is safe to use in filenames.

        Args:
            device_id: The device ID to validate

        Returns:
            True if the device ID is safe, False otherwise
        """
        if not device_id:
            return False

        # Check length
        if len(device_id) > cls.MAX_DEVICE_ID_LENGTH:
            logger.warning("Device ID too long: %s characters", len(device_id))
            return False

        # Check for dangerous patterns
        for pattern in cls.DANGEROUS_PATTERNS:
            if re.search(pattern, device_id):
                logger.warning("Dangerous pattern found in device ID: %s", pattern)
                return False

        # Additional checks for common injection attempts
        if device_id.startswith("."):
            logger.warning("Device ID starts with dot")
            return False

        return True

    @classmethod
    def sanitize_device_id(cls, device_id: str) -> str | None:
        """
        Sanitize a device ID for safe use in filenames.

        Args:
            device_id: The device ID to sanitize

        Returns:
            Sanitized device ID or None if it cannot be made safe
        """
        if not device_id:
            return None

        # Remove or replace dangerous characters
        # Replace path separators
        sanitized = device_id.replace("/", "_").replace("\\", "_")

        # Replace other dangerous characters
        sanitized = re.sub(r"[<>:|?*\x00-\x1f\x7f]", "_", sanitized)

        # Neutralize parent-directory references BEFORE stripping leading dots, so
        # ".." becomes "_" (traversal-safe) instead of being removed as leading dots.
        sanitized = sanitized.replace("..", "_")

        # Remove leading dots (hidden-file guard)
        sanitized = sanitized.lstrip(".")

        # Truncate if too long
        if len(sanitized) > cls.MAX_DEVICE_ID_LENGTH:
            sanitized = sanitized[: cls.MAX_DEVICE_ID_LENGTH]

        # Final validation
        if sanitized and cls.is_safe_device_id(sanitized):
            return sanitized

        return None

    @classmethod
    def get_safe_export_path(
        cls, export_folder: str, device_id: str, extension: str = ".json"
    ) -> Path | None:
        """
        Get a safe file path for exporting device data.

        Args:
            export_folder: The base export folder
            device_id: The device ID
            extension: File extension (default: .json)

        Returns:
            Safe Path object or None if path cannot be made safe
        """
        # Validate export folder
        try:
            export_path = Path(export_folder).resolve()
            if not export_path.exists() or not export_path.is_dir():
                logger.error(
                    "Export folder does not exist or is not a directory: %s",
                    export_folder,
                )
                return None
        except Exception as e:
            logger.error("Invalid export folder path: %s", e)
            return None

        # Sanitize device ID
        safe_device_id = cls.sanitize_device_id(device_id)
        if not safe_device_id:
            logger.error("Cannot create safe filename for device ID: %s", device_id)
            return None

        # Ensure extension is safe
        if not extension.startswith("."):
            extension = "." + extension
        if not re.match(r"^\.[a-zA-Z0-9]+$", extension):
            logger.error("Invalid file extension: %s", extension)
            return None

        # Construct filename
        filename = f"device_{safe_device_id}{extension}"
        file_path = export_path / filename

        # Verify the resolved path is still within the export folder. Use is_relative_to,
        # not a string startswith (which would let /data/export_evil pass vs /data/export).
        try:
            resolved_path = file_path.resolve()
            if not resolved_path.is_relative_to(export_path):
                logger.error("Path traversal detected: %s", file_path)
                return None
        except Exception as e:
            logger.error("Error resolving file path: %s", e)
            return None

        return resolved_path
