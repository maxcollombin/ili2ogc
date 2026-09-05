"""End-to-end regression test for tests/fixtures/tin3d_mesh/ (see its NOTICE and
docs/dev-notes/composite-surface3d-mapping.md).

Synthetic, not real-corpus evidence (RULE #7 exception) - this pins
`interlis convert-jsonfg`'s actual output against the vendored
`Geometry3D_V2` model rather than an inline model string, same style as
`test_solid3d_polyhedron_fixture.py`/`test_curve3d_composite_fixture.py`.
"""

import json
from pathlib import Path

from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection
from interlis.xtf.parse import parse_xtf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tin3d_mesh"


def test_tin3d_fixture_converts_to_the_pinned_multipolygon_output():
    repository = ModelRepository([FIXTURE_DIR])
    builder = build_from_file(FIXTURE_DIR / "GeologicalLayer_V1.ili", repository=repository)
    transfer = parse_xtf(FIXTURE_DIR / "layerboundary.xtf")

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, repository=repository)

    expected = json.loads((FIXTURE_DIR / "layerboundary.jsonfg.json").read_text())
    assert collection == expected
