"""Backlog item 15 - VIEW TOPIC data -> `.xtf` writer (`interlis.convert.xtf_writer`).

See the module's own docstring for the refman grounding (eCH-0031 V2.1.0
SS4182/SS4728/SS4.3.7/SS4.3.8) and `docs/view-transfer-format-comparison.md`
for why VIEW TOPIC transfer is FULL-only. `render_xtf`'s generic
round-trip correctness (independent of any VIEW) is proven separately,
against a real corpus `.xtf`, in `tests/test_xtf_writer_roundtrip.py`.
"""

import tempfile
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonfg import evaluate_view_objects
from interlis.convert.xtf_writer import render_xtf, write_view_basket, write_xtf
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file, parse_text
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer, parse_xtf

VIEW_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "views"

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_VIEW_MODEL = """INTERLIS 2.4;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
      Attr2: TEXT*20;
    END B;
    CLASS C =
      Attr3: TEXT*20;
    END C;
  END Base;
  VIEW TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
    VIEW VPR
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        second := B -> Attr2;
        first := B -> Attr1;
    END VPR;
    VIEW VJ
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJ;
  END Views;
END Test.
"""


def _build(src: str) -> InterlisModelBuilder:
    tree, errors = parse_text(src)
    assert not errors, f"unexpected syntax errors: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _build_fixture(name: str) -> InterlisModelBuilder:
    """Build one of `tests/fixtures/views/*.ili` (refman canonical VIEW examples - `test_view_formation_kinds.py`)."""
    tree, errors = parse_file(VIEW_FIXTURES / f"{name}.ili")
    assert not errors, f"unexpected syntax errors in {name}.ili: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _view(builder: InterlisModelBuilder, name: str) -> MetaInstance:
    for inst in builder.symbol_table.all_registered():
        if getattr(inst, "_qualified_class", None) == "IlisMeta16.ModelData.View" and inst.Name == name:
            return inst
    raise AssertionError(f"no View named {name!r} built")


def _node(tag: str, text: str) -> RawNode:
    return RawNode(tag=tag, text=text, attrib={}, children=[])


def _b(tid: str, attr1: str, attr2: str) -> XtfObject:
    return XtfObject(
        tid=tid,
        qualified_class="Test.Base.B",
        attributes={"Attr1": [_node("Attr1", attr1)], "Attr2": [_node("Attr2", attr2)]},
    )


def _c(tid: str, attr3: str) -> XtfObject:
    return XtfObject(tid=tid, qualified_class="Test.Base.C", attributes={"Attr3": [_node("Attr3", attr3)]})


def _transfer(*objects: XtfObject) -> XtfTransfer:
    basket = XtfBasket(bid="bid-1", qualified_topic="Test.Base", kind=None, endstate=None, objects=list(objects))
    return XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])


def _parse_xtf_text(text: str) -> XtfTransfer:
    """Round-trip helper: write `text` to a temp file and re-parse it (`parse_xtf` only reads from a `Path`)."""
    with tempfile.NamedTemporaryFile("w", suffix=".xtf", encoding="utf-8", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        return parse_xtf(path)
    finally:
        path.unlink()


def test_write_view_basket_tags_objects_under_the_views_own_qualified_name():
    """Regression: `evaluate_view_objects` keeps whichever `qualified_class` its SOURCE object(s) had.

    Wrong for a wire tag either way (a base object's own tag for
    PROJECTION, the VIEW's bare short `Name` for JOIN - see
    `write_view_basket`'s docstring) - `write_view_basket` must override
    it to the VIEW's own `Model.Topic.ViewName`.
    """
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x", "y"))

    basket = write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)

    assert basket.qualified_topic == "Test.Views"
    assert basket.kind is None  # omitted on the wire - refman: absent KIND means FULL
    assert basket.bid == "basket-1"
    [obj] = basket.objects
    assert obj.qualified_class == "Test.Views.VP"
    assert obj.tid == "b1"


def test_render_xtf_writes_the_views_own_wire_tags_and_declared_attribute_order():
    """Regression: a renamed VIEW attribute's `RawNode.tag` still carries the SOURCE attribute's own tag.

    (`_project_object_under_view_names` re-keys the dict but copies the
    `RawNode` unchanged - `render_xtf`/`_raw_node_to_element` must use the
    declared attribute NAME for the emitted element tag, never
    `RawNode.tag`.) Also checks refman SS4.3.7's XSD-sequence order rule:
    `VPR` declares `second` THEN `first` (reversed from `B`'s own
    `Attr2`/`Attr1` source order) - the written element order must follow
    the VIEW's OWN declaration, not the source object's wire order.
    """
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VPR")
    transfer = _transfer(_b("b1", "x", "y"))

    objects = evaluate_view_objects(view, transfer, symbol_table=builder.symbol_table)
    basket = write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)
    text = render_xtf(
        XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket]),
        attr_order_by_class={"Test.Views.VPR": ["second", "first"]},
    )

    assert len(objects) == 1  # sanity: evaluate_view_objects still ran the projection
    assert "<second>y</second><first>x</first>" in text
    assert "<Attr1>" not in text and "<Attr2>" not in text  # leftover source-named keys never leak onto the wire


def test_write_view_basket_join_pools_both_bases_under_the_views_tag():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJ")
    transfer = _transfer(_b("b1", "x", "y"), _c("c1", "z"))

    basket = write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)

    [obj] = basket.objects
    assert obj.qualified_class == "Test.Views.VJ"
    assert obj.tid == "b1_c1"
    assert set(obj.attributes) >= {"Attr1", "Attr2", "Attr3"}


def test_write_xtf_standalone_holds_only_the_views_own_model():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x", "y"))

    text = write_xtf(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)
    reparsed = _parse_xtf_text(text)

    assert [m.name for m in reparsed.models] == ["Test"]
    assert len(reparsed.baskets) == 1
    assert reparsed.baskets[0].qualified_topic == "Test.Views"


def test_write_xtf_merge_with_source_appends_to_the_source_transfers_own_baskets():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x", "y"))

    text = write_xtf(view, transfer, bid="basket-1", symbol_table=builder.symbol_table, merge_with_source=True)
    reparsed = _parse_xtf_text(text)

    assert [b.qualified_topic for b in reparsed.baskets] == ["Test.Base", "Test.Views"]


def test_write_xtf_round_trips_through_reparsing_to_the_same_property_values():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VPR")
    transfer = _transfer(_b("b1", "x", "y"))

    text = write_xtf(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)
    reparsed = _parse_xtf_text(text)

    [obj] = reparsed.baskets[0].objects
    assert obj.attributes["second"][0].text == "y"
    assert obj.attributes["first"][0].text == "x"


def test_write_view_basket_union_pools_each_bases_own_objects_under_the_views_tag():
    """UNION: `union_of.ili` (refman canonical example) - C1/C2 -> CC, each re-keyed under `Attr1`."""
    builder = _build_fixture("union_of")
    view = _view(builder, "CC")
    transfer = parse_xtf(VIEW_FIXTURES / "xtf" / "union_of.xtf")

    basket = write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)

    assert {obj.tid for obj in basket.objects} == {"c1a", "c1b", "c2a"}
    assert all(obj.qualified_class == "TestUnion.Union.CC" for obj in basket.objects)
    by_tid = {obj.tid: obj.attributes["Attr1"][0].text for obj in basket.objects}
    assert by_tid == {"c1a": "alpha", "c1b": "beta", "c2a": "gamma"}


def test_write_view_basket_inspection_pools_one_object_per_structure_element():
    """INSPECTION: `tests/fixtures/views/inspection_of.ili` - one `XtfObject` per `BAG OF A` element."""
    builder = _build_fixture("inspection_of")
    view = _view(builder, "VB")
    transfer = parse_xtf(VIEW_FIXTURES / "xtf" / "inspection_of.xtf")

    basket = write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)

    assert all(obj.qualified_class == "TestInspection.Base.VB" for obj in basket.objects)
    values = sorted(obj.attributes["Attr1"][0].text for obj in basket.objects)
    assert values == ["first", "second", "third"]


def test_write_view_basket_rejects_inspection_of_a_surface_geometry():
    """The single-hop SURFACE/AREA INSPECTION case has no XTF-transferable shape (a decomposed boundary ring list)."""
    builder = _build_fixture("inspection_of_surface")
    view = _view(builder, "ZoneBoundary")
    transfer = _transfer(_b("b1", "x", "y"))  # never actually read - rejected before touching data

    with pytest.raises(ValueError, match="SURFACE"):
        write_view_basket(view, transfer, bid="basket-1", symbol_table=builder.symbol_table)
