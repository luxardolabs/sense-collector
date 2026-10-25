import os
import sys
import tempfile

import pytest

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
            assert FilePathValidator.is_safe_device_id(device_id), (
                f"Should accept: {device_id}"
            )

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
            assert not FilePathValidator.is_safe_device_id(device_id), (
                f"Should reject: {device_id}"
            )

    def test_sanitize_device_id(self):
        """Unsafe characters are percent-encoded, so the name is safe AND one-to-one."""
        test_cases = [
            ("normal_device", "normal_device"),
            ("../dangerous", "%2E%2E%2Fdangerous"),
            ("device/with/slashes", "device%2Fwith%2Fslashes"),
            ("device\\with\\backslashes", "device%5Cwith%5Cbackslashes"),
            ("device<>:|?*", "device%3C%3E%3A%7C%3F%2A"),
            (".hidden_device", "%2Ehidden_device"),
            ("device\x00null\x1f", "device%00null%1F"),
        ]

        for input_id, expected in test_cases:
            result = FilePathValidator.sanitize_device_id(input_id)
            assert result == expected, (
                f"Sanitize '{input_id}' -> expected '{expected}', got '{result}'"
            )

    def test_distinct_device_ids_never_share_a_file(self):
        """a/b, a..b and a_b all became a_b, so one device's export overwrote another's;
        two long ids sharing a 100-character prefix collided the same way."""
        ids = ["a/b", "a..b", "a_b", "a" * 150, "a" * 149 + "b"]
        names = [FilePathValidator.sanitize_device_id(i) for i in ids]
        assert len(set(names)) == len(ids), names
        assert all(len(n) <= FilePathValidator.MAX_DEVICE_ID_LENGTH for n in names)

    def test_a_numeric_device_id_is_used_as_its_string(self):
        """An int id raised AttributeError past the export writers' narrowed catch."""
        assert FilePathValidator.sanitize_device_id(12345) == "12345"
        assert FilePathValidator.sanitize_device_id({"id": 1}) is None

    def test_no_device_id_can_overwrite_the_device_list_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # The list export (devices.json) and received data sit outside the device_
            # namespace every device file has, whatever the device ID.
            listing = FilePathValidator.get_safe_export_path(
                temp_dir, "devices", prefix=""
            )
            assert listing is not None and listing.name == "devices.json"
            for device_id in ("s", "list", "devices", "_list"):
                device = FilePathValidator.get_safe_export_path(temp_dir, device_id)
                assert device is not None and device.name.startswith("device_")
                assert device != listing

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
            path = FilePathValidator.get_safe_export_path(
                "/nonexistent/folder", "device123"
            )
            assert path is None

            # Custom extension
            path = FilePathValidator.get_safe_export_path(temp_dir, "device123", ".txt")
            assert path is not None
            assert path.suffix == ".txt"

            # Invalid extension
            path = FilePathValidator.get_safe_export_path(
                temp_dir, "device123", "../../bad"
            )
            assert path is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
