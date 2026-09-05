"""End-to-end regression test for tests/fixtures/pointcloud3d/ (see its NOTICE and
docs/dev-notes/pointcloud3d-mapping.md).

Synthetic, not real-corpus evidence (RULE #7 exception) - this pins
`interlis convert-jsonfg`'s actual output against the vendored
`Geometry3D_V2` model rather than an inline model string, same style as
the other 3 `Geometry3D_V2` fixture tests.
"""

import json
from pathlib import Path

from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import transfer_to_feature_collection
from interlis.xtf.parse import parse_xtf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pointcloud3d"


def test_pointcloud3d_fixture_converts_to_the_pinned_multipoint_output():
    repository = ModelRepository([FIXTURE_DIR])
    builder = build_from_file(FIXTURE_DIR / "LidarScan_V1.ili", repository=repository)
    transfer = parse_xtf(FIXTURE_DIR / "lidarscan.xtf")

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, repository=repository)

    expected = json.loads((FIXTURE_DIR / "lidarscan.jsonfg.json").read_text())
    assert collection == expected
