# luxarch:logging-contract asset v4 - DO NOT edit this marker line; it is how repo.emitted_assets_current knows your copy is current. Re-emit with `luxarch --emit logging-contract`.
"""The contract of the logging module's trace-id source, asked of your copy of the module.

Save as `tests/test_logging_contract.py` and set the one EDIT THIS line. `configure_logging(trace_ids=)`
takes a source (the tracing module's `current_trace_ids`) and every record made while a span is current
carries its ids. A module that handles the happy path passes a smoke test; these tests pin what a
smoke test misses, each against a change to the module that would otherwise go unnoticed:

- the ids are the span's pair, under OpenTelemetry's record attribute names, in the order (trace, span);
- the source is called once per record, inside the caller's context, when the record is MADE: a handler
  that formats later (a `MemoryHandler`, a queue) still writes the ids the record was made under;
- a source that raises `Exception` (any subclass) costs the line its ids, never the line; a
  `BaseException` (Ctrl-C) is not swallowed by a log call;
- a source that answers anything but a `(str, str)` tuple costs the line its ids and writes none, and
  nothing is remembered from an earlier good answer;
- a line that carries `extra=` still carries the ids; a caller who names `otelTraceID` / `otelSpanID`
  in `extra=` keeps the caller's and gets none from the span (never one id from each);
- the JSON line has `trace_id` / `span_id` at the top level, once, and the text line names them once;
- configuring again without a source stops writing ids;
- a traceback whose chain (cause, context, exception group) holds a pydantic or FastAPI validation
  error is written without that error's message, in the JSON and the text line, for every logger;
- a validation error passed as a `%s` argument of a log call is written as its type name;
- so is one inside an argument (a list, tuple, set or dict, at any depth), an `extra=` field or a
  `log_context` field; an exception whose arguments or attributes hold one (`detail=err.errors()`)
  is withheld, in a traceback too, and so is an object (a dataclass, a slotted class) holding one;
- an entry of a validation error's `errors()` is written without its `input`, wherever it is passed,
  and an entry whose message is the validator's own (`value_error`, `assertion_error`, a custom type)
  as its `type` and `loc` only;
- an argument, field, object or exception that holds no validation error is written as passed;
- a value that raises when it is read is written as its type, and the line is still written.

The validation cases raise pydantic's own errors where the repo has pydantic. Without it, they raise a
stand-in that carries pydantic_core's class name and package and gives pydantic's `errors()` entries,
so every case runs in every repo. A validation error from pydantic's v1 API and FastAPI's
`ValidationException` are stand-ins in every repo.

Edit nothing else. A failure here is the module's contract broken, not a test to adapt.
"""

from __future__ import annotations

import contextvars
import dataclasses
import importlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from typing import Annotated, Any, Protocol, runtime_checkable

import pytest

# --- EDIT THIS: where your copy of the emitted logging module is imported from. ---
LOGGING_MODULE = "app.core.logging_config"

# The stdlib method the module replaces; named so the restore reads as what it is.
_MAKE_RECORD = "makeRecord"
_TID = "0af7651916cd43dd8448eb211c80319c"
_SID = "b7ad6b7169203331"


class _Probe(logging.Handler):
    """Keeps the records; formatting is the test's to do, when it chooses."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.module = importlib.import_module(LOGGING_MODULE)
        self.monkeypatch = monkeypatch
        self.log = logging.getLogger("luxarch.logging_contract")
        self.log.setLevel(logging.DEBUG)
        self.probe = _Probe()
        self.log.addHandler(self.probe)
        self.formatter: logging.Formatter | None = None
        self.formatted: list[str] = []

    def configure(self, source: Callable[[], Any] | None, text: bool = False) -> None:
        self.monkeypatch.setenv("LOG_FORMAT", "text" if text else "json")
        self.module.configure_logging(service="contract", version="1", trace_ids=source)
        self.formatter = logging.getLogger().handlers[0].formatter
        assert self.formatter is not None

    def lines(self) -> list[str]:
        assert self.formatter is not None
        return [self.formatter.format(r) for r in self.probe.records]

    def json_lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines()]


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    make_record = logging.Logger.makeRecord
    r = _Rig(monkeypatch)
    try:
        yield r
    finally:
        r.module.configure_logging(service="contract", version="1", trace_ids=None)
        r.log.removeHandler(r.probe)
        root.handlers[:] = handlers
        root.setLevel(level)
        setattr(logging.Logger, _MAKE_RECORD, make_record)


def _good() -> tuple[str, str]:
    return _TID, _SID


def _has_no_ids(record: logging.LogRecord) -> bool:
    return "otelTraceID" not in record.__dict__ and "otelSpanID" not in record.__dict__


def test_the_record_carries_the_sources_pair_under_otels_names_in_order(
    rig: _Rig,
) -> None:
    rig.configure(_good)
    rig.log.warning("in a span")
    (record,) = rig.probe.records
    assert record.__dict__["otelTraceID"] == _TID
    assert record.__dict__["otelSpanID"] == _SID


def test_the_source_is_called_once_per_record_in_the_callers_context(rig: _Rig) -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("contract_var")
    calls: list[str] = []

    def source() -> tuple[str, str]:
        calls.append(var.get("(context lost)"))
        return _TID, _SID

    rig.configure(source)
    token = var.set("callers-context")
    try:
        rig.log.warning("one")
        rig.log.warning("two")
    finally:
        var.reset(token)
    assert calls == ["callers-context", "callers-context"]


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_line_formatted_after_the_span_ends_keeps_the_ids_it_was_made_under(
    rig: _Rig, text: bool
) -> None:
    current: list[tuple[str, str] | None] = [(_TID, _SID)]
    rig.configure(lambda: current[0], text=text)
    rig.log.warning("made inside")
    current[0] = (
        "f" * 32,
        "e" * 16,
    )  # the span ended, another began, before the handler formats
    (line,) = rig.lines()
    assert _TID in line and _SID in line
    assert "f" * 32 not in line and "e" * 16 not in line


_NOT_A_PAIR: list[Any] = [
    None,
    (),
    ("a",),
    ("a", "b", "c"),
    (1, 2),
    ("a", 1),
    (1, "a"),
    "ab",
    {"a": "b"},
    ["a", "b"],
    7,
]


@pytest.mark.parametrize("bad", _NOT_A_PAIR, ids=repr)
def test_an_answer_that_is_not_a_pair_of_strings_writes_no_ids_and_keeps_the_line(
    rig: _Rig, bad: Any
) -> None:
    answers: list[Any] = [_good(), bad]
    rig.configure(lambda: answers.pop(0))
    rig.log.warning("good first")
    rig.log.warning("bad second")
    first, second = rig.probe.records
    assert first.__dict__["otelTraceID"] == _TID
    assert second.getMessage() == "bad second"
    assert _has_no_ids(second)


class _Custom(Exception):
    pass


@pytest.mark.parametrize(
    "error",
    [RuntimeError, ValueError, OSError, ImportError, KeyError, _Custom],
    ids=lambda e: e.__name__,
)
def test_a_source_that_raises_costs_the_line_its_ids_never_the_line(
    rig: _Rig, error: type[Exception]
) -> None:
    answers: list[Any] = [_good(), error("tracing bug")]

    def source() -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    rig.configure(source)
    rig.log.warning("good first")
    rig.log.warning("raising second")
    first, second = rig.probe.records
    assert first.__dict__["otelTraceID"] == _TID
    assert second.getMessage() == "raising second"
    assert _has_no_ids(second)


@pytest.mark.parametrize(
    "error", [KeyboardInterrupt, SystemExit], ids=lambda e: e.__name__
)
def test_a_base_exception_from_the_source_is_not_swallowed_by_a_log_call(
    rig: _Rig, error: type[BaseException]
) -> None:
    def source() -> tuple[str, str]:
        raise error()

    rig.configure(source)
    with pytest.raises(error):
        rig.log.warning("interrupted")


def test_a_line_carrying_extra_still_carries_the_ids(rig: _Rig) -> None:
    rig.configure(_good)
    rig.log.warning("with fields", extra={"device_id": 7})
    (line,) = rig.json_lines()
    assert line["trace_id"] == _TID and line["span_id"] == _SID
    assert line["attributes"]["device_id"] == 7


def test_the_json_line_has_the_ids_at_the_top_level_once(rig: _Rig) -> None:
    rig.configure(_good)
    rig.log.warning("json")
    (line,) = rig.json_lines()
    assert line["trace_id"] == _TID and line["span_id"] == _SID
    assert "otelTraceID" not in json.dumps(line) and "otelSpanID" not in json.dumps(
        line
    )


def test_the_text_line_names_the_ids_once(rig: _Rig) -> None:
    rig.configure(_good, text=True)
    rig.log.warning("text")
    (line,) = rig.lines()
    assert line.count(f"trace_id={_TID}") == 1 and line.count(f"span_id={_SID}") == 1
    assert "otelTraceID" not in line and "otelSpanID" not in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_callers_own_otel_id_is_never_paired_with_the_spans_other_id(
    rig: _Rig, text: bool
) -> None:
    rig.configure(_good, text=text)
    rig.log.warning("own trace", extra={"otelTraceID": "CALLER-TID"})
    rig.log.warning("own span", extra={"otelSpanID": "CALLER-SID"})
    own_trace, own_span = rig.lines()
    assert "CALLER-TID" in own_trace and _SID not in own_trace and _TID not in own_trace
    assert "CALLER-SID" in own_span and _TID not in own_span and _SID not in own_span


def test_configuring_again_without_a_source_stops_writing_ids(rig: _Rig) -> None:
    rig.configure(_good)
    rig.configure(None)
    rig.log.warning("no source")
    (record,) = rig.probe.records
    assert _has_no_ids(record)


_QUOTED = "swordfish-for-the-contract"


def _pydantic() -> Any:
    """pydantic and pydantic_core when the repo has them, else None: the stand-in below is used."""
    try:
        return importlib.import_module("pydantic"), importlib.import_module(
            "pydantic_core"
        )
    except ImportError:
        return None


def _stand_in(
    title: str,
    entry: dict[str, object],
    where: tuple[str, str] = ("pydantic_core._pydantic_core", "ValidationError"),
) -> Exception:
    """What these tests raise in a repo without pydantic: a validation error the way pydantic
    builds one, as far as the module reads it. The module knows one by its class's name and package,
    so each is an instance of a class made here with pydantic_core's (`where`), directly over
    `ValueError` as pydantic's is. Its text and repr quote the value it refused, and its `errors()`
    gives pydantic's entries (`type`, `loc`, `msg`, `input`, and `ctx` where pydantic has one). Like
    pydantic's, the instance has no arguments and no attributes of its own: its title and entries
    are its class's, so the module can withhold it only by knowing its class."""
    module, name = where

    def errors(self: Exception) -> list[dict[str, object]]:
        return [dict(entry)]

    def text(self: Exception) -> str:
        given = f", input_value={entry['input']!r}" if "input" in entry else ""
        return f"1 validation error for {title}\n  {entry['msg']} [type={entry['type']}{given}]"

    namespace = {
        "__module__": module,
        "errors": errors,
        "__str__": text,
        "__repr__": text,
    }
    refused: Exception = type(name, (ValueError,), namespace)()
    return refused


def _stand_in_refusal() -> Exception:
    message = "Input should be a valid integer, unable to parse string as an integer"
    entry: dict[str, object] = {
        "type": "int_parsing",
        "loc": (),
        "msg": message,
        "input": _QUOTED,
    }
    return _stand_in("int", entry)


def _validation_error() -> Exception:
    found = _pydantic()
    if found is None:
        return _stand_in_refusal()
    pydantic, _ = found
    try:
        pydantic.TypeAdapter(int).validate_python(_QUOTED)
    except pydantic.ValidationError as err:
        refused: Exception = err
        return refused
    raise AssertionError("pydantic accepted the value")


@runtime_checkable
class _Refused(Protocol):
    """A validation error, as far as these tests read one."""

    def errors(self) -> list[dict[str, object]]: ...


def _errors_of(err: Exception) -> list[dict[str, object]]:
    assert isinstance(err, _Refused)
    return err.errors()


def _stand_in_quoting(kind: str) -> Exception:
    said = f"{_QUOTED!r} is not an address"
    entry: dict[str, object] = {"loc": (), "input": _QUOTED}
    if kind == "assert":
        entry |= {
            "type": "assertion_error",
            "msg": f"Assertion failed, {said}",
            "ctx": {"error": AssertionError(said)},
        }
    elif kind == "custom":
        entry |= {
            "type": "not_an_address",
            "msg": f"{_QUOTED} is not an address",
            "ctx": {"value": _QUOTED},
        }
    else:
        entry |= {
            "type": "value_error",
            "msg": f"Value error, {said}",
            "ctx": {"error": ValueError(said)},
        }
    return _stand_in("function-after[refuse(), str]", entry)


def _quoting(kind: str) -> Exception:
    """A validation error from a validator whose own message quotes the value it refused."""
    found = _pydantic()
    if found is None:
        return _stand_in_quoting(kind)
    pydantic, pydantic_core = found

    def refuse(value: str) -> str:
        if kind == "assert":
            assert value.startswith("@"), f"{value!r} is not an address"
        if kind == "custom":
            raise pydantic_core.PydanticCustomError(
                "not_an_address", "{value} is not an address", {"value": value}
            )
        raise ValueError(f"{value!r} is not an address")

    adapter = pydantic.TypeAdapter(Annotated[str, pydantic.AfterValidator(refuse)])
    try:
        adapter.validate_python(_QUOTED)
    except pydantic.ValidationError as err:
        refused: Exception = err
        return refused
    raise AssertionError("pydantic accepted the value")


class _Refusal(Exception):
    """An HTTP error the way Starlette builds one: the detail is an attribute, not an argument."""

    def __init__(self, status: int, detail: object) -> None:
        super().__init__()
        self.status = status
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}"


@dataclasses.dataclass
class _RowResult:
    row: int
    error: object


class _Slotted:
    __slots__ = ("__error", "row")

    def __init__(self, row: int, error: object) -> None:
        self.row = row
        self.__error = error

    def __repr__(self) -> str:
        return f"_Slotted({self.row}, {self.__error!r})"


def _chained(kind: str) -> BaseException:
    inner = _validation_error()
    if kind == "direct":
        return inner
    if kind == "cause":
        try:
            raise RuntimeError("wrapped") from inner
        except RuntimeError as err:
            return err
    if kind == "context":
        try:
            try:
                raise inner
            except Exception:
                raise RuntimeError("wrapped") from None
        except RuntimeError as err:
            err.__cause__ = None
            err.__suppress_context__ = False
            return err
    return ExceptionGroup("group", [inner])


@pytest.mark.parametrize("kind", ["direct", "cause", "context", "group"])
@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_validation_errors_input_never_reaches_a_traceback_line(
    rig: _Rig, kind: str, text: bool
) -> None:
    rig.configure(None, text=text)
    err = _chained(kind)
    try:
        raise err
    except BaseException:
        rig.log.exception("failed")
    (line,) = rig.lines()
    assert _QUOTED not in line
    assert "message withheld" in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_validation_error_passed_as_a_log_argument_is_written_as_its_type(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    err = _validation_error()
    rig.log.error("failed: %s", err)
    rig.log.error("failed: %(e)s", {"e": err})
    for line in rig.lines():
        assert _QUOTED not in line
        assert "ValidationError: input withheld" in line


def _raised_with(arg: object) -> BaseException:
    try:
        raise RuntimeError(arg)
    except RuntimeError as err:
        return err


def _in_context(rig: _Rig, err: Exception) -> None:
    bind: Callable[..., AbstractContextManager[None]] = rig.module.log_context
    with bind(error=err):
        rig.log.warning("form_refused")
        # formatted inside the block, as a handler formats a record it is handed
        rig.formatted = rig.lines()


def _traceback_of(rig: _Rig, err: Exception) -> None:
    try:
        raise RuntimeError("import failed", _errors_of(err))
    except RuntimeError:
        rig.log.exception("failed")


def _refusal_traceback(rig: _Rig, err: Exception) -> None:
    errors = _errors_of(err)
    try:
        raise _Refusal(422, errors)
    except _Refusal:
        rig.log.exception("request failed")


def _in_a_cycle(rig: _Rig, err: Exception) -> None:
    holder: list[object] = [err]
    holder.append(holder)
    rig.log.warning("failures: %s", holder)


class _Order:
    """An ORM-style parent: its items refer back to it, and it keeps the errors of its last save."""

    def __init__(self, errors: object) -> None:
        self.items: list[_Item] = []
        self.errors = errors


class _Item:
    def __init__(self, order: _Order) -> None:
        self.order = order
        order.items.append(self)


def _with_a_backref(rig: _Rig, err: Exception) -> None:
    order = _Order(_errors_of(err))
    item = _Item(order)
    rig.log.warning("order %s (%s)", order, item)


def _deep(rig: _Rig, err: Exception) -> None:
    nested: object = err
    for _ in range(20):
        nested = [nested]
    rig.log.warning("failures: %s", nested)


# Each way a caller hands a validation error (or its `errors()`) to a log call, other than the
# traceback and a top-level `%s` argument.
_HANDED: dict[str, Callable[[_Rig, Exception], None]] = {
    "an extra field": lambda r, e: r.log.warning("form_refused", extra={"error": e}),
    "a nested extra field": lambda r, e: r.log.warning(
        "form_refused", extra={"batch": {"rows": [e]}}
    ),
    "a log_context field": _in_context,
    "errors() as an argument": lambda r, e: r.log.warning(
        "form_refused: %s",
        _errors_of(e),
    ),
    "errors() as an extra field": lambda r, e: r.log.warning(
        "form_refused",
        extra={"errors": _errors_of(e)},
    ),
    "one errors() entry": lambda r, e: r.log.warning(
        "form_refused: %s",
        _errors_of(e)[0],
    ),
    "a list argument": lambda r, e: r.log.warning("failures: %s", [e]),
    "a tuple in a dict argument": lambda r, e: r.log.warning(
        "failures: %s", {"batch": [(1, e)]}
    ),
    "a set argument": lambda r, e: r.log.warning("failures: %s", {e}),
    "a mapping argument": lambda r, e: r.log.warning("failures: %(f)s", {"f": [e]}),
    "an exception argument holding it": lambda r, e: r.log.warning(
        "failed: %s", _raised_with(e)
    ),
    "an exception argument holding errors()": lambda r, e: r.log.warning(
        "failed: %s",
        _raised_with(_errors_of(e)),
    ),
    "a traceback whose exception holds errors()": _traceback_of,
    "a list that holds itself": _in_a_cycle,
    "an exception attribute holding errors() as an argument": lambda r, e: (
        r.log.warning("refused: %s", _Refusal(422, _errors_of(e)))
    ),
    "an exception attribute holding errors() as an extra field": lambda r, e: (
        r.log.warning("refused", extra={"error": _Refusal(422, _errors_of(e))})
    ),
    "a traceback whose exception attribute holds errors()": _refusal_traceback,
    "a dataclass argument holding it": lambda r, e: r.log.warning(
        "import finished: %s", [_RowResult(1, e)]
    ),
    "a dataclass extra field holding it": lambda r, e: r.log.warning(
        "row_failed", extra={"result": _RowResult(1, e)}
    ),
    "an object's private slot holding it": lambda r, e: r.log.warning(
        "failed: %s", _Slotted(1, e)
    ),
    "a frozenset argument": lambda r, e: r.log.warning("failed: %s", frozenset({e})),
    "a dict key": lambda r, e: r.log.warning("failed: %s", {e: 1}),
    "errors() of a validator quoting the value": lambda r, e: r.log.warning(
        "refused: %s", _errors_of(_quoting("value"))
    ),
    "errors() of an assert quoting the value": lambda r, e: r.log.warning(
        "refused", extra={"errors": _errors_of(_quoting("assert"))}
    ),
    "errors() of a custom error quoting the value": lambda r, e: r.log.warning(
        "refused: %s", _errors_of(_quoting("custom"))
    ),
    "a list nested twenty deep": _deep,
    "a dict's values view": lambda r, e: r.log.warning(
        "rejected fields: %s", {"age": _errors_of(e)[0]}.values()
    ),
    "a dict's items view as an extra field": lambda r, e: r.log.warning(
        "rejected", extra={"fields": {"age": _errors_of(e)[0]}.items()}
    ),
    "an object and the child that refers back to it": _with_a_backref,
}


@pytest.mark.parametrize("how", list(_HANDED))
@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_validation_errors_input_handed_any_other_way_is_withheld(
    rig: _Rig, how: str, text: bool
) -> None:
    rig.configure(None, text=text)
    _HANDED[how](rig, _validation_error())
    (line,) = rig.formatted or rig.lines()
    assert _QUOTED not in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_an_errors_entry_keeps_every_field_but_its_input(rig: _Rig, text: bool) -> None:
    rig.configure(None, text=text)
    err = _validation_error()
    rig.log.warning("refused: %s", _errors_of(err))
    (line,) = rig.lines()
    assert _QUOTED not in line
    assert "int_parsing" in line and "'loc'" in line and "'msg'" in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_arguments_and_fields_without_a_validation_error_are_written_as_passed(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    items = [
        1,
        {"a": (2, "b")},
        {"input": "kept"},
        {"type": "reading", "input": "kept too"},
    ]
    rig.log.warning(
        "n=%s items=%s error=%s",
        3,
        items,
        ValueError("plain text"),
        extra={"device": {"id": 7, "tags": ["x"]}, "input": "also kept"},
    )
    (line,) = rig.lines()
    assert f"n=3 items={items} error=plain text" in line
    if not text:
        attributes = json.loads(line)["attributes"]
        assert attributes == {"device": {"id": 7, "tags": ["x"]}, "input": "also kept"}


@pytest.mark.parametrize("kind", ["value", "assert", "custom"])
def test_an_entry_whose_message_is_the_validators_own_keeps_only_its_type_and_loc(
    rig: _Rig, kind: str
) -> None:
    rig.configure(None)
    rig.log.warning("refused: %s", _errors_of(_quoting(kind)))
    (line,) = rig.lines()
    assert _QUOTED not in line
    assert "'loc'" in line and "'type'" in line
    assert "'msg'" not in line and "'ctx'" not in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_objects_and_exceptions_without_a_validation_error_are_written_as_passed(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    rig.log.warning(
        "%s %s %s",
        _RowResult(2, "ok"),
        _Refusal(404, "no such page"),
        _Slotted(3, None),
    )
    (line,) = rig.lines()
    assert "_RowResult(row=2, error='ok') 404: no such page _Slotted(3, None)" in line


class _Linked:
    """A live client object whose graph points back at itself (a response and its request, a pool
    and its connections): nothing in it is a validation error."""

    def __init__(self) -> None:
        self.peer = self
        self.parts = [self, {"owner": self}]


class _ClientError(Exception):
    """An HTTP or database client error carrying its live response and whatever state it keeps."""

    def __init__(self, text: str, state: object = None) -> None:
        super().__init__(text)
        self.response = _Linked()
        self.state = state


class _Device:
    def __init__(self, n: int) -> None:
        self.n = n


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_an_ordinary_error_whose_graph_has_a_cycle_keeps_its_text(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    rig.log.error("HTTP error: %s", _ClientError("503 for /api/devices"))
    err = _ClientError("write failed: field type conflict")
    try:
        raise err
    except _ClientError:
        rig.log.exception("InfluxDB write failed")
    first, second = rig.lines()
    assert "503 for /api/devices" in first and "withheld" not in first
    assert "field type conflict" in second and "withheld" not in second


def test_an_ordinary_error_past_the_walks_reach_keeps_its_text(rig: _Rig) -> None:
    """Past the node budget or the depth bound the walk has found nothing: the value is written."""
    rig.configure(None)
    wide = _ClientError("too many devices", [_Device(n) for n in range(12_000)])
    nested: object = _Device(0)
    for _ in range(24):
        nested = [nested, _Device(1)]
    deep = _ClientError("nested state", nested)
    rig.log.error("failed: %s / %s", wide, deep)
    (line,) = rig.lines()
    assert "too many devices" in line and "nested state" in line
    assert "withheld" not in line


def test_a_validation_error_past_the_walks_reach_is_still_withheld(rig: _Rig) -> None:
    rig.configure(None)
    err = _validation_error()
    entries = [dict(e) for _ in range(10_500) for e in _errors_of(err)]
    wrapped = [_Device(n) for n in range(11_000)] + [_RowResult(1, err)]
    rig.log.warning("refused: %s", entries)
    rig.log.warning("rows: %s", wrapped)
    for line in rig.lines():
        assert _QUOTED not in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_values_beside_a_validation_error_are_written_as_passed(
    rig: _Rig, text: bool
) -> None:
    """The walk runs only for an argument that holds a validation error; there, everything else in
    it is written as passed."""
    rig.configure(None, text=text)
    items = [
        1,
        {"a": (2, "b")},
        {"input": "kept"},
        {"type": "reading", "input": "kept too"},
    ]
    batch = [
        items,
        ValueError("plain text"),
        _RowResult(2, "ok"),
        _Refusal(404, "no such page"),
        _Slotted(3, None),
        _validation_error(),
    ]
    rig.log.warning("batch: %s", batch)
    (line,) = rig.lines()
    assert _QUOTED not in line
    message = line if text else json.loads(line)["message"]
    kept = f"[{items!r}, ValueError('plain text'), _RowResult(row=2, error='ok'), "
    assert f"batch: {kept}" in message, message
    assert (
        "_Refusal(), _Slotted(3, None), '<ValidationError: input withheld>']" in message
    )


def test_a_validation_error_beside_a_large_batch_is_withheld(rig: _Rig) -> None:
    """The search of a call stops at its budget; a batch past it never clears the call."""
    rig.configure(None)
    errors = _errors_of(_validation_error())
    rows = [{"device": n, "reading": float(n)} for n in range(60_000)]
    rig.log.warning("rejected", extra={"errors": errors, "batch": rows})
    rig.log.warning("rejected %s of %s", errors, rows)
    for line in rig.lines():
        assert _QUOTED not in line


def test_two_dict_views_in_one_call_are_each_read(rig: _Rig) -> None:
    """Each view is read as its own content: a view's items are new tuples every time it is read."""
    rig.configure(None)
    rejected = {"signup": _errors_of(_validation_error())}
    accepted = {"city": "Bern"}
    rig.log.warning("rejected %s; accepted %s", rejected.items(), accepted.items())
    form_b = {"form_b": _errors_of(_validation_error())}
    rig.log.warning("rejected %s / %s", rejected.items(), form_b.items())
    first, second = rig.lines()
    assert _QUOTED not in first and "Bern" in first
    assert _QUOTED not in second and "form_b" in second


@dataclasses.dataclass
class _ImportRow:
    line: int
    error: object = None


class _ImportFailed(Exception):
    """An import error that names its row; the row keeps the error it failed with."""


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_an_error_and_the_row_that_refers_back_to_it_are_both_withheld(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    row = _ImportRow(7)
    err = _ImportFailed(row, _validation_error())
    row.error = err
    rig.log.warning("import failed: %s; row %s", err, row)
    rig.log.warning("import failed", extra={"error": err, "row": row})
    for line in rig.lines():
        assert _QUOTED not in line


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_client_error_beside_a_validation_error_keeps_its_text(
    rig: _Rig, text: bool
) -> None:
    """Each argument and field is judged on its own: a validation error in one withholds nothing
    in the others."""
    rig.configure(None, text=text)
    errors = _errors_of(_validation_error())
    rig.log.warning(
        "sync failed: %s; local: %s", _ClientError("503 for /api/sync"), errors
    )
    (line,) = rig.lines()
    assert _QUOTED not in line
    assert "503 for /api/sync" in line


def test_a_validation_error_after_a_batch_past_the_search_is_withheld(
    rig: _Rig,
) -> None:
    """A batch that spends the call's search leaves the arguments after it uncleared: each is
    walked, so a validation error after the batch is withheld too."""
    rig.configure(None)
    errors = _errors_of(_validation_error())
    rows = [{"device": n, "reading": float(n)} for n in range(60_000)]
    rig.log.warning("rejected %s then %s", rows, errors)
    rig.log.warning("rejected %s", [*rows, *errors])
    for line in rig.lines():
        assert _QUOTED not in line


# The other classes the module knows as validation errors: pydantic's v1 API, which pydantic 2
# still ships, and FastAPI's base of `RequestValidationError`.
_OTHER_PACKAGES = {
    "pydantic v1": ("pydantic.v1.error_wrappers", "ValidationError"),
    "fastapi": ("fastapi.exceptions", "ValidationException"),
}


@pytest.mark.parametrize("where", list(_OTHER_PACKAGES))
@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_validation_error_from_another_package_is_written_as_its_type(
    rig: _Rig, where: str, text: bool
) -> None:
    rig.configure(None, text=text)
    entry: dict[str, object] = {
        "type": "value_error",
        "loc": ("password",),
        "msg": f"{_QUOTED} is too weak",
    }
    if where == "fastapi":
        entry["input"] = _QUOTED  # pydantic v2's entries, as FastAPI hands them on
    err = _stand_in("Signup", entry, _OTHER_PACKAGES[where])
    rig.log.warning("refused: %s", err)
    try:
        raise err
    except ValueError:
        rig.log.exception("signup failed")
    for line in rig.lines():
        assert _QUOTED not in line


class _Unreadable(Mapping[str, object]):
    """A mapping whose store is gone (a closed shelf, a detached lazy collection): reading raises."""

    def __getitem__(self, key: str) -> object:
        raise LookupError("store closed")

    def __iter__(self) -> Iterator[str]:
        raise LookupError("store closed")

    def __len__(self) -> int:
        return 1


@pytest.mark.parametrize("text", [False, True], ids=["json", "text"])
def test_a_value_that_raises_when_read_is_written_as_its_type(
    rig: _Rig, text: bool
) -> None:
    rig.configure(None, text=text)
    rig.log.warning("state: %s", _Unreadable())
    rig.log.warning("%(state)s", _Unreadable())
    for line in rig.lines():
        assert "<_Unreadable: unreadable, withheld>" in line


def test_functions_classes_and_numbers_beside_a_validation_error_are_written_as_passed(
    rig: _Rig,
) -> None:
    rig.configure(None)
    rig.log.warning("refused: %s", [len, int, 2j, _validation_error()])
    message = json.loads(rig.lines()[0])["message"]
    assert _QUOTED not in message
    assert "[<built-in function len>, <class 'int'>, 2j, " in message
