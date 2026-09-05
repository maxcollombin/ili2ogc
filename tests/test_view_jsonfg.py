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

from conftest import build_from_text as _build

from interlis.convert.jsonfg import evaluate_view, transfer_to_feature_collection, unsupported_view_reason
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer

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
    VIEW VN
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE NOT (B->Attr1 == C->Attr2);
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VN;
    VIEW VU
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE INTERLIS.len(B->Attr1) == C->Attr2;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VU;
    VIEW VPR
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        attr1_renamed := B -> Attr1;
    END VPR;
    VIEW VJR
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      =
      ATTRIBUTE
        a1 := B -> Attr1;
        a2 := C -> Attr2;
    END VJR;
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


def test_projection_with_renamed_attribute_reads_the_base_wire_value():
    """`Name := Class -> Attr` with `Name != Attr` (any case/spelling) must still resolve.

    Regression for a real bug found via item 13's VIEW-corpus pipeline
    (`docs/fgdm4gs-view-strategy.md`) against real `xtf_corpus/geoadmin`
    data: `object_to_feature`/`_members_value` look up each property by
    the VIEW's OWN schema attribute name against the base object's RAW
    wire attribute names - correct only when they match (`ALL OF`, or a
    same-name reassignment like DMAV's `NBIdent := NBIdent`), silently
    empty otherwise. Every one of the 10 `scripts/generate_view_corpus.py`
    derived models names its VIEW attributes after a service's field
    names (e.g. `roadnumber := RoadSegment -> RoadNumber`), never matching
    the base's own spelling - `MainRoads_LV95_V1_1_d.view_roadsegment`
    produced 135/135 empty `properties` before this fix.
    """
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VPR")
    transfer = _transfer(_b("b1", "x"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert features[0]["properties"] == {"attr1_renamed": "x"}


def test_join_with_renamed_attributes_reads_each_base_wire_value():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJR")
    transfer = _transfer(_b("b1", "x"), _c("c1", "z"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert features[0]["properties"] == {"a1": "x", "a2": "z"}


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
            tid=tid,
            qualified_class="Roads.T.Segment",
            attributes={"SegNr": [_node("SegNr", nr)], "OfRoad": [RawNode("OfRoad", None, {"REF": of_road}, [])]},
        )

    def _road(tid: str, name: str) -> XtfObject:
        return XtfObject(tid=tid, qualified_class="Roads.T.Road", attributes={"RoadName": [_node("RoadName", name)]})

    basket = XtfBasket(
        bid="b",
        qualified_topic="Roads.T",
        kind=None,
        endstate=None,
        objects=[
            _road("r1", "Main St"),
            _road("r2", "Side St"),
            _seg("s1", "1", "r1"),
            _seg("s2", "2", "r1"),
            _seg("s3", "3", "r2"),
        ],
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    got = sorted((f["properties"]["SegNr"], f["properties"]["RoadName"]) for f in features)
    assert got == [(1, "Main St"), (2, "Main St"), (3, "Side St")]


def test_where_not_is_evaluated():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VN")  # WHERE NOT (B->Attr1 == C->Attr2)
    transfer = _transfer(_b("b1", "x"), _b("b2", "y"), _c("c1", "x"))

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    assert {f["id"] for f in features} == {"b2_c1"}  # b1/c1 both "x" is excluded by NOT


def test_where_with_a_function_call_is_still_skipped_with_a_reason():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VU")  # WHERE INTERLIS.len(...) - a FunctionCall, outside evaluate_expression's scope

    reason = unsupported_view_reason(view)
    assert reason is not None
    assert "WHERE" in reason

    transfer = _transfer(_b("b1", "x"), _c("c1", "x"))
    try:
        evaluate_view(view, transfer, symbol_table=builder.symbol_table)
    except ValueError as exc:
        assert "WHERE" in str(exc)
    else:
        raise AssertionError("expected ValueError for a WHERE with a function call")


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
    # Homogeneous collection: hoisted to the collection, removed from the feature.
    assert collection["featureType"] == "B"
    assert "featureType" not in collection["features"][0]


_KINDS_MODEL = """INTERLIS 2.4;
MODEL Kinds AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS X = A : TEXT*10; END X;
    CLASS Y = A : TEXT*10; END Y;
    CLASS Owner =
      Name : MANDATORY TEXT*10;
      Items : BAG {0..*} OF Kinds.Base.Item;
    END Owner;
    STRUCTURE Item = Label : MANDATORY TEXT*10; END Item;
  END Base;
  TOPIC V =
    DEPENDS ON Kinds.Base;
    VIEW U UNION OF X ~ Kinds.Base.X, Y ~ Kinds.Base.Y; = ATTRIBUTE ALL OF X; END U;
    VIEW G AGGREGATION OF Kinds.Base.X ALL; = ATTRIBUTE ALL OF X; END G;
    VIEW Insp INSPECTION OF Kinds.Base.Owner -> Items; = ATTRIBUTE ALL OF It; END Insp;
  END V;
END Kinds.
"""


def test_union_concatenates_every_base_extension():
    builder = _build(_KINDS_MODEL)
    view = _view(builder, "U")
    x1 = XtfObject(tid="x1", qualified_class="Kinds.Base.X", attributes={"A": [_node("A", "p")]})
    y1 = XtfObject(tid="y1", qualified_class="Kinds.Base.Y", attributes={"A": [_node("A", "q")]})
    basket = XtfBasket(bid="b", qualified_topic="Kinds.Base", kind=None, endstate=None, objects=[x1, y1])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    assert sorted(f["properties"]["A"] for f in features) == ["p", "q"]
    assert all(f["featureType"] == "U" for f in features)


def test_aggregation_keeps_one_feature_per_distinct_row():
    builder = _build(_KINDS_MODEL)
    view = _view(builder, "G")
    objs = [
        XtfObject(tid=f"x{i}", qualified_class="Kinds.Base.X", attributes={"A": [_node("A", v)]})
        for i, v in enumerate(("p", "p", "q"))
    ]
    basket = XtfBasket(bid="b", qualified_topic="Kinds.Base", kind=None, endstate=None, objects=objs)
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    assert sorted(f["properties"]["A"] for f in features) == ["p", "q"]  # the duplicate "p" row collapsed


def test_inspection_yields_one_feature_per_bag_element():
    builder = _build(_KINDS_MODEL)
    view = _view(builder, "Insp")
    owner = XtfObject(
        tid="o1",
        qualified_class="Kinds.Base.Owner",
        attributes={
            "Name": [_node("Name", "Alice")],
            # real wire form: ONE wrapper named after the attribute, each occurrence a direct child
            "Items": [
                RawNode(
                    "Items",
                    None,
                    {},
                    [
                        RawNode("Kinds.Base.Item", None, {}, [RawNode("Label", "one", {}, [])]),
                        RawNode("Kinds.Base.Item", None, {}, [RawNode("Label", "two", {}, [])]),
                    ],
                )
            ],
        },
    )
    basket = XtfBasket(bid="b", qualified_topic="Kinds.Base", kind=None, endstate=None, objects=[owner])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    features = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert unsupported_view_reason(view) is None
    assert sorted(f["properties"]["Label"] for f in features) == ["one", "two"]
