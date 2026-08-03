import pytest
import os
import tempfile
from unittest.mock import patch
import sys

# Add the src/app directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "app"))

from app.core.config import (
    ConfigValidator,
    get_env_int,
    get_env_float,
    get_env_bool,
    get_env_log_level,
)


class TestConfigValidator:
    """Test configuration validation."""

    def test_validate_int_valid(self):
        """Test valid integer validation."""
        assert ConfigValidator.validate_int("42") == 42
        assert ConfigValidator.validate_int("0") == 0
        assert ConfigValidator.validate_int("-10") == -10

        # With bounds
        assert ConfigValidator.validate_int("50", min_val=0, max_val=100) == 50
        assert ConfigValidator.validate_int("0", min_val=0, max_val=100) == 0
        assert ConfigValidator.validate_int("100", min_val=0, max_val=100) == 100

    def test_validate_int_invalid(self):
        """Test invalid integer validation."""
        # Invalid format
        with pytest.raises(ValueError):
            ConfigValidator.validate_int("not_a_number")

        # Below minimum
        with pytest.raises(ValueError):
            ConfigValidator.validate_int("-5", min_val=0)

        # Above maximum
        with pytest.raises(ValueError):
            ConfigValidator.validate_int("150", max_val=100)

        # With default
        assert ConfigValidator.validate_int("invalid", default=42) == 42

    def test_validate_float_valid(self):
        """Test valid float validation."""
        assert ConfigValidator.validate_float("3.14") == 3.14
        assert ConfigValidator.validate_float("0.0") == 0.0
        assert ConfigValidator.validate_float("-2.5") == -2.5

        # With bounds
        assert ConfigValidator.validate_float("0.5", min_val=0.0, max_val=1.0) == 0.5

    def test_validate_float_invalid(self):
        """Test invalid float validation."""
        # Invalid format
        with pytest.raises(ValueError):
            ConfigValidator.validate_float("not_a_float")

        # Out of bounds
        with pytest.raises(ValueError):
            ConfigValidator.validate_float("1.5", max_val=1.0)

        # With default
        assert ConfigValidator.validate_float("invalid", default=3.14) == 3.14

    def test_validate_bool_valid(self):
        """Test valid boolean validation."""
        # True values
        assert ConfigValidator.validate_bool("true") is True
        assert ConfigValidator.validate_bool("True") is True
        assert ConfigValidator.validate_bool("TRUE") is True
        assert ConfigValidator.validate_bool("1") is True
        assert ConfigValidator.validate_bool("yes") is True
        assert ConfigValidator.validate_bool("on") is True

        # False values
        assert ConfigValidator.validate_bool("false") is False
        assert ConfigValidator.validate_bool("False") is False
        assert ConfigValidator.validate_bool("FALSE") is False
        assert ConfigValidator.validate_bool("0") is False
        assert ConfigValidator.validate_bool("no") is False
        assert ConfigValidator.validate_bool("off") is False

    def test_validate_bool_invalid(self):
        """Test invalid boolean validation."""
        # Invalid without default
        with pytest.raises(ValueError):
            ConfigValidator.validate_bool("maybe")

        # With default
        assert ConfigValidator.validate_bool("invalid", default=True) is True
        assert ConfigValidator.validate_bool("invalid", default=False) is False

    def test_validate_log_level(self):
        """Test log level validation."""
        # Valid levels
        assert ConfigValidator.validate_log_level("DEBUG") == "DEBUG"
        assert ConfigValidator.validate_log_level("info") == "INFO"
        assert ConfigValidator.validate_log_level("Warning") == "WARNING"
        assert ConfigValidator.validate_log_level("ERROR") == "ERROR"
        assert ConfigValidator.validate_log_level("critical") == "CRITICAL"

        # Invalid level with default
        assert ConfigValidator.validate_log_level("TRACE", default="INFO") == "INFO"
        assert ConfigValidator.validate_log_level("", default="DEBUG") == "DEBUG"


class TestEnvironmentHelpers:
    """Test environment variable helper functions."""

    def test_get_env_int(self):
        """Test getting integer from environment."""
        with patch.dict(os.environ, {"TEST_INT": "42"}):
            assert get_env_int("TEST_INT", 10) == 42

        # Missing variable uses default
        assert get_env_int("MISSING_INT", 10) == 10

        # With validation
        with patch.dict(os.environ, {"TEST_INT": "50"}):
            assert get_env_int("TEST_INT", 10, min_val=0, max_val=100) == 50

        # Invalid value uses default
        with patch.dict(os.environ, {"TEST_INT": "invalid"}):
            assert get_env_int("TEST_INT", 10) == 10

    def test_get_env_float(self):
        """Test getting float from environment."""
        with patch.dict(os.environ, {"TEST_FLOAT": "3.14"}):
            assert get_env_float("TEST_FLOAT", 1.0) == 3.14

        # Missing variable uses default
        assert get_env_float("MISSING_FLOAT", 2.5) == 2.5

        # With validation
        with patch.dict(os.environ, {"TEST_FLOAT": "0.75"}):
            assert get_env_float("TEST_FLOAT", 0.5, min_val=0.0, max_val=1.0) == 0.75

    def test_get_env_bool(self):
        """Test getting boolean from environment."""
        with patch.dict(os.environ, {"TEST_BOOL": "true"}):
            assert get_env_bool("TEST_BOOL", False) is True

        with patch.dict(os.environ, {"TEST_BOOL": "false"}):
            assert get_env_bool("TEST_BOOL", True) is False

        # Missing variable uses default
        assert get_env_bool("MISSING_BOOL", True) is True

    def test_get_env_log_level(self):
        """Test getting log level from environment."""
        with patch.dict(os.environ, {"TEST_LOG": "DEBUG"}):
            assert get_env_log_level("TEST_LOG") == "DEBUG"

        # Case insensitive
        with patch.dict(os.environ, {"TEST_LOG": "warning"}):
            assert get_env_log_level("TEST_LOG") == "WARNING"

        # Invalid uses default
        with patch.dict(os.environ, {"TEST_LOG": "TRACE"}):
            assert get_env_log_level("TEST_LOG", "INFO") == "INFO"


class TestConfigModule:
    """Test the config module initialization."""

    def test_export_folder_creation(self):
        """Test export folder is created if it doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            test_export = os.path.join(temp_dir, "test_export")

            with patch.dict(os.environ, {"SENSE_COLLECTOR_EXPORT_FOLDER": test_export}):
                # Re-import config to trigger folder creation
                import importlib
                import app.core.config as config

                importlib.reload(config)

                # Check folder was created
                assert os.path.exists(test_export)
                assert os.path.isdir(test_export)

    def test_export_folder_fallback(self):
        """Test fallback to temp directory on permission error."""
        # Try to use a read-only directory
        with patch.dict(os.environ, {"SENSE_COLLECTOR_EXPORT_FOLDER": "/root/no_permission"}):
            import importlib
            import app.core.config as config

            # This should not raise an exception
            importlib.reload(config)

            # Should fall back to temp directory
            assert config.EXPORT_FOLDER.startswith("/tmp") or config.EXPORT_FOLDER.startswith(
                "/var/folders"
            )
            assert "sense_collector_" in config.EXPORT_FOLDER


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
