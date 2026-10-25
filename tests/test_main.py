"""Tests for the entrypoint's startup guards.

Two things here are load-bearing and cheap to get wrong: the masking applied to secrets
before they reach the startup log, and the fail-fast on missing required config. The
orchestration (task endings, client lifecycle) is tested here against stand-ins; the
real wiring end to end is the e2e stack.
"""

import asyncio
import logging
import os
import re
from pathlib import Path
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
    """`_obscure` is the last thing standing between a credential and the log file.

    It says only whether a secret is set: any character of a password, or its length, is
    entropy handed to everyone who reads the logs."""

    @pytest.mark.parametrize(
        "secret", ["a", "abcd", "abcde", "hunter2", "Tr0ub4dor", "a-long-influx-token"]
    )
    def test_reveals_no_character_and_no_length(self, secret):
        assert _obscure(secret) == "<set>"

    def test_an_unset_secret_says_so(self):
        assert _obscure("") == "<unset>"


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

    def test_exports_to_an_unwritable_folder_are_refused_not_redirected(self, caplog):
        """With exports on, a temp-dir fallback silently loses every export with the
        container; say so and stop instead."""
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(config, "OUTPUT_RECEIVED_DATA", True),
            patch.object(config, "EXPORT_FOLDER_UNWRITABLE", "/root/no_permission"),
            pytest.raises(ValueError, match="export folder"),
        ):
            validate_environment()
        assert "/root/no_permission" in caplog.text

    def test_the_fallback_is_harmless_with_exports_off(self):
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(config, "OUTPUT_RECEIVED_DATA", False),
            patch.object(config, "EXPORT_FOLDER_UNWRITABLE", "/root/no_permission"),
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

    def test_credentials_in_the_influxdb_url_are_refused_and_never_logged(self, caplog):
        """InfluxDB 2.x authenticates by token; a user:password URL cannot work, and its
        password would be printed by the startup log and the connect error."""
        password = "placeholder-" + "pw" * 6
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        env["SENSE_COLLECTOR_INFLUXDB_URL"] = f"http://alice:{password}@influx:8086"
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(
                config,
                "describe_settings",
                return_value={
                    "SENSE_COLLECTOR_INFLUXDB_URL": env["SENSE_COLLECTOR_INFLUXDB_URL"]
                },
            ),
            patch.object(config, "INFLUXDB_URL", env["SENSE_COLLECTOR_INFLUXDB_URL"]),
            pytest.raises(ValueError, match="credentials"),
        ):
            validate_environment()

        assert password not in caplog.text
        assert "http://influx:8086" in caplog.text

    @pytest.mark.parametrize(
        ("url", "problem"),
        [
            ("http://influx:99999", "not a valid URL"),
            ("http://[::1", "not a valid URL"),
            ("alice:placeholder-pw@influx:8086", "credentials"),
        ],
    )
    def test_an_unusable_influxdb_url_is_refused_with_a_reason(
        self, caplog, url, problem
    ):
        """Startup must say why it stops, and never print the password on the way."""
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(
                config,
                "describe_settings",
                return_value={"SENSE_COLLECTOR_INFLUXDB_URL": url},
            ),
            patch.object(config, "INFLUXDB_URL", url),
            pytest.raises(ValueError, match=problem),
        ):
            validate_environment()

        assert any(
            r.levelname == "ERROR" and problem in r.getMessage() for r in caplog.records
        )
        assert "placeholder-pw" not in caplog.text

    def test_an_ipv6_url_keeps_its_brackets_in_the_log(self, caplog):
        url = "http://[::1]:8086/x"
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(
                config,
                "describe_settings",
                return_value={
                    "SENSE_COLLECTOR_API_BASE_URL": "http://u:p@[::1]:8086/x"
                },
            ),
        ):
            validate_environment()
        assert url in caplog.text

    def test_the_token_renewal_interval_is_shown(self, caplog):
        """An interval, not a secret: masking it would only hide useful config."""
        env = dict.fromkeys(config.REQUIRED_ENV_VARS, "set")
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(
                config,
                "describe_settings",
                return_value={"SENSE_COLLECTOR_API_TOKEN_RENEW": "43200"},
            ),
        ):
            validate_environment()
        assert "SENSE_COLLECTOR_API_TOKEN_RENEW: 43200" in caplog.text

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


# The committed files that set an InfluxDB token. An explicit list, never a glob: the main
# checkout also holds the gitignored .env.dev*/.env.prod* files with real secrets.
_TOKEN_FILES = (".env.demo", ".env.e2e", ".env.dev.local.example", "Makefile")
_TOKEN_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(INFLUX_TOKEN|SENSE_COLLECTOR_INFLUXDB_TOKEN|TEST_INFLUXDB_TOKEN)"
    r"\s*=\s*['\"]?([^'\"\s$]+)",
    re.MULTILINE,
)


def _committed_influx_tokens() -> set[str]:
    """Every literal InfluxDB token this public repo commits, in any form compose reads."""
    root = Path(__file__).resolve().parents[1]
    return {
        value
        for name in _TOKEN_FILES
        if (root / name).exists()
        for _, value in _TOKEN_ASSIGNMENT.findall((root / name).read_text())
    }


class TestPublicTokenGuard:
    """A stack that talks to the real Sense cloud must never write with a token that is
    public in this repository: anyone who reads the repo could read the household's data."""

    @pytest.fixture
    def required_env(self):
        with patch.dict(
            os.environ, dict.fromkeys(config.REQUIRED_ENV_VARS, "set"), clear=False
        ):
            yield

    @pytest.mark.parametrize("token", sorted(_committed_influx_tokens()))
    def test_a_real_account_stack_refuses_a_committed_token(self, required_env, token):
        with (
            patch.object(config, "INFLUXDB_TOKEN", token),
            patch.object(config, "API_BASE_URL", config.SENSE_CLOUD_API_BASE_URL),
            pytest.raises(ValueError, match="public InfluxDB token"),
        ):
            validate_environment()

    def test_the_demo_and_e2e_stacks_may_use_their_committed_token(self, required_env):
        """They point the collector at the fake Sense, not the real cloud."""
        token = sorted(_committed_influx_tokens())[0]
        with (
            patch.object(config, "INFLUXDB_TOKEN", token),
            patch.object(config, "API_BASE_URL", "http://sense_fake:8080"),
        ):
            validate_environment()

    def test_a_real_account_stack_with_its_own_token_starts(self, required_env):
        with (
            patch.object(config, "INFLUXDB_TOKEN", "a-token-of-its-own"),
            patch.object(config, "API_BASE_URL", config.SENSE_CLOUD_API_BASE_URL),
        ):
            validate_environment()

    def test_the_guard_knows_every_committed_token(self):
        assert _committed_influx_tokens() <= config.PUBLIC_INFLUXDB_TOKENS

    def test_the_bundled_influxdbs_admin_token_is_checked_too(self, required_env):
        """The dev stacks set up their InfluxDB with INFLUX_TOKEN, a separate variable."""
        token = sorted(_committed_influx_tokens())[0]
        with (
            patch.dict(os.environ, {"INFLUX_TOKEN": token}),
            patch.object(config, "INFLUXDB_TOKEN", "a-token-of-its-own"),
            patch.object(config, "API_BASE_URL", config.SENSE_CLOUD_API_BASE_URL),
            pytest.raises(ValueError, match="public InfluxDB token"),
        ):
            validate_environment()

    def test_surrounding_whitespace_does_not_disguise_a_committed_token(
        self, required_env
    ):
        """A quoted env value keeps its spaces, and InfluxDB accepts the token anyway."""
        token = sorted(_committed_influx_tokens())[0]
        with (
            patch.object(config, "INFLUXDB_TOKEN", f"{token} "),
            patch.object(config, "API_BASE_URL", config.SENSE_CLOUD_API_BASE_URL),
            pytest.raises(ValueError, match="public InfluxDB token"),
        ):
            validate_environment()

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.sense.com/apiservice/api/v1/",
            "https://API.SENSE.COM/apiservice/api/v1",
            "https://api.sense.com:443/apiservice/api/v1",
            "https://api.sense.com./apiservice/api/v1",
        ],
    )
    def test_any_spelling_of_the_real_cloud_counts(self, required_env, url):
        token = sorted(_committed_influx_tokens())[0]
        with (
            patch.object(config, "INFLUXDB_TOKEN", token),
            patch.object(config, "API_BASE_URL", url),
            pytest.raises(ValueError, match="public InfluxDB token"),
        ):
            validate_environment()

    def test_the_token_reader_sees_every_form_compose_accepts(self, tmp_path):
        text = (
            "export INFLUX_TOKEN=one\nINFLUX_TOKEN = two\nTEST_INFLUXDB_TOKEN='three'\n"
        )
        found = {v for _, v in _TOKEN_ASSIGNMENT.findall(text)}
        assert found == {"one", "two", "three"}


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
