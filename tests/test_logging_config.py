"""The fleet logging setup as this collector installs it: JSON lines on stdout.

app/core/logging_config.py is emitted by luxarch and never edited here; these tests pin the
behaviour the collector relies on (the line contract, the per-area levels, the extra= collision
fix, control-character safety for remote-derived values) and the entrypoint's wiring of it.
"""

import json
import logging

import pytest

from app.core.logging_config import configure_logging, log_context
from app.utils.logging import AREA_LEVELS, api_logger, logger, storage_logger

TOUCHED = ("general", "api", "storage", "httpx", "httpcore", "app.test")


@pytest.fixture(autouse=True)
def _restore_logging(monkeypatch):
    """configure_logging mutates process-global state; put every piece of it back."""
    for var in ("LOG_FORMAT", "LOG_LEVEL", "LOG_MODULE_LEVELS"):
        monkeypatch.delenv(var, raising=False)
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {name: logging.getLogger(name).level for name in TOUCHED}
    # Re-set to itself so monkeypatch restores the stock method after configure_logging swaps it.
    monkeypatch.setattr(logging.Logger, "makeRecord", logging.Logger.makeRecord)
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def _lines(capsys) -> list[dict]:
    return [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]


def test_a_line_is_one_json_object_with_the_fleet_envelope(capsys):
    configure_logging(
        service="sense-collector", version="2026.10.1", levels=AREA_LEVELS
    )
    storage_logger.info("wrote %s points", 5, extra={"bucket": "sense"})

    (line,) = _lines(capsys)
    assert line["message"] == "wrote 5 points"
    assert line["level"] == "INFO"
    assert line["logger"] == "storage"
    assert line["service"] == "sense-collector"
    assert line["version"] == "2026.10.1"
    assert line["timestamp"].endswith("Z")
    assert line["attributes"] == {"bucket": "sense"}


def test_area_levels_gate_each_logger_independently(capsys, monkeypatch):
    levels = {**AREA_LEVELS, "general": "WARNING", "api": "DEBUG"}
    configure_logging(service="s", version="v", levels=levels)

    logger.info("general info is below WARNING")
    logger.warning("general warning")
    api_logger.debug("api debug")
    logging.getLogger("httpx").info("HTTP Request: GET /")

    assert [x["message"] for x in _lines(capsys)] == ["general warning", "api debug"]


def test_log_module_levels_overrides_at_runtime(capsys, monkeypatch):
    monkeypatch.setenv("LOG_MODULE_LEVELS", '{"storage": "ERROR"}')
    configure_logging(service="s", version="v", levels=AREA_LEVELS)

    storage_logger.warning("dropped")
    storage_logger.error("kept")

    assert [x["message"] for x in _lines(capsys)] == ["kept"]


def test_a_colliding_extra_key_is_kept_not_raised(capsys):
    configure_logging(service="s", version="v")
    # Stock makeRecord raises KeyError on `name`/`module`; the device name is a natural key.
    logging.getLogger("app.test").warning(
        "device renamed", extra={"name": "Fridge", "module": "kitchen"}
    )

    (line,) = _lines(capsys)
    assert line["attributes"] == {"name": "Fridge", "module": "kitchen"}
    assert line["logger"] == "app.test"


def test_a_device_name_with_a_newline_cannot_forge_a_second_line(capsys):
    configure_logging(service="s", version="v")
    api_logger.warning("new device: %s", 'Lamp\n{"level": "CRITICAL"}\x1b[31m')

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert (
        json.loads(out)["message"] == 'new device: Lamp\n{"level": "CRITICAL"}\x1b[31m'
    )


def test_log_context_fields_ride_on_every_line_inside_it(capsys):
    configure_logging(service="s", version="v")
    with log_context(monitor_id="12345"):
        api_logger.warning("ws reconnect")
    api_logger.warning("outside")

    inside, outside = _lines(capsys)
    assert inside["attributes"] == {"monitor_id": "12345"}
    assert "attributes" not in outside


def test_exceptions_carry_their_traceback(capsys):
    configure_logging(service="s", version="v")
    try:
        raise ValueError("bad payload")
    except ValueError:
        storage_logger.exception("write failed")

    (line,) = _lines(capsys)
    assert line["exception_type"] == "ValueError"
    assert "bad payload" in line["exception"]


def test_text_format_is_one_readable_line(capsys, monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    configure_logging(service="s", version="v")
    storage_logger.warning("queue full", extra={"device_id": "d1"})

    out = capsys.readouterr().out.strip()
    assert "WARNING storage queue full device_id=d1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_a_failing_redactor_never_writes_the_content(capsys):
    def boom(_line):
        raise RuntimeError("redactor bug")

    configure_logging(service="s", version="v", redact=boom)
    api_logger.warning("token %s", "secret-value")

    (line,) = _lines(capsys)
    assert "secret-value" not in json.dumps(line)
    assert line["redaction_error"] == "RuntimeError"


def test_reconfiguring_replaces_the_handler(capsys):
    configure_logging(service="s", version="v")
    configure_logging(service="s", version="v")
    logger.warning("once")

    assert len(_lines(capsys)) == 1


TRACE, SPAN = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"


def test_a_trace_id_source_puts_the_span_ids_on_the_line(capsys):
    configure_logging(service="s", version="v", trace_ids=lambda: (TRACE, SPAN))
    logger.warning("in a span")

    (line,) = _lines(capsys)
    assert (line["trace_id"], line["span_id"]) == (TRACE, SPAN)


@pytest.mark.parametrize(
    "answer",
    [RuntimeError("tracer bug"), None, (TRACE,), (TRACE, 7), "not-a-pair"],
    ids=["raises", "none", "one-id", "not-str", "not-tuple"],
)
def test_a_bad_trace_id_source_costs_the_ids_never_the_line(capsys, answer):
    def source():
        if isinstance(answer, Exception):
            raise answer
        return answer

    configure_logging(service="s", version="v", trace_ids=source)
    logger.warning("still written")

    (line,) = _lines(capsys)
    assert line["message"] == "still written"
    assert "trace_id" not in line and "span_id" not in line


# A stand-in for pydantic's ValidationError: the logging module recognises one by its package and
# class name, so the collector's suite exercises the withholding path without depending on pydantic.
SECRET = "swordfish-input"


class ValidationError(ValueError):
    def errors(self):
        return [
            {
                "type": "missing",
                "loc": ("password",),
                "msg": "Field required",
                "input": SECRET,
            }
        ]

    def __str__(self):
        return f"1 validation error input_value={SECRET!r}"

    __repr__ = __str__  # pydantic's repr quotes the failing input too


ValidationError.__module__ = "pydantic_core._pydantic_core"


def _chain(kind):
    inner = ValidationError()
    if kind == "direct":
        return inner
    if kind == "cause":
        try:
            raise RuntimeError("wrapped") from inner
        except RuntimeError as err:
            return err
    return ExceptionGroup("group", [inner])


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
@pytest.mark.parametrize("kind", ["direct", "cause", "group"])
def test_a_validation_errors_input_never_reaches_a_traceback(
    capsys, monkeypatch, kind, text
):
    if text:
        monkeypatch.setenv("LOG_FORMAT", "text")
    configure_logging(service="s", version="v")
    exc = _chain(kind)
    try:
        raise exc
    except Exception:
        logger.exception("failed")

    out = capsys.readouterr().out
    assert SECRET not in out
    assert "message withheld" in out


@pytest.mark.parametrize(
    "args",
    [
        (ValidationError(),),
        ([1, {"k": ValidationError()}],),
        (RuntimeError("bad", ValidationError()),),
    ],
    ids=["direct", "nested", "in-exception-args"],
)
def test_a_validation_error_passed_as_an_argument_is_withheld(capsys, args):
    configure_logging(service="s", version="v")
    logger.warning("refused: %s", *args)

    (line,) = _lines(capsys)
    assert SECRET not in json.dumps(line)


def test_a_validation_error_in_a_mapping_argument_is_withheld(capsys):
    configure_logging(service="s", version="v")
    logger.warning("refused: %(e)s", {"e": ValidationError()})

    (line,) = _lines(capsys)
    assert SECRET not in json.dumps(line)


def test_an_errors_entry_is_written_without_its_input(capsys):
    configure_logging(service="s", version="v")
    logger.warning("refused: %s", ValidationError().errors())

    (line,) = _lines(capsys)
    assert SECRET not in json.dumps(line)
    assert "password" in line["message"]


def test_a_validation_error_in_an_extra_field_or_context_is_withheld(capsys):
    configure_logging(service="s", version="v")
    with log_context(detail=[ValidationError()]):
        logger.warning("refused", extra={"errors": {"first": ValidationError()}})

    (line,) = _lines(capsys)
    assert SECRET not in json.dumps(line)


class _Slotted:
    __slots__ = ("__error", "row")

    def __init__(self, row, error):
        self.row = row
        self.__error = error

    def __repr__(self):
        return f"_Slotted({self.row}, {self.__error!r})"


class _Refusal(Exception):
    def __init__(self, detail):
        super().__init__()
        self.detail = detail

    def __str__(self):
        return f"refused: {self.detail}"


@pytest.mark.parametrize(
    "holder",
    [_Slotted(3, ValidationError()), _Refusal(ValidationError().errors())],
    ids=["slotted-object", "exception-attribute"],
)
def test_an_object_holding_a_validation_error_is_withheld(capsys, holder):
    configure_logging(service="s", version="v")
    logger.warning("result %s", holder)

    (line,) = _lines(capsys)
    assert SECRET not in json.dumps(line)


@pytest.mark.parametrize(
    "value",
    [[1, 2], ("a", {"k": "v"}), _Slotted(1, "plain"), RuntimeError("plain")],
    ids=["list", "nested", "object", "exception"],
)
def test_a_value_holding_no_validation_error_is_written_as_passed(capsys, value):
    configure_logging(service="s", version="v")
    logger.warning("value %s", value)

    (line,) = _lines(capsys)
    assert line["message"] == f"value {value}"
