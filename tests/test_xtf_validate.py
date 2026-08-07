"""Tests du validateur semantique XTF (Lot 30, src/interlis/xtf/{schema,validate}.py)
contre le schema reel construit depuis tests/fixtures/minimal_model.ili (Person:
Name MANDATORY TEXT, BirthYear 1800..2100, Kind (Adult,Child) - couvre les 3
verifications de type de base + MANDATORY + classe/attribut inconnus en une
seule fixture reutilisee de test_model_builder_minimal.py). Construit des
XtfObject/RawNode synthetiques directement en Python plutot qu'un vrai .xtf
sur disque - la couche structurelle (parse.py) est deja testee separement
(test_xtf_parse.py), ce fichier teste uniquement le croisement schema/donnees."""
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_file
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.validate import validate_transfer

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURE = Path(__file__).parent / "fixtures/minimal_model.ili"

CLASS_NAME = "MinimalTest.MainTopic.Person"


@pytest.fixture(scope="module")
def builder():
    tree, errors = parse_file(FIXTURE)
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


def _text_attr(name: str, text: str | None) -> tuple[str, list[RawNode]]:
    return name, [RawNode(tag=name, text=text, attrib={}, children=[])]


def _object(tid: str, attrs: dict[str, str | None]) -> XtfObject:
    return XtfObject(
        tid=tid, qualified_class=CLASS_NAME,
        attributes=dict(_text_attr(name, text) for name, text in attrs.items()),
    )


def _transfer(*objects: XtfObject) -> XtfTransfer:
    basket = XtfBasket(bid="b1", qualified_topic="MinimalTest.MainTopic", kind=None, endstate=None, objects=list(objects))
    return XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])


def _messages(issues, *, attribute: str | None = None, severity: str | None = None) -> list[str]:
    return [
        i.message for i in issues
        if (attribute is None or i.attribute == attribute) and (severity is None or i.severity == severity)
    ]


def test_valid_object_has_no_issues(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "1990", "Kind": "Adult"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    assert issues == []


def test_missing_mandatory_attribute_flagged(builder):
    transfer = _transfer(_object("t1", {"BirthYear": "1990", "Kind": "Adult"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="Name", severity="error")
    assert any("MANDATORY" in m for m in msgs)


def test_numeric_out_of_range_flagged(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "1500"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="BirthYear", severity="error")
    assert any("Min" in m for m in msgs)


def test_numeric_non_numeric_value_flagged(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "not-a-number"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="BirthYear", severity="error")
    assert any("non numerique" in m for m in msgs)


def test_enum_invalid_value_flagged(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "Kind": "Robot"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="Kind", severity="error")
    assert any("enumeration" in m for m in msgs)


def test_unknown_attribute_flagged(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "Nickname": "Al"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="Nickname", severity="warning")
    assert any("absent du schema" in m for m in msgs)


def test_unknown_class_flagged(builder):
    obj = XtfObject(tid="t1", qualified_class="MinimalTest.MainTopic.Ghost", attributes={})
    transfer = _transfer(obj)
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "absente du schema" in issues[0].message


# --- Lot 31 : resolution TID/REF cross-panier (tests/fixtures/xtf/reference_model.ili :
# Indicator.RefLocation, REFERENCE TO Location) ---

REF_FIXTURE = Path(__file__).parent / "fixtures/xtf/reference_model.ili"
INDICATOR_CLASS = "RefTest.MainTopic.Indicator"
LOCATION_CLASS = "RefTest.MainTopic.Location"


@pytest.fixture(scope="module")
def ref_builder():
    tree, errors = parse_file(REF_FIXTURE)
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


def _ref_attr(name: str, target_tid: str) -> tuple[str, list[RawNode]]:
    """Forme reelle "REF nu sur le noeud du role" (Lot 31, confirmee sur
    rMeasurementLocation) - la plus simple des 3 formes, suffisante pour
    exercer _extract_reference (deja testee structurellement par ailleurs)."""
    return name, [RawNode(tag=name, text=None, attrib={"REF": target_tid}, children=[])]


def test_reference_resolved_within_same_basket_has_no_issue(ref_builder):
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_across_different_baskets_has_no_issue(ref_builder):
    """Une reference peut viser un objet d'un AUTRE panier du meme
    transfert (cas reel confirme, voir docstring _build_tid_index) -
    l'index doit couvrir TOUS les paniers, pas seulement celui de
    l'objet source."""
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket_a = XtfBasket(bid="a", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location])
    basket_b = XtfBasket(bid="b", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket_a, basket_b])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_target_not_found_is_warning_not_error(ref_builder):
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="warning")
    assert any("introuvable dans ce transfert" in m for m in msgs)
    assert _messages(issues, attribute="RefLocation", severity="error") == []
