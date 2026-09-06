"""Tests for the Docker healthcheck and the epoch-conversion helper.

The healthcheck is the only thing that notices a collector that is running but no longer
collecting, so its freshness arithmetic and its exit codes are load-bearing.
"""

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.utils.time import convert_to_epoch


class TestConvertToEpoch:
    def test_parses_a_sense_timestamp_as_utc(self):
        """The trailing Z is matched literally, so the value must be stamped UTC.

        Without the explicit tzinfo this would be read as container-local and land off by
        the host's UTC offset.
        """
        assert convert_to_epoch("2026-01-01T00:00:00.000Z") == 1767225600

    def test_is_independent_of_the_container_timezone(self):
        first = convert_to_epoch("2026-06-15T12:30:45.123Z")
        assert first == int(datetime(2026, 6, 15, 12, 30, 45, tzinfo=UTC).timestamp())

    @pytest.mark.parametrize(
        "bad",
        ["", "not-a-timestamp", "2026-01-01", "2026-01-01T00:00:00Z"],
        ids=["empty", "garbage", "date-only", "no-microseconds"],
    )
    def test_an_unparseable_value_is_none_not_an_exception(self, bad):
        assert convert_to_epoch(bad) is None


def _reload_check(**env):
    """Re-import the healthcheck so its module-level env constants are re-read."""
    import importlib

    import app.health.check as check

    with patch.dict(os.environ, env, clear=False):
        return importlib.reload(check)


class TestCheckHeartbeat:
    def test_missing_file_is_unhealthy(self, tmp_path):
        check = _reload_check(
            SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(tmp_path / "absent")
        )
        healthy, message = check.check_heartbeat()
        assert healthy is False
        assert "no heartbeat file" in message

    def test_a_fresh_heartbeat_is_healthy(self, tmp_path):
        hb = tmp_path / ".heartbeat"
        hb.touch()
        check = _reload_check(SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(hb))
        healthy, message = check.check_heartbeat()
        assert healthy is True
        assert "healthy" in message

    def test_a_stale_heartbeat_is_unhealthy(self, tmp_path):
        """The whole point: a running-but-not-collecting container must report unhealthy."""
        hb = tmp_path / ".heartbeat"
        hb.touch()
        old = time.time() - 600
        os.utime(hb, (old, old))
        check = _reload_check(
            SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(hb),
            SENSE_COLLECTOR_HEALTH_CHECK_MAX_AGE="120",
        )
        healthy, message = check.check_heartbeat()
        assert healthy is False
        assert "old" in message

    def test_age_is_computed_from_an_aware_utc_instant(self, tmp_path):
        """A heartbeat written 'now' must read as ~0s old regardless of the container TZ.

        Subtracting two naive local readings would silently shift by an hour across a DST
        boundary and could report a fresh heartbeat as stale.
        """
        hb = tmp_path / ".heartbeat"
        hb.touch()
        check = _reload_check(SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(hb))
        healthy, message = check.check_heartbeat()
        assert healthy is True
        age = float(message.split("heartbeat ")[1].split("s ago")[0])
        assert age < 5


class TestHealthCheckMain:
    def test_exits_zero_when_healthy(self, tmp_path, capsys):
        hb = tmp_path / ".heartbeat"
        hb.touch()
        check = _reload_check(SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(hb))
        with pytest.raises(SystemExit) as exc:
            check.main()
        assert exc.value.code == 0
        # stdout IS the healthcheck's interface — Docker surfaces it in Health.Log
        assert "HEALTHY" in capsys.readouterr().out

    def test_exits_one_and_explains_when_unhealthy(self, tmp_path, capsys):
        check = _reload_check(
            SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(tmp_path / "absent")
        )
        with pytest.raises(SystemExit) as exc:
            check.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "UNHEALTHY" in out
        assert "no heartbeat file" in out

    def test_reports_build_provenance(self, tmp_path, capsys):
        """Version/build are read at CALL time, so the env must be live during main()."""
        hb = tmp_path / ".heartbeat"
        hb.touch()
        check = _reload_check(SENSE_COLLECTOR_HEALTH_HEARTBEAT_FILE=str(hb))
        with (
            patch.dict(
                os.environ,
                {
                    "SENSE_COLLECTOR_VERSION": "2026.08.0",
                    "SENSE_COLLECTOR_BUILD_TIMESTAMP": "2026-08-01T00:00:00Z",
                },
            ),
            pytest.raises(SystemExit),
        ):
            check.main()
        out = capsys.readouterr().out
        assert "2026.08.0" in out
        assert "2026-08-01T00:00:00Z" in out


def test_heartbeat_path_defaults_under_the_export_folder(tmp_path):
    check = _reload_check(SENSE_COLLECTOR_EXPORT_FOLDER=str(tmp_path))
    assert Path(check.HEARTBEAT_FILE) == tmp_path / ".heartbeat"
