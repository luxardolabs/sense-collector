import hashlib
import os
import re
from pathlib import Path
from typing import Any

from app.core import config
from app.utils.logging import logger

# Characters a device ID keeps as they are in a filename; all others are percent-encoded.
_KEEP = re.compile(r"[A-Za-z0-9_-]")


def private_file(path: str, flags: int) -> int:
    """An `opener` that leaves an export file readable by its owner only (0600): exports
    hold per-second energy data and device names. The mode is set on the open file too,
    because os.open applies it only when it creates the file."""
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return fd


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
    def sanitize_device_id(cls, device_id: Any) -> str | None:
        """
        A device ID as a safe, one-to-one filename part.

        Letters, digits, `_` and `-` are kept; every other character is percent-encoded
        (UTF-8), so distinct IDs never map to one file and nothing can traverse, hide or
        carry a control character. An ID longer than MAX_DEVICE_ID_LENGTH is cut and given
        a hash of the whole ID, so two long IDs sharing a prefix stay distinct. An int is
        used as its string; any other non-string is refused.

        Returns:
            The filename part, or None if the ID cannot be used
        """
        if isinstance(device_id, int) and not isinstance(device_id, bool):
            device_id = str(device_id)
        if not isinstance(device_id, str) or not device_id:
            return None

        encoded = "".join(
            ch
            if _KEEP.fullmatch(ch)
            else "".join(f"%{b:02X}" for b in ch.encode("utf-8", "surrogatepass"))
            for ch in device_id
        )
        if len(encoded) > cls.MAX_DEVICE_ID_LENGTH:
            digest = hashlib.sha256(
                device_id.encode("utf-8", "surrogatepass")
            ).hexdigest()
            # `~` is percent-encoded everywhere else, so a shortened name can never equal
            # the name of an ID that was short enough to keep whole.
            encoded = f"{encoded[: cls.MAX_DEVICE_ID_LENGTH - 9]}~{digest[:8]}"

        if cls.is_safe_device_id(encoded):
            return encoded
        return None

    @classmethod
    def get_safe_export_path(
        cls,
        export_folder: str,
        device_id: Any,
        extension: str = ".json",
        prefix: str = "device_",
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
        except (OSError, ValueError) as e:
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
        # Non-device exports pass prefix="" so no device ID can name the same file.
        filename = f"{prefix}{safe_device_id}{extension}"
        file_path = export_path / filename

        # Verify the resolved path is still within the export folder. Use is_relative_to,
        # not a string startswith (which would let /data/export_evil pass vs /data/export).
        try:
            resolved_path = file_path.resolve()
            if not resolved_path.is_relative_to(export_path):
                logger.error("Path traversal detected: %s", file_path)
                return None
        except (OSError, ValueError) as e:
            logger.error("Error resolving file path: %s", e)
            return None

        return resolved_path
