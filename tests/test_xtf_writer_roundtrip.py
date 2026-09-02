"""`render_xtf`'s generic round-trip - independent of any VIEW.

Answers a question raised while framing the `write-xtf` writer: does the
writer's core serialization work for ANY model, not just VIEW-derived
data? Yes - `render_xtf` never inspects `XtfObject.qualified_class` for
VIEW-ness, it just re-serializes whatever `XtfBasket`/`XtfObject`/
`RawNode` it is given (see `convert/xtf_writer.py`'s module docstring).
Proven here by parsing a real `.xtf` (real `RoadTrafficCensus_V1_1`
wire shapes - a catalogue `REFERENCE`, a `COORD`), rendering it straight
back out, re-parsing the result, and comparing structurally - isolating
this layer from VIEW-evaluation correctness entirely (that half is
covered by `tests/test_xtf_writer.py`, plus real MainRoads/
Waldabstandslinien corpus data verified ad hoc against `ili2c`/
`xmllint`, not committed as a test).
"""

from pathlib import Path

from interlis.convert.xtf_writer import render_xtf
from interlis.xtf.parse import RawNode, XtfTransfer, parse_xtf

_SAMPLE = Path(__file__).resolve().parent / "fixtures/xtf/sample.xtf"


def _node_key(node: RawNode):
    return (node.tag, node.text, tuple(sorted(node.attrib.items())), tuple(_node_key(c) for c in node.children))


def _structurally_equal(a: XtfTransfer, b: XtfTransfer) -> bool:
    if [(m.name, m.version, m.uri) for m in a.models] != [(m.name, m.version, m.uri) for m in b.models]:
        return False
    if len(a.baskets) != len(b.baskets):
        return False
    for basket_a, basket_b in zip(a.baskets, b.baskets):
        if (basket_a.bid, basket_a.qualified_topic, basket_a.kind, basket_a.endstate) != (
            basket_b.bid,
            basket_b.qualified_topic,
            basket_b.kind,
            basket_b.endstate,
        ):
            return False
        if len(basket_a.objects) != len(basket_b.objects):
            return False
        for obj_a, obj_b in zip(basket_a.objects, basket_b.objects):
            if (obj_a.tid, obj_a.qualified_class) != (obj_b.tid, obj_b.qualified_class):
                return False
            keys_a = {name: [_node_key(n) for n in nodes] for name, nodes in obj_a.attributes.items()}
            keys_b = {name: [_node_key(n) for n in nodes] for name, nodes in obj_b.attributes.items()}
            if keys_a != keys_b:
                return False
    return True


def test_render_xtf_round_trips_a_real_plain_class_transfer_byte_for_structure(tmp_path):
    """No VIEW involved - `sample.xtf`'s 2 `MeasurementLocation` objects, one with a COORD and a catalogue REF."""
    original = parse_xtf(_SAMPLE)

    rendered = render_xtf(original)
    reparsed_path = tmp_path / "roundtrip.xtf"
    reparsed_path.write_text(rendered, encoding="utf-8")
    reparsed = parse_xtf(reparsed_path)

    assert _structurally_equal(original, reparsed)
