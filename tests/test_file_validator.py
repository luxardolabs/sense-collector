import pytest
import tempfile
import sys
import os

# Add the src/app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "app"))

from app.utils.file_validator import FilePathValidator


class TestFilePathValidator:
    """Test file path validation."""

    def test_safe_device_ids(self):
        """Test that valid device IDs are accepted."""
        valid_ids = [
            "device123",
            "sensor_001",
            "ABCD-1234-EFGH",
            "device.name.123",
            "device-with-dashes",
            "123456789",
        ]

        for device_id in valid_ids:
            assert FilePathValidator.is_safe_device_id(device_id), f"Should accept: {device_id}"

    def test_unsafe_device_ids(self):
        """Test that dangerous device IDs are rejected."""
        unsafe_ids = [
            "../etc/passwd",
            "../../secret",
            "/etc/passwd",
            "~/sensitive",
            "device\x00null",
            "device<script>",
            "device|pipe",
            "device?query",
            "device*wildcard",
            ".hidden",
            "a" * 101,  # Too long
            "",  # Empty
        ]

        for device_id in unsafe_ids:
            assert not FilePathValidator.is_safe_device_id(device_id), f"Should reject: {device_id}"

    def test_sanitize_device_id(self):
        """Test device ID sanitization."""
        test_cases = [
            ("normal_device", "normal_device"),
            ("../dangerous", "__dangerous"),
            ("device/with/slashes", "device_with_slashes"),
            ("device\\with\\backslashes", "device_with_backslashes"),
            ("device<>:|?*", "device______"),
            (".hidden_device", "hidden_device"),
            ("device\x00null\x1f", "device_null_"),
            ("a" * 150, "a" * 100),  # Truncate long IDs
        ]

        for input_id, expected in test_cases:
            result = FilePathValidator.sanitize_device_id(input_id)
            assert result == expected, (
                f"Sanitize '{input_id}' -> expected '{expected}', got '{result}'"
            )

    def test_get_safe_export_path(self):
        """Test safe export path generation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Valid case
            path = FilePathValidator.get_safe_export_path(temp_dir, "device123")
            assert path is not None
            assert str(path).startswith(temp_dir)
            assert path.name == "device_device123.json"

            # Path traversal attempt
            path = FilePathValidator.get_safe_export_path(temp_dir, "../etc/passwd")
            assert path is not None
            assert str(path).startswith(temp_dir)
            assert "etc" not in str(path.parent)

            # Invalid export folder
            path = FilePathValidator.get_safe_export_path("/nonexistent/folder", "device123")
            assert path is None

            # Custom extension
            path = FilePathValidator.get_safe_export_path(temp_dir, "device123", ".txt")
            assert path is not None
            assert path.suffix == ".txt"

            # Invalid extension
            path = FilePathValidator.get_safe_export_path(temp_dir, "device123", "../../bad")
            assert path is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
