"""CLI wiring for `interlis write-xtf` (backlog item 15)."""

from pathlib import Path

from interlis.cli import main
from interlis.cli_style import ExitCode

_VIEW_TOPIC_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS A =
      Attr: TEXT*20;
    END A;
  END Base;
  VIEW TOPIC Views =
    DEPENDS ON Foo.Base;
    VIEW VA
      PROJECTION OF Foo.Base.A;
      =
      ATTRIBUTE
        ALL OF A;
    END VA;
  END Views;
END Foo.
"""

_PLAIN_TOPIC_MODEL = _VIEW_TOPIC_MODEL.replace("VIEW TOPIC Views", "TOPIC Views")

_TWO_VIEW_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS A =
      Attr: TEXT*20;
    END A;
  END Base;
  VIEW TOPIC Views =
    DEPENDS ON Foo.Base;
    VIEW VA
      PROJECTION OF Foo.Base.A;
      =
      ATTRIBUTE
        ALL OF A;
    END VA;
    VIEW VB
      PROJECTION OF Foo.Base.A;
      =
      ATTRIBUTE
        ALL OF A;
    END VB;
  END Views;
END Foo.
"""

_XTF = """<?xml version="1.0" encoding="UTF-8"?><TRANSFER xmlns="http://www.interlis.ch/INTERLIS2.3">
<HEADERSECTION SENDER="t" VERSION="2.3"><MODELS><MODEL NAME="Foo" VERSION="1" URI="http://x"></MODEL></MODELS></HEADERSECTION>
<DATASECTION>
<Foo.Base BID="1">
<Foo.Base.A TID="a1"><Attr>hello</Attr></Foo.Base.A>
</Foo.Base>
</DATASECTION>
</TRANSFER>
"""


def _write(tmp_path: Path, model_text: str) -> tuple[Path, Path]:
    model_path = tmp_path / "Foo.ili"
    model_path.write_text(model_text, encoding="utf-8")
    xtf_path = tmp_path / "data.xtf"
    xtf_path.write_text(_XTF, encoding="utf-8")
    return model_path, xtf_path


def test_write_xtf_happy_path_prints_the_view_basket(tmp_path, capsys):
    model_path, xtf_path = _write(tmp_path, _VIEW_TOPIC_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path)]) == ExitCode.OK
    out = capsys.readouterr().out
    assert '<Foo.Views.VA TID="a1">' in out
    assert "<Attr>hello</Attr>" in out


def test_write_xtf_defaults_sender_rather_than_omitting_it(tmp_path, capsys):
    """A real ili2c-compiled schema marks HEADERSECTION's SENDER required (verified empirically, item 15) -
    --sender defaults to a real value instead of silently omitting the attribute."""
    model_path, xtf_path = _write(tmp_path, _VIEW_TOPIC_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path)]) == ExitCode.OK
    assert 'SENDER="interlis-runtime"' in capsys.readouterr().out


def test_write_xtf_rejects_a_view_declared_in_a_plain_topic(tmp_path, capsys):
    model_path, xtf_path = _write(tmp_path, _PLAIN_TOPIC_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path)]) == ExitCode.INVALID
    assert "not a VIEW TOPIC" in capsys.readouterr().err


def test_write_xtf_requires_view_flag_when_the_model_declares_several(tmp_path, capsys):
    model_path, xtf_path = _write(tmp_path, _TWO_VIEW_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path)]) == ExitCode.USAGE
    err = capsys.readouterr().err
    assert "--view" in err and "VA" in err and "VB" in err


def test_write_xtf_view_flag_picks_one_of_several(tmp_path, capsys):
    model_path, xtf_path = _write(tmp_path, _TWO_VIEW_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path), "--view", "VB"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert '<Foo.Views.VB TID="a1">' in out
    assert "Foo.Views.VA" not in out


def test_write_xtf_missing_model_file_exits_not_found(tmp_path, capsys):
    xtf_path = tmp_path / "data.xtf"
    xtf_path.write_text(_XTF, encoding="utf-8")

    assert main(["write-xtf", str(tmp_path / "nope.ili"), str(xtf_path)]) == ExitCode.NOT_FOUND
    assert capsys.readouterr().err.startswith("error: .ili file not found:")


def test_write_xtf_missing_xtf_file_exits_not_found(tmp_path, capsys):
    model_path = tmp_path / "Foo.ili"
    model_path.write_text(_VIEW_TOPIC_MODEL, encoding="utf-8")

    assert main(["write-xtf", str(model_path), str(tmp_path / "nope.xtf")]) == ExitCode.NOT_FOUND
    assert capsys.readouterr().err.startswith("error: .xtf file not found:")


def test_write_xtf_merge_with_source_includes_the_original_basket(tmp_path, capsys):
    model_path, xtf_path = _write(tmp_path, _VIEW_TOPIC_MODEL)

    assert main(["write-xtf", str(model_path), str(xtf_path), "--merge-with-source"]) == ExitCode.OK
    out = capsys.readouterr().out
    assert "<Foo.Base BID=" in out
    assert '<Foo.Views.VA TID="a1">' in out
