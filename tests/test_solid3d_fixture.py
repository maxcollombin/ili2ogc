"""End-to-end regression test for tests/fixtures/solid3d/ (see its NOTICE and
docs/dev-notes/solid3d-polyhedron-mapping.md).

Synthetic, not real-corpus evidence (RULE #7 exception) - this pins
`interlis convert-jsonfg`'s actual output against the vendored
(adapted) `Geometry3D_V2` model rather than an inline model string, so a
regression here is caught the same way `test_view_wfs_crosscheck.py`
catches drift on its own vendored real models.
"""

import json
from pathlib import Path

from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection
from interlis.xtf.parse import parse_xtf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "solid3d"


def test_solid3d_fixture_converts_to_the_pinned_polyhedron_output():
    repository = ModelRepository([FIXTURE_DIR])
    builder = build_from_file(FIXTURE_DIR / "Building3D_V1.ili", repository=repository)
    transfer = parse_xtf(FIXTURE_DIR / "building3d.xtf")

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, repository=repository)

    expected = json.loads((FIXTURE_DIR / "building3d.jsonfg.json").read_text())
    assert collection == expected
