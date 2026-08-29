"""Real-corpus regression: the DMAV VIEW idiom (`tests/fixtures/dmav_view_pattern.ili`).

A 2026 survey of the published DMAV_* and SIA405_Abwasser_* model families
found every real VIEW `WHERE` uses only nested `DEFINED()` over multi-hop
association paths joined by `AND`/`OR`/`NOT`, ending on a plain attribute -
no arithmetic, no function call. This fixture is the reduced, hermetic
reproduction of that idiom, modelled on
`V_D/DMAV_Grundstuecke_V1_1.ili`'s `Grundstueck_Gueltig`. It exercises:

- `PROJECTION OF` a single class with `ALL OF <class>` re-export
- a `WHERE` that navigates two association hops through a shared
  mutation-tracking class (`Grundstueck->Entstehung->Grundbucheintrag`)
- catalogue-numbered view-level `UNIQUE` (`CH041101` / `CH041102`)
- `.ili -> CREATE VIEW` (`convert/sql.py`) and `.xtf -> JSON-FG`
  (`convert/jsonfg.py`) end to end
"""

import json
import sqlite3
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonfg import evaluate_view, unsupported_view_reason
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file
from interlis.xtf.parse import parse_xtf

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "dmav_view_pattern.ili"
XTF = Path(__file__).resolve().parent / "fixtures" / "xtf" / "dmav_view_pattern.xtf"


def _build():
    tree, errors = parse_file(FIXTURE)
    assert not errors, f"unexpected syntax errors: {errors}"
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


def _view(builder=None):
    builder = builder or _build()
    views = _registered(builder, "View")
    assert len(views) == 1
    return views[0]


def test_projection_with_all_of_attributes():
    view = _view()
    assert view.Name == "Grundstueck_Gueltig"
    assert view.FormationKind == "Projection"
    assert {a.Name for a in view.ClassAttribute} == {"NBIdent", "Nummer", "EGRID"}


def test_all_of_attributes_carry_a_synthetic_identity_derivates():
    """`ALL OF Grundstueck` re-exports each attribute as `Grundstueck -> <attr>` so a converter can project it."""
    for attr in _view().ClassAttribute:
        derivates = list(getattr(attr, "Derivates", None) or [])
        assert len(derivates) == 1, attr.Name
        factor = derivates[0]
        assert [pe.Ref for pe in factor.PathEls] == ["Grundstueck", attr.Name]
        assert getattr(factor, "_all_of_identity", False)


def test_view_level_unique_constraints_are_built_once_and_linked_to_the_view():
    """`UNIQUE CH041101:` / `CH041102:` land on `View.Constraint`, each exactly once (no double-attach)."""
    view = _view()
    constraints = list(getattr(view, "Constraint", None) or [])
    assert len(constraints) == len({id(c) for c in constraints}) == 2
    assert {c.Name for c in constraints} == {"CH041101", "CH041102"}
    assert all(c._qualified_class == "IlisMeta16.ModelData.UniqueConstraint" for c in constraints)


def test_nested_defined_where_is_in_evaluator_scope():
    """The DMAV `WHERE` (nested DEFINED over two association hops) is NOT skipped."""
    assert unsupported_view_reason(_view()) is None


def test_convert_sql_emits_a_real_create_view_with_exists_predicates():
    builder = _build()
    classes = _registered(builder, "Class")
    sql_views = build_views(
        _registered(builder, "View"),
        build_tables(classes, symbol_table=builder.symbol_table),
        symbol_table=builder.symbol_table,
    )
    assert len(sql_views) == 1
    v = sql_views[0]
    assert v.body is not None, v.notes
    # the two-hop `DEFINED(... ->Entstehung->Grundbucheintrag)` becomes an
    # EXISTS over the shared mutation class plus an IS NOT NULL on its date
    assert 'EXISTS (SELECT 1 FROM "gsnachfuehrung"' in v.body
    assert '"grundbucheintrag" IS NOT NULL' in v.body
    # the view-level UNIQUE is surfaced as a note, never carried onto the CREATE VIEW
    assert any("VIEW-level UNIQUE 'CH041101'" in n for n in v.notes)
    assert "UNIQUE" not in v.body


def test_convert_sql_create_view_executes_and_filters_against_live_sqlite():
    builder = _build()
    classes = _registered(builder, "Class")
    tables = build_tables(classes, symbol_table=builder.symbol_table)
    sql_views = build_views(_registered(builder, "View"), tables, symbol_table=builder.symbol_table)
    schema = render_gpkg(tables).split("\nINSERT INTO gpkg_contents")[0]
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    conn.execute(f'CREATE VIEW "{sql_views[0].name}" AS {sql_views[0].body}')
    conn.executescript("""
        INSERT INTO grundstueck (id, nbident, nummer, egrid, entstehung, untergang) VALUES
          ('g1','CH1','100','E1','n1',NULL),
          ('g2','CH1','200','E2','n2e','n2u'),
          ('g3','CH1','300','E3','n3',NULL);
        INSERT INTO gsnachfuehrung (id, nbident, identifikator, gueltigereintrag, grundbucheintrag) VALUES
          ('n1','CH1','D-100','2020-01-10','2020-02-14'),
          ('n2e','CH1','D-200e','2019-01-10','2019-02-14'),
          ('n2u','CH1','D-200u','2022-01-10','2022-02-14'),
          ('n3','CH1','D-300','2024-01-10',NULL);
    """)
    assert sorted(r[0] for r in conn.execute("SELECT nummer FROM grundstueck_gueltig")) == ["100"]


def test_xtf_to_jsonfg_flattens_the_view_and_applies_the_where_filter():
    builder = _build()
    transfer = parse_xtf(XTF)
    features = evaluate_view(_view(builder), transfer, symbol_table=builder.symbol_table)
    assert [(f["id"], f["properties"]["Nummer"]) for f in features] == [("g1", "100")]
    assert set(features[0]["properties"]) == {"NBIdent", "Nummer", "EGRID"}


def test_jsonfg_base_class_output_is_unchanged_by_the_all_of_derivates():
    """Adding `Derivates` to the `ALL OF` attributes must not change what a plain PROJECTION emits."""
    builder = _build()
    transfer = parse_xtf(XTF)
    feature = evaluate_view(_view(builder), transfer, symbol_table=builder.symbol_table)[0]
    # PROJECTION re-tags the base object; the emitted properties are still
    # the base attribute values read by name, never the synthetic path
    assert feature["properties"] == {"NBIdent": "CH0000001", "Nummer": "100", "EGRID": "CH100000000001"}
    json.dumps(feature)  # serialisable
