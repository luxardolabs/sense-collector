"""Tests for the entrypoint's startup guards.

Two things here are load-bearing and cheap to get wrong: the masking applied to secrets
before they reach the startup log, and the fail-fast on missing required config. The
orchestration (task endings, client lifecycle) is tested here against stand-ins; the
real wiring end to end is the e2e stack.
"""

import asyncio
import logging
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.core import config
from app.main import (
    _SENSITIVE,
    _obscure,
    create_collector,
    main,
    run_collector_tasks,
    validate_environment,
)


class TestObscure:
    """`_obscure` is the last thing standing between a credential and the log file."""

    def test_keeps_only_the_first_and_last_two_characters(self):
        assert _obscure("supersecret") == "su*******et"

    def test_masked_length_matches_the_original(self):
        """The mask must not leak length information by being a fixed width... but it
        also must not change length, or the log becomes misleading about the value."""
        secret = "abcdefghijklmnop"
        assert len(_obscure(secret)) == len(secret)

    @pytest.mark.parametrize("value", ["a", "ab", "abc", "abcd"])
    def test_short_values_are_fully_redacted(self, value):
        """<=4 chars: partial masking would reveal the whole thing, so redact entirely."""
        masked = _obscure(value)
        assert masked == "*" * len(value)
        assert not set(masked) & set(value)

    def test_five_characters_is_the_first_partial_mask(self):
        assert _obscure("abcde") == "ab*de"

    def test_empty_stays_empty(self):
        assert _obscure("") == ""

    def test_never_returns_the_input_verbatim_for_a_real_secret(self):
        for secret in ("hunter2", "a-very-long-influx-token-value", "pw12"):
            assert _obscure(secret) != secret


class TestValidateEnvironment:
    @pytest.fixture(autouse=True)
    def _capture_the_startup_log(self, caplog):
        """The startup log is INFO on the `general` logger, whose level comes from
        configure_logging in production; capture it here, or a masking assertion that
        checks for an ABSENT secret would pass on an empty log."""
        caplog.set_level(logging.INFO, logger="general")

    def test_passes_when_every_required_var_is_present(self):
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with patch.dict(os.environ, env, clear=False):
            validate_environment()

    @pytest.mark.parametrize("missing", config.REQUIRED_ENV_VARS)
    def test_fails_fast_when_any_required_var_is_missing(self, missing):
        """Starting without config would fail later, mid-stream, and less legibly."""
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        del env[missing]
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="Missing required"),
        ):
            validate_environment()

    def test_an_empty_string_counts_as_missing(self):
        """An unset var and an empty one are the same failure — neither can authenticate."""
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        env["SENSE_COLLECTOR_API_PASSWORD"] = ""
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ValueError, match="Missing required"),
        ):
            validate_environment()

    def test_the_error_names_every_missing_var(self, caplog):
        with patch.dict(os.environ, {}, clear=True), pytest.raises(ValueError):
            validate_environment()
        logged = caplog.text
        for var in config.REQUIRED_ENV_VARS:
            assert var in logged

    def test_secrets_are_masked_in_the_startup_log(self, caplog):
        """describe_settings() feeds the startup log — no credential may appear verbatim.

        The stand-in values are assembled at runtime rather than written as literals: a
        realistic-looking token literal trips the repo's gitleaks pre-commit hook, and a
        test fixture is not worth teaching the secret scanner to ignore.
        """
        fake_password = "placeholder-" + "pw" * 6
        fake_token = "placeholder-" + "tok" * 6
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(
                config,
                "describe_settings",
                return_value={
                    "SENSE_COLLECTOR_API_PASSWORD": fake_password,
                    "SENSE_COLLECTOR_INFLUXDB_TOKEN": fake_token,
                    "SENSE_COLLECTOR_API_USERNAME": "person@example.com",
                    "SENSE_COLLECTOR_LOG_LEVEL_GENERAL": "INFO",
                },
            ),
        ):
            validate_environment()

        logged = caplog.text
        assert fake_password not in logged
        assert fake_token not in logged
        assert "person@example.com" not in logged
        # a non-sensitive setting is shown in full — masking everything would be useless
        assert "INFO" in logged

    def test_every_sensitive_marker_is_actually_masked(self, caplog):
        """Guards the _SENSITIVE tuple itself: each marker must drive real masking."""
        settings = {f"SENSE_COLLECTOR_{s}": f"value-for-{s}" for s in _SENSITIVE}
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(config, "describe_settings", return_value=settings),
        ):
            validate_environment()
        for raw in settings.values():
            assert raw not in caplog.text


class _FakeCollector:
    """Stands in for SenseCollector.collect_sense_data with a chosen ending."""

    def __init__(self, ending):
        self.ending = ending
        self.cancelled = False

    async def collect_sense_data(self, shutdown_event):
        try:
            if self.ending == "forever":
                await asyncio.Event().wait()
            if self.ending == "raise":
                raise ConnectionError("websocket gone")
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TestRunCollectorTasks:
    """Any data-task ending other than a requested shutdown must exit non-zero, so the
    container's restart policy actually restarts it."""

    @pytest.mark.asyncio
    async def test_shutdown_stops_the_data_task_cleanly(self):
        collector = _FakeCollector("forever")
        shutdown = asyncio.Event()
        shutdown.set()
        await run_collector_tasks(collector, shutdown)
        assert collector.cancelled

    @pytest.mark.asyncio
    async def test_a_data_task_that_returns_is_a_failure(self):
        with pytest.raises(RuntimeError, match="exited unexpectedly"):
            await run_collector_tasks(_FakeCollector("return"), asyncio.Event())

    @pytest.mark.asyncio
    async def test_a_data_task_error_propagates(self):
        with pytest.raises(ConnectionError, match="websocket gone"):
            await run_collector_tasks(_FakeCollector("raise"), asyncio.Event())


class TestCreateCollector:
    @pytest.mark.asyncio
    async def test_failed_auth_closes_the_client_and_raises(self):
        collector = AsyncMock()
        collector.authenticate.side_effect = PermissionError("bad credentials")
        with (
            patch("app.main.InfluxDBStorage") as storage_cls,
            patch("app.main.SenseCollector", return_value=collector),
            pytest.raises(PermissionError),
        ):
            storage_cls.return_value.connect = AsyncMock()
            await create_collector()
        collector.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_both_connected_clients(self):
        collector = AsyncMock()
        with (
            patch("app.main.InfluxDBStorage") as storage_cls,
            patch("app.main.SenseCollector", return_value=collector),
        ):
            storage_cls.return_value.connect = AsyncMock()
            got_collector, got_storage = await create_collector()
        assert got_collector is collector
        assert got_storage is storage_cls.return_value
        got_storage.connect.assert_awaited_once()
        collector.connect.assert_awaited_once()
        collector.close.assert_not_awaited()


class TestMain:
    """main() always closes both clients, and a fatal error exits non-zero."""

    @pytest.mark.asyncio
    async def test_a_clean_shutdown_closes_both_clients(self):
        collector, storage = AsyncMock(), AsyncMock()
        with (
            patch("app.main.validate_environment"),
            patch("app.main.create_collector", return_value=(collector, storage)),
            patch("app.main.run_collector_tasks", new=AsyncMock()),
        ):
            await main()
        collector.close.assert_awaited_once()
        storage.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_fatal_error_exits_non_zero_after_cleanup(self):
        collector, storage = AsyncMock(), AsyncMock()
        collector.close.side_effect = OSError("already closed")
        failing = AsyncMock(side_effect=RuntimeError("Task exited unexpectedly"))
        with (
            patch("app.main.validate_environment"),
            patch("app.main.create_collector", return_value=(collector, storage)),
            patch("app.main.run_collector_tasks", new=failing),
            pytest.raises(SystemExit) as exit_info,
        ):
            await main()
        assert exit_info.value.code == 1
        # A failing collector close must not skip the InfluxDB close after it.
        storage.close.assert_awaited_once()
