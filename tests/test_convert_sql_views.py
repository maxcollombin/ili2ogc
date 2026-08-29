"""Backlog item 14, Lot 2 - `.ili` VIEW -> SQL `CREATE VIEW`.

A `Projection`/`Join` VIEW becomes `SELECT <attr := path> ... FROM <base
tables, comma-joined> WHERE <translated Where predicates>` - see
`docs/sql-conversion-strategy.md` and `mappings/ilismeta16-to-sql-rules.yml`
(`View` concept). A VIEW whose bases aren't built, or whose expressions
fall outside the translatable subset, is emitted as a `-- NOTE`, never a
half-built statement (RULE #5).

The `tests/fixtures/fgdm4gs/` derived models are the real-corpus anchor,
but their base models pull in external imports (`GeometryCHLV95_V2` etc.)
this hermetic test can't resolve, so a fully executable `CREATE VIEW`
there would need network model downloads - the self-contained model below
carries the same shapes (a `JOIN OF ... WHERE role == base`, a
`PROJECTION OF`, a reference hop) and is verified end-to-end against a
real SQLite engine.
"""

import sqlite3
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import meta_attribute_comments, parse_file, parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FGDM4GS = Path(__file__).resolve().parent / "fixtures" / "fgdm4gs"


_MODEL = """INTERLIS 2.4;
MODEL RoadView AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:2056
    C2 = COORD 0.000 .. 999999.999, 0.000 .. 999999.999;
  TOPIC Base =
    CLASS Road =
      RoadName : MANDATORY TEXT*40;
      Owner : TEXT*20;
    END Road;
    CLASS Segment =
      SegNr : MANDATORY 0 .. 99999;
      Geom : MANDATORY C2;
      OfRoad : MANDATORY REFERENCE TO Road;
    END Segment;
  END Base;
  TOPIC Derived =
    DEPENDS ON RoadView.Base;
    VIEW RoadSegments
      JOIN OF Segment ~ RoadView.Base.Segment, Road ~ RoadView.Base.Road;
      WHERE
        Segment -> OfRoad == Road;
      =
      ATTRIBUTE
        geom := Segment -> Geom;
        segnr := Segment -> SegNr;
        road_name := Road -> RoadName;
        owner_name := Road -> Owner;
    END RoadSegments;
    VIEW SegmentRoad
      PROJECTION OF RoadView.Base.Segment;
      =
      ATTRIBUTE
        geom := Segment -> Geom;
        road_name := Segment -> OfRoad -> RoadName;
    END SegmentRoad;
  END Derived;
END RoadView.
"""


def _build_model():
    tree, errors = parse_text(_MODEL)
    assert not errors, errors
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attribute_comments(_MODEL))
    return builder


def _split(builder):
    registered = builder.symbol_table.all_registered()
    classes = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".Class")]
    views = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".View")]
    ctn: dict[int, str] = {}
    tables = build_tables(classes, symbol_table=builder.symbol_table, class_table_names=ctn)
    sql_views = build_views(views, tables, symbol_table=builder.symbol_table, class_table_names=ctn)
    return tables, sql_views


def _view(sql_views, name):
    return next(v for v in sql_views if v.name == name)


def test_join_view_body_is_a_select_over_the_base_tables():
    _tables, sql_views = _split(_build_model())
    view = _view(sql_views, "roadsegments")
    assert view.body is not None, view.notes
    body = view.body
    assert '"segment"."geom" AS "geom"' in body
    assert '"road"."roadname" AS "road_name"' in body
    assert 'FROM "segment" "segment", "road" "road"' in body
    assert '"segment"."ofroad" = "road"."id"' in body


def test_projection_view_navigates_a_reference_hop_with_an_extra_join():
    _tables, sql_views = _split(_build_model())
    view = _view(sql_views, "segmentroad")
    assert view.body is not None, view.notes
    body = view.body
    # `Segment -> OfRoad -> RoadName` crosses the OfRoad reference: an extra
    # join table + its ON condition, not a second base.
    assert '"ofroad" = "j1_road"."id"' in body
    assert '"j1_road"."roadname" AS "road_name"' in body


def test_join_view_executes_against_real_sqlite():
    tables, sql_views = _split(_build_model())
    schema = render_gpkg(tables).split("\nINSERT INTO gpkg_contents")[0]
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for v in sql_views:
        assert v.body is not None, v.notes
        conn.execute(f'CREATE VIEW "{v.name}" AS {v.body}')
    conn.execute("INSERT INTO road (id, roadname, owner) VALUES ('r1', 'Main St', 'City')")
    conn.execute("INSERT INTO road (id, roadname, owner) VALUES ('r2', 'Side St', 'Canton')")
    conn.execute("INSERT INTO segment (id, segnr, geom, ofroad) VALUES ('s1', 1, 'x', 'r1')")
    conn.execute("INSERT INTO segment (id, segnr, geom, ofroad) VALUES ('s2', 2, 'x', 'r1')")
    conn.execute("INSERT INTO segment (id, segnr, geom, ofroad) VALUES ('s3', 3, 'x', 'r2')")
    rows = sorted(conn.execute("SELECT segnr, road_name, owner_name FROM roadsegments"))
    assert rows == [(1, "Main St", "City"), (2, "Main St", "City"), (3, "Side St", "Canton")]
    proj = sorted(conn.execute("SELECT road_name FROM segmentroad"))
    assert proj == [("Main St",), ("Main St",), ("Side St",)]


def test_unbuilt_base_table_demotes_the_view_to_a_note():
    """A VIEW built without its base classes among `tables` yields a note, not a broken CREATE VIEW."""
    builder = _build_model()
    views = [i for i in builder.symbol_table.all_registered() if getattr(i, "_qualified_class", "").endswith(".View")]
    sql_views = build_views(views, [], symbol_table=builder.symbol_table)
    assert all(v.body is None for v in sql_views)
    assert any("not built" in v.notes[0] for v in sql_views)


def _convert_with_auto_base_models(derived: str):
    """Mirror `cli.cmd_convert_sql`'s auto base-model folding, without --catalog.

    Build the derived model, then fold in every base model `build()`
    actually resolved (via the repository) so the VIEW's base classes
    become tables in the SAME conversion.
    """
    tree, errors = parse_file(FGDM4GS / derived)
    assert not errors, errors
    repo = ModelRepository([FGDM4GS])
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repo)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)

    registered = builder.symbol_table.all_registered()
    classes = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".Class")]
    views = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".View")]
    class_symbol_tables: dict[int, object] = {}
    already = {id(c) for c in classes}
    view_base_ids = {
        id(rbv.BaseView)
        for v in views
        for rbv in (getattr(v, "RenamedBaseView", None) or [])
        if isinstance(getattr(rbv, "BaseView", None), MetaInstance)
    }
    for model_table in repo.loaded_models().values():
        model_classes = [
            i for i in model_table.all_registered() if getattr(i, "_qualified_class", "").endswith(".Class")
        ]
        if not any(id(c) in view_base_ids for c in model_classes):
            continue
        for c in model_classes:
            if id(c) not in already:
                already.add(id(c))
                classes.append(c)
                class_symbol_tables.setdefault(id(c), model_table)

    ctn: dict[int, str] = {}
    tables = build_tables(
        classes,
        symbol_table=builder.symbol_table,
        class_symbol_tables=class_symbol_tables,
        class_table_names=ctn,
    )
    sql_views = build_views(
        views,
        tables,
        symbol_table=builder.symbol_table,
        class_symbol_tables=class_symbol_tables,
        class_table_names=ctn,
    )
    return tables, sql_views


@pytest.mark.parametrize("derived", ["Planungszonen_V2_d_A.ili", "IVS_V3_d.ili"])
def test_fgdm4gs_derived_view_auto_includes_its_base_model_tables(derived):
    """The derived VIEW model's base classes (in the IMPORTED model) become tables automatically.

    The base models' OWN external imports (`GeometryCHLV95_V2` etc.) aren't
    in this hermetic fixture repo, so the geometry column the VIEW selects
    is absent and the VIEW honestly degrades to a `-- NOTE` - but the base
    tables themselves are built (proving the auto-inclusion), and nothing
    crashes. With the full model chain resolvable (a real `--repo`), the
    same code produces a complete, executable `CREATE VIEW` - see
    docs/sql-conversion-strategy.md.
    """
    tables, sql_views = _convert_with_auto_base_models(derived)
    table_names = {t.name for t in tables}
    assert len(table_names) > 3  # the imported model's classes, not just the (class-less) derived model
    assert sql_views
    for v in sql_views:
        assert v.body is None and v.notes
        assert "not built" not in v.notes[0]  # the base tables ARE built; only the geometry column is missing
