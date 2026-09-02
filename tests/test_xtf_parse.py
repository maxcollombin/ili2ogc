"""Structural XTF parser: covers the 3 attribute forms found in the real
corpus (data.geo.admin.ch, see scripts/fetch_xtf_corpus.py) - simple,
nested reference (<Reference REF="..."/>), coordinate
(<COORD><C1>/<C2></COORD>) - via a reduced fixture
(tests/fixtures/xtf/sample.xtf, a faithful extract of a real downloaded
file, not invented)."""

from pathlib import Path

from interlis.xtf.parse import parse_xtf

FIXTURE = Path(__file__).parent / "fixtures/xtf/sample.xtf"


def test_header_and_models():
    transfer = parse_xtf(FIXTURE)
    assert transfer.sender == "test-fixture"
    assert transfer.ili_version == "2.3"
    names = [m.name for m in transfer.models]
    assert names == ["Units", "RoadTrafficCensus_V1_1"]


def test_basket_and_objects():
    transfer = parse_xtf(FIXTURE)
    assert len(transfer.baskets) == 1
    basket = transfer.baskets[0]
    assert basket.qualified_topic == "RoadTrafficCensus_V1_1.RoadTrafficCensus"
    assert basket.bid == "2"
    assert basket.kind == "INITIAL"
    assert basket.endstate == "2026-05-07"
    assert len(basket.objects) == 2
    assert basket.objects[0].tid == "1572f265-8664-47b2-a99d-06c2718e9346"
    assert basket.objects[0].qualified_class == "RoadTrafficCensus_V1_1.RoadTrafficCensus.MeasurementLocation"


def test_simple_attribute():
    transfer = parse_xtf(FIXTURE)
    obj = transfer.baskets[0].objects[0]
    (node,) = obj.attributes["MLocName"]
    assert node.text == "MUTTENZ, A2/ZUBR. SCHAENZLI"
    assert node.children == []


def test_optional_attribute_absent_on_second_object():
    transfer = parse_xtf(FIXTURE)
    obj = transfer.baskets[0].objects[1]
    assert "MLocStatus" not in obj.attributes
    assert "LocationLV95" not in obj.attributes
    assert obj.attributes["MLocNr"][0].text == "10"


def test_nested_reference_attribute():
    transfer = parse_xtf(FIXTURE)
    obj = transfer.baskets[0].objects[0]
    (node,) = obj.attributes["MLocStatus"]
    assert node.text is None
    (role_node,) = node.children
    assert role_node.tag == "RoadTrafficCensus_V1_1.RoadTrafficCensusCatalogues.MLocStatusRef"
    (ref_node,) = role_node.children
    assert ref_node.tag == "Reference"
    assert ref_node.attrib == {"REF": "ch.astra.roadtrafficcensus.402"}


def test_coordinate_attribute():
    transfer = parse_xtf(FIXTURE)
    obj = transfer.baskets[0].objects[0]
    (node,) = obj.attributes["LocationLV95"]
    (coord,) = node.children
    assert coord.tag == "COORD"
    c1, c2 = coord.children
    assert (c1.tag, c1.text) == ("C1", "2613975.0")
    assert (c2.tag, c2.text) == ("C2", "1265100.0")
