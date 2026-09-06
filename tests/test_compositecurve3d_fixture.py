"""End-to-end regression test for tests/fixtures/compositecurve3d/ (see its NOTICE and
docs/dev-notes/curve3d-mapping.md).

Synthetic, not real-corpus evidence (RULE #7 exception) - this pins
`interlis convert-jsonfg`'s actual output against the vendored
`Geometry3D_V2` model rather than an inline model string, same style as
`test_solid3d_fixture.py`.
"""

import json
from pathlib import Path

from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection
from interlis.xtf.parse import parse_xtf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "compositecurve3d"


def test_composite_curve3d_fixture_converts_to_the_pinned_linestring_output():
    repository = ModelRepository([FIXTURE_DIR])
    builder = build_from_file(FIXTURE_DIR / "RoadAxis_V1.ili", repository=repository)
    transfer = parse_xtf(FIXTURE_DIR / "roadaxis.xtf")

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, repository=repository)

    expected = json.loads((FIXTURE_DIR / "roadaxis.jsonfg.json").read_text())
    assert collection == expected
