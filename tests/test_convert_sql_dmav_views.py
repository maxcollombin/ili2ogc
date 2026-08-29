"""Real DMAV models -> `CREATE VIEW` (`tests/fixtures/dmav/`).

Every DMAV data topic publishes its "current" extract as
`VIEW <Class>_Gueltig PROJECTION OF <Class>; WHERE DEFINED(...) ...; =
ALL OF <Class>; UNIQUE CHxxxxxx: ...`. This locks the three pieces that
makes that shape produce an executable `CREATE VIEW`:

- `ALL OF <Class>` re-exports each base attribute with a synthetic
  identity `Derivates`, so `build_views` can project it;
- `WHERE DEFINED(a->role->attr)` becomes an `EXISTS (SELECT 1 FROM ...)`
  over the association, reading the FK from whichever side carries it;
- the view-level `UNIQUE CHxxxxxx:` is surfaced as a `-- NOTE` (a SQL
  view cannot enforce it), never dropped.

The hermetic, network-free reduction is `tests/test_view_dmav_pattern.py`.
"""
import sqlite3
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.convert.sql import build_tables, build_views, render_gpkg
from interlis.metamodel.instance import MetaInstance
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


def test_grundstueck_gueltig_where_becomes_an_exists_chain_and_the_unique_is_a_note():
    sql_views, _tables = _views_and_tables("DMAV_Grundstuecke_V1_1.ili")
    view = next(v for v in sql_views if v.name == "grundstueck_gueltig")
    # `DEFINED(Grundstueck->Entstehung->Grundbucheintrag)` - a hop through the
    # shared GSNachfuehrung mutation class, ending on its nullable date
    assert 'EXISTS (SELECT 1 FROM "gsnachfuehrung"' in view.body
    assert '"grundbucheintrag" IS NOT NULL' in view.body
    assert "NOT (EXISTS" in view.body  # the `NOT(DEFINED(...->Untergang->...))` branch
    assert "UNIQUE" not in view.body  # never carried onto the CREATE VIEW
    assert any("VIEW-level UNIQUE 'CH041101'" in n and "NBIdent, Nummer" in n for n in view.notes)


def test_dmav_create_views_compile_against_a_real_sqlite_engine():
    """The generated `CREATE VIEW` text is valid SQL, not just Python-assembled - run it."""
    sql_views, tables = _views_and_tables("DMAV_Grundstuecke_V1_1.ili")
    schema = render_gpkg(tables).split("\nINSERT INTO gpkg_contents")[0]
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema)
    for v in sql_views:
        conn.execute(f'CREATE VIEW "{v.name}" AS {v.body}')
        conn.execute(f'SELECT * FROM "{v.name}"').fetchall()  # empty tables, but the plan compiles
