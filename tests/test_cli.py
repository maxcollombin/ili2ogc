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


def test_convert_sql_catalog_flag_keeps_cross_model_foreign_key(tmp_path, capsys):
    """`--catalog` closes build_tables()'s own documented cross-model FK-drop: without it, `zone` keeps its column but loses its FOREIGN KEY (target table not in this conversion's own output)."""
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"
    catalog_path.write_text(_CATALOG_MODEL, encoding="utf-8")

    # Without --catalog: column kept, FK dropped with a note.
    assert main(["convert-sql", str(main_path), "--repo", str(tmp_path)]) == 0
    ddl_without = capsys.readouterr().out
    assert '"zone" text' in ddl_without
    assert "ADD CONSTRAINT" not in ddl_without  # the actual FK constraint statement, not the "-- NOTE" mentioning it
    assert "different model" in ddl_without

    # With --catalog: the catalogue's own table is created in the SAME
    # conversion, so the FK constraint is kept too.
    assert main([
        "convert-sql", str(main_path), "--repo", str(tmp_path), "--catalog", str(catalog_path),
    ]) == 0
    ddl_with = capsys.readouterr().out
    assert 'CREATE TABLE "zonecatalog" (' in ddl_with
    assert 'FOREIGN KEY ("zone") REFERENCES "zonecatalog" ("id")' in ddl_with


def test_convert_sql_catalog_flag_ignores_the_same_file_given_twice(tmp_path, capsys):
    """Passing the same --catalog file twice (or the same as the main file) must not duplicate its table."""
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"
    catalog_path.write_text(_CATALOG_MODEL, encoding="utf-8")

    assert main([
        "convert-sql", str(main_path), "--repo", str(tmp_path),
        "--catalog", str(catalog_path), "--catalog", str(catalog_path),
    ]) == 0
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
    """`_columns_for_class` must resolve a `--catalog` class's embedded association role against THAT model's own symbol table, not the root file's (docs/sql-conversion-strategy.md's "known, separate, pre-existing limitation")."""
    main_path = tmp_path / "Main.ili"
    main_path.write_text(_MAIN_MODEL_WITH_CATALOG_REF, encoding="utf-8")
    catalog_path = tmp_path / "Catalog.ili"
    catalog_path.write_text(_CATALOG_MODEL_WITH_EMBEDDED_ROLE, encoding="utf-8")

    assert main([
        "convert-sql", str(main_path), "--repo", str(tmp_path), "--catalog", str(catalog_path),
    ]) == 0
    ddl = capsys.readouterr().out
    # `Parent_Child` embeds role `Parent` (the {1} end) onto `Child` (the
    # {0..*} end) - entirely declared inside Catalog.ili, invisible from
    # Main.ili's own symbol table. A `"parent"` FK column on `child`
    # proves it was resolved against the catalogue's OWN table.
    assert 'CREATE TABLE "child" (' in ddl
    assert '"parent" text' in ddl
    assert 'FOREIGN KEY ("parent") REFERENCES "parent" ("id")' in ddl
