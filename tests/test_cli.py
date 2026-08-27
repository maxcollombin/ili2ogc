"""CLI wiring tests - root-file eCH-0117 meta-attribute capture (technicalContact/CRS)."""
import json

from interlis.cli import main

_MODEL_WITH_META = """INTERLIS 2.4;
!!@technicalContact=mailto:test@example.com
!!@IDGeoIV=1.2.3
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:2056
    Coord2D = COORD 2000000.000 .. 3000000.000, 1000000.000 .. 1400000.000;
  TOPIC T =
    CLASS A =
      Geom : MANDATORY Coord2D;
    END A;
  END T;
END Foo.
"""

_XTF = """<?xml version="1.0" encoding="UTF-8"?><TRANSFER xmlns="http://www.interlis.ch/INTERLIS2.3">
<HEADERSECTION SENDER="test-fixture" VERSION="2.3"><MODELS><MODEL NAME="Foo" VERSION="1" URI="http://x"></MODEL></MODELS></HEADERSECTION>
<DATASECTION>
<Foo.T BID="1">
<Foo.T.A TID="obj-1"><Geom><COORD><C1>2600000.0</C1><C2>1200000.0</C2></COORD></Geom></Foo.T.A>
</Foo.T>
</DATASECTION>
</TRANSFER>
"""


def test_convert_surfaces_model_level_meta_attributes_as_x_meta(tmp_path, capsys):
    """Real CLI wiring, not a direct class_to_json_schema() call - this file's own MODEL-level `!!@` comments must reach the output."""
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert", str(ili_path)]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["x-meta"] == {"technicalContact": "mailto:test@example.com", "IDGeoIV": "1.2.3"}


def test_convert_jsonfg_resolves_crs_declared_locally_not_via_import(tmp_path, capsys):
    """Same CLI wiring gap, .xtf side: a CoordType declared IN the root .ili (not imported) must still resolve a "place"/"coordRefSys"."""
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")
    xtf_path = tmp_path / "data.xtf"
    xtf_path.write_text(_XTF, encoding="utf-8")

    assert main(["convert-jsonfg", str(xtf_path), "--model", str(ili_path)]) == 0
    collection = json.loads(capsys.readouterr().out)
    feature = collection["features"][0]
    assert feature["place"] == {"type": "Point", "coordinates": [2600000.0, 1200000.0]}
    # Hoisted to the collection (transfer_to_feature_collection): the only
    # Feature here has exactly one "coordRefSys" value.
    assert collection["coordRefSys"] == "http://www.opengis.net/def/crs/EPSG/0/2056"


def test_convert_sql_resolves_crs_declared_locally_not_via_import(tmp_path, capsys):
    """Same CLI wiring gap as convert/convert-jsonfg, SQL side: a CoordType declared IN the root .ili must resolve a real geometry column."""
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert-sql", str(ili_path)]) == 0
    ddl = capsys.readouterr().out
    assert "CREATE TABLE a (" in ddl
    assert "ogc_fid text PRIMARY KEY" in ddl
    # Not "... NOT NULL": MANDATORY on a NAMED domain reference (Coord2D)
    # doesn't reach resolve_attribute's mandatory flag - a known,
    # orthogonal gap, see docs/sql-conversion-strategy.md.
    assert "geom geometry(Point, 2056)" in ddl
