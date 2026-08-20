"""End-to-end validation of tests/fixtures/xtf/xtf23overlap/ (external, see its
NOTICE file) - the only real-world AREA/SURFACE geometry fixtures available,
confirming the wire encoding for LineType Kind=Area (a `<SURFACE>` tag, never
a distinct `<AREA>` tag - see the comment preceding
interlis.xtf.validate._validate_line_attribute) and for multi-boundary/ARC
segments. `WITHOUT OVERLAPS` is a formal constraint (never executed by this
validator, see docs/dev-notes/xtf-validator-scope.md) - the *NotAllowedOverlap
fixtures are included for structural coverage only, not to assert an overlap
error that this validator doesn't compute.
"""
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_file
from interlis.xtf.parse import parse_xtf
from interlis.xtf.validate import validate_transfer

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURE_DIR = Path(__file__).parent / "fixtures/xtf/xtf23overlap"


@pytest.fixture(scope="module")
def builder():
    tree, errors = parse_file(FIXTURE_DIR / "Overlap23.ili")
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


ALL_FIXTURES = sorted(FIXTURE_DIR.glob("*.xtf"))


@pytest.mark.parametrize("xtf_path", ALL_FIXTURES, ids=lambda p: p.name)
def test_fixture_has_no_error_issue(builder, xtf_path):
    """AreaSimple.xtf (LineType Kind=Area, wire-encoded as `<SURFACE>`) is
    the regression case for the tag bug this fixture set caught - it would
    fail here if the validator went back to expecting a literal `<AREA>`
    tag that real files never use."""
    transfer = parse_xtf(xtf_path)
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    errors = [i for i in issues if i.severity == "error"]
    assert not errors, errors
