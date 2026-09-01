"""Every INTERLIS `VIEW` FormationKind, across all three converters.

`tests/fixtures/views/*.ili` are the reference-manual canonical examples
for `UNION OF`, `AGGREGATION OF` and `INSPECTION OF` (also quoted in the
FGDM4GS report, HEIG-VD 2024, chapter 4.2), plus a few translatable
variants: `AGGREGATION OF` with columns that are plain key projections
rather than a FUNCTION over the implicit `AGGREGATES` bag (`_key`), that
navigate a REFERENCE TO hop (`_key_reference`), or that use a standard
`INTERLIS.objectCount`/`elementCount(AGGREGATES)` column alongside
`EQUAL(key)` (`_count`) or `ALL` (`_count_all`) - a USER function
(`countB`, the reference manual's own example) stays untranslated either
way; `INSPECTION OF` adding a `PARENT->` view attribute (`_parent`), an
indirect multi-hop path (`_nested`), or a single-hop SURFACE geometry
attribute decomposed to its boundary (`inspection_of_surface.ili`,
`!!@CRS`-annotated - `_build` below wires `meta_attributes` in for it);
`UNION OF` with a branch attribute navigating a REFERENCE TO hop
(`_reference`); `PROJECTION OF` an embedded 2-role ASSOCIATION
(`projection_of_association.ili`, self-contained shape of the real
`Planungszonen_V2_d_B.ili`/`TypPZ_Planungszone`).

What each converter is expected to do per kind is documented in
docs/view-formation-support.md; this test pins it:

- `convert` (JSON Schema): a `$def` for every kind, GET-only `x-crud`
  for anything but a plain `PROJECTION OF`.
- `convert-sql` (DDL): `UNION ALL` for Union, a `SELECT` over the child
  table for Inspection, `SELECT DISTINCT` for a projection-only
  Aggregation, a `-- NOTE` for an Aggregation whose column is a user
  FUNCTION. Every emitted `CREATE VIEW` runs against live SQLite.
- `convert-jsonfg` (data): concatenated + per-base-remapped rows for
  Union, one Feature per structure element for Inspection, de-duplicated
  rows for a projection-only Aggregation; a skip diagnostic for the
  FUNCTION Aggregation.
"""

import sqlite3
from pathlib import Path

from conftest import build_from_file

from interlis.convert.jsonfg import evaluate_view, unsupported_view_reason
from interlis.convert.jsonschema import model_to_json_schema
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import meta_attribute_comments_in_file
from interlis.xtf.parse import parse_xtf

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "views"


def _build(name: str):
    path = FIXTURES / f"{name}.ili"
    return build_from_file(path, meta_attributes=meta_attribute_comments_in_file(path))


def _registered(builder, suffix):
    return [
        inst
        for inst in builder.symbol_table.all_registered()
        if isinstance(inst, MetaInstance) and inst._qualified_class == f"IlisMeta16.ModelData.{suffix}"
    ]


def _schema(builder):
    return model_to_json_schema(
        _registered(builder, "Class") + _registered(builder, "View"),
        symbol_table=builder.symbol_table,
    )


def _sql_views(builder):
    tables = build_tables(_registered(builder, "Class"), symbol_table=builder.symbol_table)
    views = build_views(_registered(builder, "View"), tables, symbol_table=builder.symbol_table)
    return tables, views


def _run_ddl(tables, views):
    """Create every table + `CREATE VIEW` body against live SQLite - proves the DDL compiles."""
    con = sqlite3.connect(":memory:")
    con.executescript(render_gpkg(tables).split("\nINSERT INTO gpkg_contents")[0])
    for view in views:
        if view.body is not None:
            con.execute(f'CREATE VIEW "{view.name}" AS {view.body}')
    return con


def _features(builder, xtf_name):
    view = _registered(builder, "View")[0]
    transfer = parse_xtf(FIXTURES / "xtf" / f"{xtf_name}.xtf")
    return evaluate_view(view, transfer, symbol_table=builder.symbol_table)


# --- UNION OF --------------------------------------------------------------


def test_union_json_schema_has_the_union_attribute_and_is_get_only():
    schema = _schema(_build("union_of"))
    assert set(schema["$defs"]) == {"C1", "C2", "CC"}
    assert set(schema["$defs"]["CC"]["properties"]) == {"Attr1"}
    assert schema["$defs"]["CC"]["x-crud"] == ["GET"]


def test_union_sql_is_union_all_and_runs():
    builder = _build("union_of")
    _tables, views = _sql_views(builder)
    (cc,) = views
    assert cc.body is not None and "UNION ALL" in cc.body
    con = _run_ddl(_tables, views)
    con.execute('INSERT INTO "c1" ("id", "attr1") VALUES (1, ?)', ("alpha",))
    con.execute('INSERT INTO "c2" ("id", "attr2") VALUES (1, ?)', ("gamma",))
    assert sorted(r[0] for r in con.execute('SELECT "attr1" FROM "cc"')) == ["alpha", "gamma"]


def test_union_jsonfg_concatenates_and_remaps_each_base():
    feats = _features(_build("union_of"), "union_of")
    assert [f["properties"].get("Attr1") for f in feats] == ["alpha", "beta", "gamma"]
    assert {f["featureType"] for f in feats} == {"CC"}


def test_union_sql_joins_a_branch_attribute_that_navigates_a_reference():
    builder = _build("union_of_reference")
    tables, views = _sql_views(builder)
    (cc,) = views
    assert cc.body is not None
    assert 'FROM "c1" "c1", "d" "j1_d"' in cc.body
    assert '"c1"."refd" = "j1_d"."id"' in cc.body
    con = _run_ddl(tables, views)
    con.execute('INSERT INTO "d" ("id", "name") VALUES (1, ?)', ("delta",))
    con.execute('INSERT INTO "c1" ("id", "refd") VALUES (1, 1)')
    con.execute('INSERT INTO "c2" ("id", "attr2") VALUES (1, ?)', ("gamma",))
    assert sorted(r[0] for r in con.execute('SELECT "attr1" FROM "cc"')) == ["delta", "gamma"]


# --- INSPECTION OF -------------------------------------------------------


def test_inspection_json_schema_projects_the_element_attribute():
    schema = _schema(_build("inspection_of"))
    assert schema["$defs"]["VB"]["properties"] == {"Attr1": {"type": "string", "maxLength": 20}}
    assert schema["$defs"]["VB"]["x-crud"] == ["GET"]


def test_inspection_sql_selects_over_the_child_table_and_runs():
    builder = _build("inspection_of")
    _tables, views = _sql_views(builder)
    (vb,) = views
    assert vb.body is not None and 'FROM "b_attr2"' in vb.body
    con = _run_ddl(_tables, views)
    con.execute('INSERT INTO "b" ("id") VALUES (1)')
    con.execute('INSERT INTO "b_attr2" ("id", "b_fk", "attr1") VALUES (1, 1, ?)', ("first",))
    con.execute('INSERT INTO "b_attr2" ("id", "b_fk", "attr1") VALUES (2, 1, ?)', ("second",))
    assert sorted(r[0] for r in con.execute('SELECT "attr1" FROM "vb"')) == ["first", "second"]


def test_inspection_jsonfg_is_one_feature_per_structure_element():
    feats = _features(_build("inspection_of"), "inspection_of")
    assert [f["properties"]["Attr1"] for f in feats] == ["first", "second", "third"]


def test_inspection_sql_resolves_parent_arrow_by_joining_back_to_the_base_table():
    builder = _build("inspection_of_parent")
    _tables, views = _sql_views(builder)
    (vb,) = views
    assert vb.body is not None
    assert 'FROM "b_attr2" "insp"' in vb.body
    assert 'JOIN "b" "b" ON "insp"."b_fk" = "b"."id"' in vb.body
    con = _run_ddl(_tables, views)
    con.execute('INSERT INTO "b" ("id", "name") VALUES (1, ?)', ("owner-1",))
    con.execute('INSERT INTO "b_attr2" ("id", "b_fk", "attr1") VALUES (1, 1, ?)', ("first",))
    con.execute('INSERT INTO "b_attr2" ("id", "b_fk", "attr1") VALUES (2, 1, ?)', ("second",))
    rows = sorted(con.execute('SELECT "attr1", "ownername" FROM "vb"'))
    assert rows == [("first", "owner-1"), ("second", "owner-1")]


def test_inspection_sql_resolves_an_indirect_multi_hop_path_via_a_nested_child_table():
    builder = _build("inspection_of_nested")
    tables, views = _sql_views(builder)
    (vb,) = views
    assert vb.body is not None
    assert 'FROM "b_attr2_attr3" "insp"' in vb.body
    table_names = {t.name for t in tables}
    assert {"b", "b_attr2", "b_attr2_attr3"} <= table_names
    con = _run_ddl(tables, views)
    con.execute('INSERT INTO "b" ("id") VALUES (1)')
    con.execute('INSERT INTO "b_attr2" ("id", "b_fk", "attr1") VALUES (1, 1, ?)', ("first",))
    con.execute('INSERT INTO "b_attr2_attr3" ("id", "b_attr2_fk", "attr4") VALUES (1, 1, ?)', ("alpha",))
    con.execute('INSERT INTO "b_attr2_attr3" ("id", "b_attr2_fk", "attr4") VALUES (2, 1, ?)', ("beta",))
    assert sorted(r[0] for r in con.execute('SELECT "attr4" FROM "vb"')) == ["alpha", "beta"]


def test_inspection_jsonfg_resolves_an_indirect_multi_hop_path_too():
    feats = _features(_build("inspection_of_nested"), "inspection_of_nested")
    assert [f["properties"]["Attr4"] for f in feats] == ["alpha", "beta"]


def test_inspection_sql_resolves_a_single_hop_surface_geometry_to_its_boundary():
    builder = _build("inspection_of_surface")
    tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None
    assert v.body == 'SELECT\n    ST_Boundary("zone"."geometrie") AS "boundary"\nFROM "zone" "zone"'
    # ST_Boundary is OGC SFA/PostGIS SQL - CREATE VIEW compiles against plain
    # SQLite (lazy function resolution) but SELECT-ing from it needs SpatiaLite
    # loaded, not available in this hermetic test - see the function's own
    # docstring and docs/sql-conversion-strategy.md.
    con = _run_ddl(tables, views)
    (name,) = con.execute("SELECT name FROM sqlite_master WHERE type='view' AND name='zoneboundary'").fetchone()
    assert name == "zoneboundary"


def test_inspection_jsonfg_resolves_a_single_hop_surface_geometry_to_its_boundary():
    feats = _features(_build("inspection_of_surface"), "inspection_of_surface")
    (feature,) = feats
    assert feature["properties"]["Boundary"] == {
        "type": "LineString",
        "coordinates": [
            [2600000.0, 1200000.0],
            [2600100.0, 1200000.0],
            [2600100.0, 1200100.0],
            [2600000.0, 1200000.0],
        ],
    }


# --- AGGREGATION OF -----------------------------------------------------


def test_aggregation_function_column_json_schema_keeps_the_declared_type():
    schema = _schema(_build("aggregation_of"))
    assert schema["$defs"]["VB2"]["properties"]["ElementCount"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10000,
    }
    assert schema["$defs"]["VB2"]["x-crud"] == ["GET"]


def test_aggregation_function_column_sql_is_a_note_not_a_broken_view():
    builder = _build("aggregation_of")
    _tables, views = _sql_views(builder)
    (vb2,) = views
    assert vb2.body is None
    assert any("SQL-VIEW-FORMATION-UNSUPPORTED" in n and "AGGREGATES" in n for n in vb2.notes)


def test_aggregation_function_column_jsonfg_is_skipped_with_a_reason():
    view = _registered(_build("aggregation_of"), "View")[0]
    reason = unsupported_view_reason(view)
    assert reason is not None and "function engine" in reason


def test_aggregation_projection_only_sql_groups_by_the_stashed_key_and_runs():
    builder = _build("aggregation_key")
    _tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None and "GROUP BY" in v.body and not v.body.startswith("SELECT DISTINCT")
    con = _run_ddl(_tables, views)
    con.executemany(
        'INSERT INTO "parcel" ("id", "municipality") VALUES (?, ?)',
        [(1, "Lausanne"), (2, "Lausanne"), (3, "Renens")],
    )
    assert sorted(r[0] for r in con.execute('SELECT "municipality" FROM "municipalitylist"')) == ["Lausanne", "Renens"]


def test_aggregation_projection_only_jsonfg_deduplicates():
    feats = _features(_build("aggregation_key"), "aggregation_key")
    assert sorted(f["properties"]["Municipality"] for f in feats) == ["Lausanne", "Renens"]


def test_aggregation_sql_joins_an_attribute_that_navigates_a_reference():
    builder = _build("aggregation_key_reference")
    tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None and "GROUP BY" in v.body
    assert 'FROM "parcel" "parcel", "municipality" "j1_municipality"' in v.body
    con = _run_ddl(tables, views)
    con.execute('INSERT INTO "municipality" ("id", "name") VALUES (1, ?)', ("Lausanne",))
    con.execute('INSERT INTO "municipality" ("id", "name") VALUES (2, ?)', ("Renens",))
    con.executemany(
        'INSERT INTO "parcel" ("id", "municipality", "zone") VALUES (?, ?, ?)',
        [(1, 1, "A"), (2, 1, "B"), (3, 2, "A")],
    )
    rows = sorted(r[0] for r in con.execute('SELECT "municipalityname" FROM "municipalitylist"'))
    assert rows == ["Lausanne", "Renens"]


def test_aggregation_standard_count_function_sql_groups_by_key_and_runs():
    builder = _build("aggregation_count")
    tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None
    assert "COUNT(*)" in v.body and 'GROUP BY "parcel"."municipality"' in v.body
    con = _run_ddl(tables, views)
    con.executemany(
        'INSERT INTO "parcel" ("id", "municipality") VALUES (?, ?)',
        [(1, "Lausanne"), (2, "Lausanne"), (3, "Renens")],
    )
    rows = dict(con.execute('SELECT "municipality", "parcelcount" FROM "municipalitystats"'))
    assert rows == {"Lausanne": 2, "Renens": 1}


def test_aggregation_standard_count_function_all_sql_is_a_single_ungrouped_row():
    builder = _build("aggregation_count_all")
    tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None
    assert "COUNT(*)" in v.body and "GROUP BY" not in v.body
    con = _run_ddl(tables, views)
    con.executemany(
        'INSERT INTO "parcel" ("id", "municipality") VALUES (?, ?)',
        [(1, "Lausanne"), (2, "Lausanne"), (3, "Renens")],
    )
    (total,) = con.execute('SELECT "total" FROM "parcelstats"').fetchone()
    assert total == 3


def test_aggregation_user_function_still_demotes_even_with_a_stashed_key():
    """The stashed EQUAL(key) doesn't itself make a USER FUNCTION (countB) translatable."""
    builder = _build("aggregation_of")
    view = _registered(builder, "View")[0]
    assert getattr(view, "_aggregation_key", None) is not None
    _tables, views = _sql_views(builder)
    (vb2,) = views
    assert vb2.body is None
    assert any("SQL-VIEW-FORMATION-UNSUPPORTED" in n and "AGGREGATES" in n for n in vb2.notes)


# --- PROJECTION OF an ASSOCIATION -----------------------------------------


def test_projection_of_association_sql_resolves_the_embedded_carrier_and_far_role():
    builder = _build("projection_of_association")
    tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None
    assert 'FROM "planungszone" "typpz_planungszone", "typpz" "j1_typpz"' in v.body
    assert '"typpz_planungszone"."typpz" = "j1_typpz"."id"' in v.body
    con = _run_ddl(tables, views)
    con.execute('INSERT INTO "typpz" ("id", "code") VALUES (1, ?)', ("Z1",))
    con.execute('INSERT INTO "planungszone" ("id", "publishedfrom", "typpz") VALUES (1, ?, 1)', ("2024-01-01",))
    row = con.execute('SELECT "publishedfrom", "typecode" FROM "view_pz"').fetchone()
    assert row == ("2024-01-01", "Z1")
