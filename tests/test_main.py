"""Tests for the entrypoint's startup guards.

Two things here are load-bearing and cheap to get wrong: the masking applied to secrets
before they reach the startup log, and the fail-fast on missing required config. The
orchestration loop itself (task wiring + signal handlers) is covered by the e2e stack.
"""

import os
from unittest.mock import patch

import pytest

from app.core import config
from app.main import _SENSITIVE, _obscure, validate_environment


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
