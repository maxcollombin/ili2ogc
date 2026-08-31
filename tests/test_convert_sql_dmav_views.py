"""Real DMAV models -> `CREATE VIEW` (`tests/fixtures/dmav/`).

Every DMAV data topic publishes its "current" extract as
`VIEW <Class>_Gueltig PROJECTION OF <Class>; WHERE DEFINED(...) ...; =
ALL OF <Class>; UNIQUE CHxxxxxx: ...`. This locks the three pieces that
makes that shape produce an executable `CREATE VIEW`:

- `ALL OF <Class>` re-exports each base attribute with a synthetic
  identity `Derivates`, so `build_views` can project it;
- `WHERE DEFINED(a->role->attr)` becomes an `EXISTS (SELECT 1 FROM ...)`
  over the association, reading the FK from whichever side carries it;
- a plain-column view-level `UNIQUE CHxxxxxx:` becomes a `BEFORE INSERT`/
  `BEFORE UPDATE` trigger on the base table (a `CREATE VIEW` itself
  cannot enforce it); one navigating a nested/geometry attribute, or a
  `SetConstraint`/`ExistenceConstraint`, stays a `-- NOTE` - never dropped.

The hermetic, network-free reduction is `tests/test_view_dmav_pattern.py`.
"""

import sqlite3
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.runtime.parse import parse_file

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
DMAV = Path(__file__).resolve().parent / "fixtures" / "dmav"


def _views_and_tables(model: str):
    tree, errors = parse_file(DMAV / model)
    assert not errors, errors
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=ModelRepository([DMAV]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    registered = builder.symbol_table.all_registered()
    classes = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".Class")]
    views = [i for i in registered if getattr(i, "_qualified_class", "").endswith(".View")]
    tables = build_tables(classes, symbol_table=builder.symbol_table)
    return build_views(views, tables, symbol_table=builder.symbol_table), tables


@pytest.mark.parametrize(
    ("model", "expected_views"),
    [
        ("DMAV_Grundstuecke_V1_1.ili", 5),
        ("DMAV_Bodenbedeckung_V1_1.ili", 1),
    ],
)
def test_every_dmav_gueltig_view_is_a_real_create_view(model, expected_views):
    sql_views, _tables = _views_and_tables(model)
    assert len(sql_views) == expected_views
    for v in sql_views:
        assert v.body is not None, (v.name, v.notes)
        assert v.body.startswith("SELECT")


def test_grundstueck_gueltig_where_becomes_an_exists_chain_and_the_unique_is_a_trigger():
    sql_views, _tables = _views_and_tables("DMAV_Grundstuecke_V1_1.ili")
    view = next(v for v in sql_views if v.name == "grundstueck_gueltig")
    # `DEFINED(Grundstueck->Entstehung->Grundbucheintrag)` - a hop through the
    # shared GSNachfuehrung mutation class, ending on its nullable date
    assert 'EXISTS (SELECT 1 FROM "gsnachfuehrung"' in view.body
    assert '"grundbucheintrag" IS NOT NULL' in view.body
    assert "NOT (EXISTS" in view.body  # the `NOT(DEFINED(...->Untergang->...))` branch
    assert "UNIQUE" not in view.body  # never carried onto the CREATE VIEW
    # both plain-column view-level UNIQUE constraints became real triggers, not notes
    assert {(t.label, tuple(t.columns)) for t in view.triggers} == {
        ("CH041101", ("nbident", "nummer")),
        ("CH041102", ("egrid",)),
    }


def test_grenzpunkt_gueltig_geometry_unique_and_liegenschaft_gueltig_set_stay_notes():
    """A geometry-typed key (`Grenzpunkt.Geometrie` is a nested `Coord2`, not a single column) and a whole-population
    `SetConstraint` (`INTERLIS.areAreas`) are outside a trigger's reach - unlike a plain-column UNIQUE.
    """
    sql_views, _tables = _views_and_tables("DMAV_Grundstuecke_V1_1.ili")
    grenzpunkt = next(v for v in sql_views if v.name == "grenzpunkt_gueltig")
    liegenschaft = next(v for v in sql_views if v.name == "liegenschaft_gueltig")
    assert not grenzpunkt.triggers
    assert any("VIEW-level UNIQUE 'CH040601'" in n for n in grenzpunkt.notes)
    assert not liegenschaft.triggers
    assert any("VIEW-level SetConstraint 'CH041501'" in n for n in liegenschaft.notes)


def test_dmav_create_views_and_unique_triggers_compile_against_a_real_sqlite_engine():
    """The generated `CREATE VIEW`/`CREATE TRIGGER` text is valid SQL, not just Python-assembled - run it."""
    sql_views, tables = _views_and_tables("DMAV_Grundstuecke_V1_1.ili")
    full = render_gpkg(tables, tuple(sql_views))
    schema = "\n".join(line for line in full.splitlines() if not line.startswith(("INSERT INTO gpkg_", "-- TODO:")))
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for v in sql_views:
        conn.execute(f'SELECT * FROM "{v.name}"').fetchall()  # empty tables, but the plan compiles
