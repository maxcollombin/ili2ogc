"""CLI wiring tests - root-file eCH-0117 meta-attribute capture (technicalContact/CRS)."""

import json
from pathlib import Path

from interlis.cli import main

_ROOT_FIXTURES = Path(__file__).resolve().parent / "fixtures"

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
    """Real CLI wiring, not a direct class_to_json_schema() call - this file's own MODEL-level `!!@` comments must reach
    the output.
    """
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert", str(ili_path)]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["x-meta"] == {"technicalContact": "mailto:test@example.com", "IDGeoIV": "1.2.3"}


def test_convert_jsonfg_resolves_crs_declared_locally_not_via_import(tmp_path, capsys):
    """Same CLI wiring gap, .xtf side: a CoordType declared IN the root .ili (not imported) must still resolve a
    "place"/"coordRefSys".
    """
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
    """Same CLI wiring gap as convert/convert-jsonfg, SQL side: a CoordType declared IN the root .ili must resolve a
    real geometry column.
    """
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert-sql", str(ili_path)]) == 0
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "a" (' in ddl
    assert '"id" text UNIQUE NOT NULL' in ddl
    assert '"geom" geometry(Point, 2056) NOT NULL' in ddl


def test_convert_sql_dialect_gpkg_declares_everything_inline(tmp_path, capsys):
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert-sql", str(ili_path), "--dialect", "gpkg"]) == 0
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "a" (' in ddl
    assert '"geom" POINT NOT NULL' in ddl
    assert "ALTER TABLE" not in ddl
    assert "gpkg_geometry_columns" in ddl


_MAIN_MODEL_WITH_CATALOG_REF = """INTERLIS 2.4;
MODEL Main AT "http://x" VERSION "1" =
  IMPORTS Catalog;
  TOPIC T =
    CLASS Parcel =
      Zone : REFERENCE TO Catalog.CatTopic.ZoneCatalog;
    END Parcel;
  END T;
END Main.
"""

_CATALOG_MODEL = """INTERLIS 2.4;
MODEL Catalog AT "http://y" VERSION "1" =
  TOPIC CatTopic =
    CLASS ZoneCatalog =
      Code : MANDATORY TEXT*10;
    END ZoneCatalog;
  END CatTopic;
END Catalog.
"""


def test_convert_sql_cross_model_reference_target_on_repo_keeps_fk_automatically(tmp_path, capsys):
    """A REFERENCE TO a class in an imported model resolvable via --repo keeps the cross-model FK automatically."""
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    (tmp_path / "Catalog.ili").write_text(_CATALOG_MODEL, encoding="utf-8")

    assert main(["convert-sql", str(main_path), "--repo", str(tmp_path)]) == 0
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "zonecatalog" (' in ddl
    assert 'FOREIGN KEY ("zone") REFERENCES "zonecatalog" ("id")' in ddl
    assert "different model" not in ddl


def test_convert_sql_catalog_flag_still_works_when_target_model_is_not_on_repo(tmp_path, capsys):
    """`--catalog` remains the way in for a model not reachable via --repo (or wanted as tables regardless)."""
    (tmp_path / "Catalog.ili").write_text(_CATALOG_MODEL, encoding="utf-8")
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"

    assert (
        main(
            [
                "convert-sql",
                str(main_path),
                "--repo",
                str(tmp_path),
                "--catalog",
                str(catalog_path),
            ]
        )
        == 0
    )
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "zonecatalog" (' in ddl
    assert 'FOREIGN KEY ("zone") REFERENCES "zonecatalog" ("id")' in ddl


def test_convert_sql_catalog_flag_ignores_the_same_file_given_twice(tmp_path, capsys):
    """Passing the same --catalog file twice (or the same as the main file) must not duplicate its table."""
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"
    catalog_path.write_text(_CATALOG_MODEL, encoding="utf-8")

    assert (
        main(
            [
                "convert-sql",
                str(main_path),
                "--repo",
                str(tmp_path),
                "--catalog",
                str(catalog_path),
                "--catalog",
                str(catalog_path),
            ]
        )
        == 0
    )
    ddl = capsys.readouterr().out
    assert ddl.count('CREATE TABLE "zonecatalog"') == 1
    assert 'CREATE TABLE "zonecatalog_2"' not in ddl


_CATALOG_MODEL_WITH_EMBEDDED_ROLE = """INTERLIS 2.4;
MODEL Catalog AT "http://y" VERSION "1" =
  TOPIC CatTopic =
    CLASS Parent =
      Name : MANDATORY TEXT*10;
    END Parent;
    CLASS Child =
      Name : MANDATORY TEXT*10;
    END Child;
    ASSOCIATION Parent_Child =
      Children -- {0..*} Child;
      Parent -<#> {1} Parent;
    END Parent_Child;
  END CatTopic;
END Catalog.
"""


def test_convert_sql_catalog_flag_resolves_embedded_role_in_the_catalogues_own_model(tmp_path, capsys):
    """`_columns_for_class` must resolve a `--catalog` class's embedded association role against THAT model's own symbol
    table, not the root file's (docs/sql-conversion-strategy.md's "known, separate, pre-existing limitation").
    """
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"
    catalog_path.write_text(_CATALOG_MODEL_WITH_EMBEDDED_ROLE, encoding="utf-8")

    # Exit 2 (completed-with-degradations): this catalogue has no
    # `ZoneCatalog`, so Main.ili's `Zone` REFERENCE TO stays unresolved
    # (one SQL-REF-TARGET-UNRESOLVED -- NOTE) - unrelated to the embedded
    # role under test.
    assert (
        main(
            [
                "convert-sql",
                str(main_path),
                "--repo",
                str(tmp_path),
                "--catalog",
                str(catalog_path),
            ]
        )
        == 2
    )
    ddl = capsys.readouterr().out
    # `Parent_Child` embeds role `Parent` (the {1} end) onto `Child` (the
    # {0..*} end) - entirely declared inside Catalog.ili, invisible from
    # Main.ili's own symbol table. A `"parent"` FK column on `child`
    # proves it was resolved against the catalogue's OWN table.
    assert 'CREATE TABLE "child" (' in ddl
    assert '"parent" text' in ddl
    assert 'FOREIGN KEY ("parent") REFERENCES "parent" ("id")' in ddl


_BASE_MODEL_FOR_VIEW = """INTERLIS 2.4;
MODEL RoadsBase AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Road =
      RoadName : MANDATORY TEXT*40;
    END Road;
    CLASS Segment =
      SegNr : MANDATORY 0 .. 999;
      OfRoad : MANDATORY REFERENCE TO Road;
    END Segment;
  END T;
END RoadsBase.
"""

_DERIVED_VIEW_MODEL = """INTERLIS 2.4;
MODEL RoadsView AT "http://x" VERSION "1" =
  IMPORTS RoadsBase;
  TOPIC D =
    DEPENDS ON RoadsBase.T;
    VIEW v_seg
      JOIN OF Segment ~ RoadsBase.T.Segment, Road ~ RoadsBase.T.Road;
      WHERE Segment -> OfRoad == Road;
      =
      ATTRIBUTE
        nr := Segment -> SegNr;
        name := Road -> RoadName;
    END v_seg;
  END D;
END RoadsView.
"""


def test_convert_sql_auto_includes_a_views_base_model_from_repo(tmp_path, capsys):
    """A VIEW's base classes come from the imported model - --repo alone builds its tables and the CREATE VIEW."""
    (tmp_path / "RoadsBase.ili").write_text(_BASE_MODEL_FOR_VIEW, encoding="utf-8")
    derived = tmp_path / "RoadsView.ili"
    derived.write_text(_DERIVED_VIEW_MODEL, encoding="utf-8")

    assert main(["convert-sql", str(derived), "--repo", str(tmp_path)]) == 0
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "road" (' in ddl  # the imported base model's tables, auto-included
    assert 'CREATE TABLE "segment" (' in ddl
    assert 'CREATE VIEW "v_seg" AS' in ddl
    assert '"segment"."ofroad" = "road"."id"' in ddl
    assert "-- NOTE (view v_seg)" not in ddl


# --- shared diagnostics core (Lot 2): --output-format / --report / --strict / exit codes ---

_MODEL_WITH_UNSUPPORTED_CHECK = """INTERLIS 2.4;
MODEL Deg AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Name : MANDATORY TEXT*40;
      MANDATORY CONSTRAINT INTERLIS.len(Name) > 2;
    END A;
  END T;
END Deg.
"""


def test_convert_sql_clean_model_exits_0_with_no_diagnostics(tmp_path, capsys):
    ili_path = tmp_path / "Foo.ili"
    ili_path.write_text(_MODEL_WITH_META, encoding="utf-8")

    assert main(["convert-sql", str(ili_path)]) == 0
    err = capsys.readouterr().err
    assert "diagnostic(s)" not in err


def test_convert_sql_degraded_model_exits_2_and_lists_the_rule_id(tmp_path, capsys):
    ili_path = tmp_path / "Deg.ili"
    ili_path.write_text(_MODEL_WITH_UNSUPPORTED_CHECK, encoding="utf-8")

    assert main(["convert-sql", str(ili_path)]) == 2
    out = capsys.readouterr()
    assert "-- NOTE" in out.out and "[SQL-CHECK-EXPR-UNSUPPORTED]" in out.out
    assert "note SQL-CHECK-EXPR-UNSUPPORTED" in out.err
    assert out.err.strip().endswith("1 diagnostic(s): 0 errors, 0 warnings, 1 note")


def test_strict_promotes_a_note_to_a_failure(tmp_path, capsys):
    ili_path = tmp_path / "Deg.ili"
    ili_path.write_text(_MODEL_WITH_UNSUPPORTED_CHECK, encoding="utf-8")

    assert main(["convert-sql", str(ili_path), "--strict"]) == 1


def test_output_format_sarif_writes_a_valid_log_to_stderr(tmp_path, capsys):
    from jsonschema import Draft4Validator

    schema = json.loads((_ROOT_FIXTURES / "sarif-2.1.0-schema.json").read_text())
    ili_path = tmp_path / "Deg.ili"
    ili_path.write_text(_MODEL_WITH_UNSUPPORTED_CHECK, encoding="utf-8")

    assert main(["convert-sql", str(ili_path), "--output-format", "sarif"]) == 2
    err = capsys.readouterr().err
    log = json.loads(err)
    Draft4Validator(schema).validate(log)
    assert log["runs"][0]["results"][0]["ruleId"] == "SQL-CHECK-EXPR-UNSUPPORTED"


def test_report_writes_a_sidecar_regardless_of_output_format(tmp_path, capsys):
    ili_path = tmp_path / "Deg.ili"
    ili_path.write_text(_MODEL_WITH_UNSUPPORTED_CHECK, encoding="utf-8")
    report = tmp_path / "report.sarif"

    assert main(["convert-sql", str(ili_path), "--report", str(report)]) == 2
    log = json.loads(report.read_text())
    assert log["version"] == "2.1.0"
    assert {r["id"] for r in log["runs"][0]["tool"]["driver"]["rules"]} == {"SQL-CHECK-EXPR-UNSUPPORTED"}


def test_class_c_message_names_repo_or_catalog_as_the_fix(tmp_path, capsys):
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")

    assert main(["convert-sql", str(main_path), "--repo", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "warning SQL-REF-TARGET-UNRESOLVED" in err
    assert "--repo" in err and "--catalog" in err
