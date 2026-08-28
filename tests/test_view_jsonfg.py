"""Backlog item 8, Lot C - `.xtf` -> JSON-FG, PROJECTION OF/JOIN OF VIEW evaluator.

See `interlis.convert.jsonfg.evaluate_view`/`unsupported_view_reason` and
.claude/PROGRESS.md (item 8) for scope: an in-memory cartesian-product
join over already-parsed `XtfObject`s, narrowed by a translatable `WHERE`
subset (`And`/`Or`-joined relational comparisons of two plain paths, a
bare base alias denoting that object's OID, one hop to an
attribute/reference on it - the FGDM4GS `Seg->OfRoad == Road` idiom). A
`WHERE` outside that subset still leaves the whole VIEW skipped with a
clear diagnostic.
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonfg import evaluate_view, transfer_to_feature_collection, unsupported_view_reason
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_text
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_VIEW_MODEL = """INTERLIS 2.4;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
    CLASS C =
      Attr2: TEXT*20;
    END C;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
    VIEW VJ
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJ;
    VIEW VJN
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C (OR NULL);
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJN;
    VIEW VW
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE B->Attr1 == C->Attr2;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VW;
    VIEW VU
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE NOT (B->Attr1 == C->Attr2);
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VU;
  END Views;
END Test.
"""

_REFERENCE_JOIN_MODEL = """INTERLIS 2.4;
MODEL Roads AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Road =
      RoadName : MANDATORY TEXT*40;
    END Road;
    CLASS Segment =
      SegNr : MANDATORY 0 .. 999;
      OfRoad : MANDATORY REFERENCE TO Road;
    END Segment;
  END T;
  TOPIC V =
    DEPENDS ON Roads.T;
    VIEW RoadSegments
      JOIN OF Segment ~ Roads.T.Segment, Road ~ Roads.T.Road;
      WHERE Segment -> OfRoad == Road;
      =
      ATTRIBUTE
        ALL OF Segment;
        ALL OF Road;
    END RoadSegments;
  END V;
END Roads.
"""


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _view(builder, name: str) -> MetaInstance:
    for inst in builder.symbol_table.all_registered():
        if getattr(inst, "_qualified_class", None) == "IlisMeta16.ModelData.View" and inst.Name == name:
            return inst
    raise AssertionError(f"no View named {name!r} built")


def _node(tag: str, text: str) -> RawNode:
    return RawNode(tag=tag, text=text, attrib={}, children=[])


def _b(tid: str, attr1: str) -> XtfObject:
    return XtfObject(tid=tid, qualified_class="Test.Base.B", attributes={"Attr1": [_node("Attr1", attr1)]})


def _c(tid: str, attr2: str) -> XtfObject:
    return XtfObject(tid=tid, qualified_class="Test.Base.C", attributes={"Attr2": [_node("Attr2", attr2)]})


def _transfer(*objects: XtfObject) -> XtfTransfer:
    basket = XtfBasket(bid="bid-1", qualified_topic="Test.Base", kind=None, endstate=None, objects=list(objects))
    return XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])


def test_projection_reuses_base_objects_tagged_as_the_view():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x"), _b("b2", "y"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert [f["featureType"] for f in features] == ["VP", "VP"]
    assert [f["id"] for f in features] == ["b1", "b2"]
    assert features[0]["properties"] == {"Attr1": "x"}


def test_join_evaluates_cartesian_product_of_two_bases():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJ")
    transfer = _transfer(_b("b1", "x"), _b("b2", "y"), _c("c1", "z"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert {f["id"] for f in features} == {"b1_c1", "b2_c1"}
    assert all(f["featureType"] == "VJ" for f in features)
    by_id = {f["id"]: f["properties"] for f in features}
    assert by_id["b1_c1"] == {"Attr1": "x", "Attr2": "z"}
    assert by_id["b2_c1"] == {"Attr1": "y", "Attr2": "z"}


def test_join_feature_carries_join_members_pointing_back_to_editable_base_features():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJ")
    transfer = _transfer(_b("b1", "x"), _c("c1", "z"))

    [feature] = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert feature["x-join-members"] == [
        {"featureType": "B", "id": "b1"},
        {"featureType": "C", "id": "c1"},
    ]


def test_join_or_null_placeholder_omitted_from_join_members():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJN")
    transfer = _transfer(_b("b1", "x"))  # no C objects, C is (OR NULL)

    [feature] = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert feature["x-join-members"] == [{"featureType": "B", "id": "b1"}]


def test_projection_feature_has_no_join_members_marker():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x"))

    [feature] = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert "x-join-members" not in feature


def test_join_without_or_null_and_empty_base_yields_no_features():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJ")
    transfer = _transfer(_b("b1", "x"))  # no C objects at all

    assert evaluate_view(view, transfer, symbol_table=builder.symbol_table) == []


def test_join_or_null_keeps_combinations_when_base_has_no_objects():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJN")
    transfer = _transfer(_b("b1", "x"), _b("b2", "y"))  # no C objects, C is (OR NULL)

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert {f["id"] for f in features} == {"b1", "b2"}
    assert all(f["properties"] == {"Attr1": v} for f, v in zip(features, ("x", "y")))


def test_where_join_filters_the_cartesian_product_on_a_scalar_match():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VW")  # WHERE B->Attr1 == C->Attr2
    transfer = _transfer(_b("b1", "x"), _b("b2", "y"), _c("c1", "x"), _c("c2", "z"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    # 2x2 cartesian product, only the b1/c1 pair has Attr1 == Attr2 == "x".
    assert {f["id"] for f in features} == {"b1_c1"}
    assert unsupported_view_reason(view) is None


def test_where_reference_join_keeps_only_matching_rows():
    """The real FGDM4GS idiom: JOIN OF ... WHERE Segment->OfRoad == Road (join on the reference)."""
    builder = _build(_REFERENCE_JOIN_MODEL)
    view = _view(builder, "RoadSegments")

    def _seg(tid: str, nr: str, of_road: str) -> XtfObject:
        return XtfObject(
            tid=tid, qualified_class="Roads.T.Segment",
            attributes={"SegNr": [_node("SegNr", nr)], "OfRoad": [RawNode("OfRoad", None, {"REF": of_road}, [])]},
        )

    def _road(tid: str, name: str) -> XtfObject:
        return XtfObject(tid=tid, qualified_class="Roads.T.Road", attributes={"RoadName": [_node("RoadName", name)]})

    basket = XtfBasket(bid="b", qualified_topic="Roads.T", kind=None, endstate=None, objects=[
        _road("r1", "Main St"), _road("r2", "Side St"),
        _seg("s1", "1", "r1"), _seg("s2", "2", "r1"), _seg("s3", "3", "r2"),
    ])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    got = sorted((f["properties"]["SegNr"], f["properties"]["RoadName"]) for f in features)
    assert got == [(1, "Main St"), (2, "Main St"), (3, "Side St")]


def test_where_view_outside_the_translatable_subset_is_still_skipped_with_a_reason():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VU")  # WHERE NOT (...) - a UnaryExpr, not in the subset

    reason = unsupported_view_reason(view)
    assert reason is not None
    assert "WHERE" in reason

    transfer = _transfer(_b("b1", "x"), _c("c1", "x"))
    try:
        evaluate_view(view, transfer, symbol_table=builder.symbol_table)
    except ValueError as exc:
        assert "WHERE" in str(exc)
    else:
        raise AssertionError("expected ValueError for an un-translatable WHERE-clause view")


def test_projection_view_supported_by_unsupported_view_reason():
    builder = _build(_VIEW_MODEL)
    assert unsupported_view_reason(_view(builder, "VP")) is None
    assert unsupported_view_reason(_view(builder, "VJ")) is None


def test_transfer_to_feature_collection_folds_view_features_alongside_class_features():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x"))

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table, views=[view])

    feature_types = {f["featureType"] for f in collection["features"]}
    assert feature_types == {"B", "VP"}
    assert len(collection["features"]) == 2


def test_transfer_to_feature_collection_without_views_param_is_unchanged():
    builder = _build(_VIEW_MODEL)
    transfer = _transfer(_b("b1", "x"))

    collection = transfer_to_feature_collection(transfer, symbol_table=builder.symbol_table)

    assert len(collection["features"]) == 1
    assert collection["features"][0]["featureType"] == "B"
