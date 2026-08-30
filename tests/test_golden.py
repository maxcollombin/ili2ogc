"""Byte-frozen golden output for a handful of real fixtures, across all three converters.

A regression net for the large builder/converter refactors, and the first
test that actually LOCKS pipeline determinism: every case is run twice and
must be byte-identical, then compared to a committed golden file.

Regenerate the golden files after an intentional output change:

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py

and review the `git diff` before committing.
"""

import os
from pathlib import Path

import pytest

from interlis.cli import main

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"

# (id, [argv for `interlis`], golden filename). `-o <path>` is appended by
# the test, so each command writes its primary output to a file.
CASES: list[tuple[str, list[str], str]] = [
    ("minimal-jsonschema", ["convert", str(FIX / "minimal_model.ili")], "minimal_model.jsonschema.json"),
    ("minimal-sql", ["convert-sql", str(FIX / "minimal_model.ili")], "minimal_model.sql"),
    ("oid-jsonschema", ["convert", str(FIX / "oid_model.ili")], "oid_model.jsonschema.json"),
    ("oid-sql", ["convert-sql", str(FIX / "oid_model.ili")], "oid_model.sql"),
    ("geometry-jsonschema", ["convert", str(FIX / "xtf/geometry_model.ili")], "geometry_model.jsonschema.json"),
    ("geometry-sql", ["convert-sql", str(FIX / "xtf/geometry_model.ili")], "geometry_model.sql"),
    ("view-jsonschema", ["convert", str(FIX / "dmav_view_pattern.ili")], "dmav_view_pattern.jsonschema.json"),
    ("view-sql", ["convert-sql", str(FIX / "dmav_view_pattern.ili")], "dmav_view_pattern.sql"),
    (
        "view-jsonfg",
        ["convert-jsonfg", str(FIX / "xtf/dmav_view_pattern.xtf"), "--model", str(FIX / "dmav_view_pattern.ili")],
        "dmav_view_pattern.jsonfg.json",
    ),
    (
        "translation-jsonschema",
        ["convert", str(FIX / "translation/Wanderwege_V1.ili"), "--repo", str(FIX / "translation")],
        "Wanderwege_V1.jsonschema.json",
    ),
    (
        "translation-sql",
        ["convert-sql", str(FIX / "translation/Wanderwege_V1.ili"), "--repo", str(FIX / "translation")],
        "Wanderwege_V1.sql",
    ),
    (
        "translation-jsonfg",
        ["convert-jsonfg", str(FIX / "translation/wanderwege.xtf"), "--repo", str(FIX / "translation")],
        "wanderwege.jsonfg.json",
    ),
    (
        "dmav-jsonschema",
        ["convert", str(FIX / "dmav/DMAV_Grundstuecke_V1_1.ili"), "--repo", str(FIX / "dmav")],
        "DMAV_Grundstuecke_V1_1.jsonschema.json",
    ),
    (
        "dmav-sql",
        ["convert-sql", str(FIX / "dmav/DMAV_Grundstuecke_V1_1.ili"), "--repo", str(FIX / "dmav")],
        "DMAV_Grundstuecke_V1_1.sql",
    ),
]


def _run(argv: list[str], out: Path) -> str:
    rc = main([*argv, "-o", str(out)])
    # 0 = clean, 2 = completed with degradations - both produce the full,
    # deterministic primary output; 1 (hard failure) must not happen here.
    assert rc in (0, 2), f"{argv} exited {rc}"
    return out.read_text(encoding="utf-8")


@pytest.mark.parametrize("argv,golden_name", [(c[1], c[2]) for c in CASES], ids=[c[0] for c in CASES])
def test_golden_output_is_byte_identical(argv: list[str], golden_name: str, tmp_path: Path) -> None:
    produced = _run(argv, tmp_path / golden_name)

    # Determinism: a second run must be byte-identical to the first.
    again = _run(argv, tmp_path / f"rerun_{golden_name}")
    assert produced == again, f"{golden_name}: non-deterministic output between two runs"

    golden_path = GOLDEN / golden_name
    if os.environ.get("UPDATE_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(produced, encoding="utf-8")
        pytest.skip(f"UPDATE_GOLDEN: wrote {golden_name}")

    assert golden_path.exists(), f"missing golden {golden_name} - regenerate with UPDATE_GOLDEN=1"
    expected = golden_path.read_text(encoding="utf-8")
    assert produced == expected, (
        f"{golden_name} drifted from its golden. Review the change; if intentional, "
        f"regenerate with UPDATE_GOLDEN=1 and commit the diff."
    )
