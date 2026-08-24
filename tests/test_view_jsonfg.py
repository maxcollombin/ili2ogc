"""Backlog item 8, Lot C - `.xtf` -> JSON-FG, PROJECTION OF/JOIN OF VIEW evaluator.

See `interlis.convert.jsonfg.evaluate_view`/`unsupported_view_reason` and
.claude/PROGRESS.md (item 8) for scope: an in-memory cartesian-product
join over already-parsed `XtfObject`s, no `WHERE` support (a VIEW with a
`WHERE` is explicitly excluded rather than evaluated against the
currently-broken `Expression` tree, see .claude/HANDOFF.md).
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
  END Views;
END Test.
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

    assert feature["x-interlis-join-members"] == [
        {"featureType": "B", "id": "b1"},
        {"featureType": "C", "id": "c1"},
    ]


def test_join_or_null_placeholder_omitted_from_join_members():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VJN")
    transfer = _transfer(_b("b1", "x"))  # no C objects, C is (OR NULL)

    [feature] = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert feature["x-interlis-join-members"] == [{"featureType": "B", "id": "b1"}]


def test_projection_feature_has_no_join_members_marker():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VP")
    transfer = _transfer(_b("b1", "x"))

    [feature] = evaluate_view(view, transfer, symbol_table=builder.symbol_table)

    assert "x-interlis-join-members" not in feature


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


def test_where_view_is_unsupported_with_clear_reason():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VW")

    reason = unsupported_view_reason(view)

    assert reason is not None
    assert "WHERE" in reason


def test_evaluate_view_raises_for_a_where_view_rather_than_silently_misevaluating():
    builder = _build(_VIEW_MODEL)
    view = _view(builder, "VW")
    transfer = _transfer(_b("b1", "x"), _c("c1", "x"))

    try:
        evaluate_view(view, transfer, symbol_table=builder.symbol_table)
    except ValueError as exc:
        assert "WHERE" in str(exc)
    else:
        raise AssertionError("expected ValueError for a WHERE-clause view")


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
