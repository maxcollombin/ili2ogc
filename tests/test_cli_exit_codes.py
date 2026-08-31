"""`interlis.cli_style`: `ExitCode` for a hard stop before any `DiagnosticBag` exists, and the color helpers.

Before this module these paths (missing file, unparseable input, an
incomplete invocation) all returned a bare `1`, indistinguishable from a
`DiagnosticBag` failure and never asserted by any test - see
docs/diagnostics.md for why 0/1/2 stay reserved for the bag's own axis.
"""

from io import StringIO

from interlis.cli import main
from interlis.cli_style import ExitCode, error, use_color, warn

_MINIMAL_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Name : TEXT*20;
    END A;
  END T;
END Foo.
"""


def test_convert_missing_file_exits_not_found(tmp_path, capsys):
    assert main(["convert", str(tmp_path / "nope.ili")]) == ExitCode.NOT_FOUND
    assert capsys.readouterr().err.startswith("error: file not found:")


def test_convert_syntax_error_exits_invalid(tmp_path, capsys):
    bad = tmp_path / "bad.ili"
    bad.write_text("not ili at all", encoding="utf-8")
    assert main(["convert", str(bad)]) == ExitCode.INVALID
    assert "syntax error" in capsys.readouterr().err


def test_convert_sql_missing_catalog_exits_not_found(tmp_path, capsys):
    model = tmp_path / "Foo.ili"
    model.write_text(_MINIMAL_MODEL, encoding="utf-8")
    assert main(["convert-sql", str(model), "--catalog", str(tmp_path / "nope.ili")]) == ExitCode.NOT_FOUND
    assert "--catalog file not found" in capsys.readouterr().err


def test_validate_without_model_or_repo_exits_usage(tmp_path, capsys):
    xtf = tmp_path / "empty.xtf"
    xtf.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><TRANSFER xmlns="http://www.interlis.ch/INTERLIS2.3">'
        "<HEADERSECTION SENDER='t' VERSION='2.3'/><DATASECTION/></TRANSFER>",
        encoding="utf-8",
    )
    assert main(["validate", str(xtf)]) == ExitCode.USAGE
    assert "--repo is required" in capsys.readouterr().err


def test_convert_jsonfg_missing_xtf_exits_not_found(tmp_path, capsys):
    assert main(["convert-jsonfg", str(tmp_path / "nope.xtf")]) == ExitCode.NOT_FOUND
    assert capsys.readouterr().err.startswith("error: .xtf file not found:")


class _FakeStream(StringIO):
    def __init__(self, is_a_tty: bool) -> None:
        super().__init__()
        self._is_a_tty = is_a_tty

    def isatty(self) -> bool:
        return self._is_a_tty


def test_use_color_true_only_on_a_real_terminal_without_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert use_color(_FakeStream(True)) is True
    assert use_color(_FakeStream(False)) is False


def test_use_color_false_when_no_color_is_set_even_on_a_tty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color(_FakeStream(True)) is False


def test_error_and_warn_write_to_the_given_stream_uncolored_when_not_a_tty():
    stream = _FakeStream(False)
    error("boom", stream=stream)
    warn("careful", stream=stream)
    assert stream.getvalue() == "error: boom\nwarning: careful\n"


def test_error_writes_to_sys_stderr_looked_up_at_call_time(capsys):
    """Regression: a default param bound to `sys.stderr` at import time would miss `capsys`'s later swap."""
    error("boom")
    assert capsys.readouterr().err == "error: boom\n"
