"""Every INTERLIS `VIEW` FormationKind, across all three converters.

`tests/fixtures/views/*.ili` are the reference-manual canonical examples
for `UNION OF`, `AGGREGATION OF` and `INSPECTION OF` (also quoted in the
FGDM4GS report, HEIG-VD 2024, chapter 4.2), plus one translatable
`AGGREGATION OF` variant whose columns are plain key projections rather
than a FUNCTION over the implicit `AGGREGATES` bag.

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
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonfg import evaluate_view, unsupported_view_reason
from interlis.convert.jsonschema import model_to_json_schema
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file
from interlis.xtf.parse import parse_xtf

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURES = ROOT / "tests" / "fixtures" / "views"


def _build(name: str):
    tree, errors = parse_file(FIXTURES / f"{name}.ili")
    assert not errors, f"unexpected syntax errors in {name}.ili: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


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


def test_aggregation_projection_only_sql_is_select_distinct_and_runs():
    builder = _build("aggregation_key")
    _tables, views = _sql_views(builder)
    (v,) = views
    assert v.body is not None and v.body.startswith("SELECT DISTINCT")
    con = _run_ddl(_tables, views)
    con.executemany(
        'INSERT INTO "parcel" ("id", "municipality") VALUES (?, ?)',
        [(1, "Lausanne"), (2, "Lausanne"), (3, "Renens")],
    )
    assert sorted(r[0] for r in con.execute('SELECT "municipality" FROM "municipalitylist"')) == ["Lausanne", "Renens"]


def test_aggregation_projection_only_jsonfg_deduplicates():
    feats = _features(_build("aggregation_key"), "aggregation_key")
    assert sorted(f["properties"]["Municipality"] for f in feats) == ["Lausanne", "Renens"]
