"""End-to-end validation against tests/fixtures/xtf/xtf24allerrors/ (external,
see its NOTICE file) - the only real-world example found of the XTF 2.4 wire
encoding (`geom:`-namespaced, lowercase geometry tags, `ili:bid`/`ili:tid`
namespaced envelope attributes, model name as element text rather than a
NAME= attribute) exercising every geometry kind in one transfer, including
MULTICOORD/MULTIPOLYLINE/MULTISURFACE - never confirmed real in the XTF 2.3
convention (see `tests/test_xtf_area_overlap_fixtures.py`'s module docstring).
Both `interlis.xtf.parse.parse_xtf` (envelope/basket/object structure) and
`interlis.xtf.validate.validate_transfer` (geometry tag matching) must accept
this encoding for this file to validate cleanly.
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
FIXTURE_DIR = Path(__file__).parent / "fixtures/xtf/xtf24allerrors"


@pytest.fixture(scope="module")
def builder():
    tree, errors = parse_file(FIXTURE_DIR / "AllErrors24.ili")
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


@pytest.fixture(scope="module")
def issues(builder):
    transfer = parse_xtf(FIXTURE_DIR / "AllErrors24-ok.xtf")
    return validate_transfer(transfer, symbol_table=builder.symbol_table)


def test_envelope_parsed_as_xtf24(builder):
    """Sanity check that the file was actually parsed, not silently skipped.

    A class/topic is resolved by its full Model.Topic.Class-qualified name,
    reconstructed from the tag's own real XML namespace (the model's own
    URI, `http://www.interlis.ch/xtf/2.4/<Model>`) plus the enclosing
    basket's TOPIC - XTF 2.4 tags carry only the bare local name on the
    wire, unlike XTF 2.3's own already-fully-qualified bare tags.
    """
    transfer = parse_xtf(FIXTURE_DIR / "AllErrors24-ok.xtf")
    assert transfer.baskets
    assert transfer.baskets[0].objects
    assert any(obj.qualified_class == "AllErrors24.MainTopic.GeometryClass" for obj in transfer.baskets[0].objects)


def test_geometry_class_has_no_error(issues):
    """COORD/POLYLINE(straights+arcs)/SURFACE/MULTICOORD/MULTIPOLYLINE/
    MULTISURFACE, all in one real object - the regression case for both the
    XTF 2.4 tag-case fix (interlis.xtf.validate) and the envelope-parsing
    fix (interlis.xtf.parse)."""
    errors = [i for i in issues if i.qualified_class == "AllErrors24.MainTopic.GeometryClass" and i.severity == "error"]
    assert not errors, errors


def test_area_topology_class_has_no_error(issues):
    """AREA (Kind=Area, wire-encoded as <geom:surface> like Kind=Surface)."""
    errors = [
        i for i in issues
        if i.qualified_class == "AllErrors24.MainTopic.AreaTopologyClass" and i.severity == "error"
    ]
    assert not errors, errors


def test_trimmed_attribute_reported_as_unknown(issues):
    """`interlisName` was removed from AllErrors24.ili (see NOTICE) but the
    real .xtf still declares it - expected fallout of the trim, not a bug."""
    msgs = [i.message for i in issues if i.attribute == "interlisName"]
    assert any("absent" in m for m in msgs)
