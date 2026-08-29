"""XTF 2.4 envelope structure (interlis.xtf.parse): a second, real wire
convention besides XTF 2.3 (see test_xtf_parse.py) - lowercase `ili:`-namespaced
section tags, model name as element TEXT (not a NAME= attribute),
`ili:bid`/`ili:tid` namespaced attributes, `<ili:sender>` as a child element.
tests/fixtures/xtf/xtf24envelope/MultiCoord.xtf (external, see its NOTICE
file) additionally uses a PREFIXED model namespace rather than the default
one used by tests/fixtures/xtf/xtf24allerrors/AllErrors24-ok.xtf (covered by
test_xtf_xtf24_fixtures.py, schema-aware end-to-end coverage of geometry
tags)."""

from pathlib import Path

from interlis.xtf.parse import parse_xtf

FIXTURE = Path(__file__).parent / "fixtures/xtf/xtf24envelope/MultiCoord.xtf"


def test_header_sender_and_model_name():
    transfer = parse_xtf(FIXTURE)
    assert transfer.sender == "IOX"
    names = [m.name for m in transfer.models]
    assert names == ["DataTest1"]
    # No per-model VERSION/URI in the XTF 2.4 header form (see the comment
    # preceding the "model" role's end-event handling in parse.py).
    assert transfer.models[0].version is None
    assert transfer.models[0].uri is None


def test_basket_and_object_use_namespaced_bid_tid():
    """qualified_topic/qualified_class are reconstructed as Model.Topic[.Class]
    from the tag's own real XML namespace (DataTest1's own URI) plus the
    bare local tag - XTF 2.4 tags carry only the bare local name on the
    wire, unlike XTF 2.3's own already-fully-qualified bare tags."""
    transfer = parse_xtf(FIXTURE)
    (basket,) = transfer.baskets
    assert basket.bid == "bidB"
    assert basket.qualified_topic == "DataTest1.TopicB"
    (obj,) = basket.objects
    assert obj.tid == "mOid"
    assert obj.qualified_class == "DataTest1.TopicB.MultiCoord"


def test_multicoord_attribute_tag_is_lowercase_namespaced():
    transfer = parse_xtf(FIXTURE)
    obj = transfer.baskets[0].objects[0]
    (node,) = obj.attributes["attr4"]
    (multicoord,) = node.children
    assert multicoord.tag == "multicoord"
    assert len(multicoord.children) == 3
    coord = multicoord.children[0]
    assert coord.tag == "coord"
    c1, c2, c3 = coord.children
    assert (c1.tag, c1.text) == ("c1", "480000.111")
    assert (c2.tag, c2.text) == ("c2", "70000.111")
    assert (c3.tag, c3.text) == ("c3", "5000.111")
