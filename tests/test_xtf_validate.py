"""Tests for the semantic XTF validator (src/interlis/xtf/{schema,validate}.py)
against the real schema built from tests/fixtures/minimal_model.ili (Person:
Name MANDATORY TEXT, BirthYear 1800..2100, Kind (Adult,Child) - covers the 3
basic type checks + MANDATORY + unknown class/attribute in a single fixture
reused from test_model_builder_minimal.py). Builds synthetic XtfObject/RawNode
directly in Python rather than a real .xtf on disk - the structural layer
(parse.py) is already tested separately (test_xtf_parse.py), this file only
tests the schema/data cross-check."""

from pathlib import Path

import pytest
from conftest import build_from_file

from interlis.builder.repository import ModelRepository
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.validate import validate_transfer

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures/minimal_model.ili"

CLASS_NAME = "MinimalTest.MainTopic.Person"


@pytest.fixture(scope="module")
def builder():
    return build_from_file(FIXTURE)


def _text_attr(name: str, text: str | None) -> tuple[str, list[RawNode]]:
    return name, [RawNode(tag=name, text=text, attrib={}, children=[])]


def _object(tid: str, attrs: dict[str, str | None]) -> XtfObject:
    return XtfObject(
        tid=tid,
        qualified_class=CLASS_NAME,
        attributes=dict(_text_attr(name, text) for name, text in attrs.items()),
    )


def _transfer(*objects: XtfObject) -> XtfTransfer:
    basket = XtfBasket(
        bid="b1", qualified_topic="MinimalTest.MainTopic", kind=None, endstate=None, objects=list(objects)
    )
    return XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])


def _messages(issues, *, attribute: str | None = None, severity: str | None = None) -> list[str]:
    return [
        i.message
        for i in issues
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


def test_numeric_within_rounding_tolerance_has_no_issue(builder):
    """RULE #4, eCH-0031 V2.1.0 §2.8 "Umgang mit Rundung von numerischen
    Werten und Koordinaten" / §4.3.11.4 "Codierung von numerischen
    Datentypen": a value may be transferred with a precision HIGHER than
    the domain's own (BirthYear: 1800..2100, 0 decimals) - all that
    matters is that it rounds into range. 1799.6 rounds to 1800, must NOT
    be flagged < Min."""
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "1799.6"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    assert _messages(issues, attribute="BirthYear") == []


def test_numeric_beyond_rounding_tolerance_still_flagged(builder):
    """Regression-guard : au-dela de la demi-unite de tolerance (0.5 pour 0
    decimale), une valeur qui arrondit encore HORS plage reste une erreur -
    1799.4 arrondit a 1799, toujours < Min 1800."""
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "1799.4"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="BirthYear", severity="error")
    assert any("Min" in m for m in msgs)


def test_numeric_non_numeric_value_flagged(builder):
    transfer = _transfer(_object("t1", {"Name": "Alice", "BirthYear": "not-a-number"}))
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
    msgs = _messages(issues, attribute="BirthYear", severity="error")
    assert any("not numeric" in m for m in msgs)


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
    assert "absent from the resolved schema" in issues[0].message


# --- Cross-basket TID/REF resolution (tests/fixtures/xtf/reference_model.ili :
# Indicator.RefLocation, REFERENCE TO Location) ---

REF_FIXTURE = Path(__file__).parent / "fixtures/xtf/reference_model.ili"
INDICATOR_CLASS = "RefTest.MainTopic.Indicator"
LOCATION_CLASS = "RefTest.MainTopic.Location"


@pytest.fixture(scope="module")
def ref_builder():
    return build_from_file(REF_FIXTURE)


def _ref_attr(name: str, target_tid: str) -> tuple[str, list[RawNode]]:
    """Real-world form "bare REF on the role node" (confirmed on
    rMeasurementLocation) - the simplest of the 3 forms, sufficient to
    exercise _extract_reference (already tested structurally elsewhere)."""
    return name, [RawNode(tag=name, text=None, attrib={"REF": target_tid}, children=[])]


def test_reference_resolved_within_same_basket_has_no_issue(ref_builder):
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_across_different_baskets_has_no_issue(ref_builder):
    """A reference can target an object in a DIFFERENT basket of the same
    transfer (confirmed real case, see _build_tid_index's docstring) -
    the index must cover ALL baskets, not just the source object's own."""
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket_a = XtfBasket(bid="a", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location])
    basket_b = XtfBasket(bid="b", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket_a, basket_b])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_target_not_found_is_warning_not_error(ref_builder):
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="warning")
    assert any("not found in this transfer" in m for m in msgs)
    assert _messages(issues, attribute="RefLocation", severity="error") == []


# --- Class compatibility of a RESOLVED reference (RefLocation declares
# REFERENCE TO Location - fixture extended with SpecialLocation
# EXTENDS Location, tests/fixtures/xtf/reference_model.ili) ---


def test_reference_resolved_to_declared_class_has_no_issue(ref_builder):
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_to_subclass_is_compatible(ref_builder):
    """Standard INTERLIS polymorphism: a reference declared to Location
    must accept a real target of type SpecialLocation (EXTENDS Location)
    without flagging it as incompatible."""
    special = XtfObject(
        tid="loc-1",
        qualified_class="RefTest.MainTopic.SpecialLocation",
        attributes=dict([_text_attr("Name", "Bern"), _text_attr("Detail", "capital")]),
    )
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[special, indicator]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_to_incompatible_class_is_error(ref_builder):
    """RefLocation resout vers un objet REELLEMENT present dans le
    transfert (donc information COMPLETE, pas ambigu comme le cas "REF
    introuvable") mais de classe Indicator, sans rapport avec Location
    (ni identique, ni sous-classe) - doit devenir une `error`."""
    other_indicator = XtfObject(
        tid="ind-2",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "1")]),
    )
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "ind-2")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[other_indicator, indicator]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="error")
    assert any("incompatible" in m for m in msgs)


# --- Embedded association roles as pseudo-attributes (tests/fixtures/xtf/
# reference_model.ili : ASSOCIATION Location_Indicator = rLocation -<#>
# Location; rIndicator -- {0..*} Indicator; - same shape as ASSOCIATION
# MeasurementLocation_Indicator in the real RoadTrafficCensus_V1_1 corpus) ---


def test_embedded_role_resolved_is_not_unknown_attribute(ref_builder):
    """rLocation s'embarque sur Indicator (role rIndicator, cote {0..*}) -
    doit etre reconnu comme un attribut de schema valide, PAS "inconnu"."""
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "loc-1")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="rLocation") == []


def test_embedded_role_not_exposed_on_opposite_class(ref_builder):
    """rLocation must NOT appear as a pseudo-attribute of Location itself
    (it embeds only on the Indicator side, the class whose opposite role
    - rIndicator - has cardinality {0..*}) - even though Location does
    carry ANOTHER role genuinely embedded on itself (rNote, ASSOCIATION
    Location_Note, see the tests further below)."""
    from interlis.xtf.schema import embedded_roles_of, resolve_class

    location_cls = resolve_class(LOCATION_CLASS, symbol_table=ref_builder.symbol_table, repository=None)
    assert "rLocation" not in embedded_roles_of(location_cls, ref_builder.symbol_table)


def test_embedded_role_unresolved_ref_is_warning(ref_builder):
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rLocation", severity="warning")
    assert any("not found in this transfer" in m for m in msgs)


# --- embedded_roles_of must follow the EXTENDS chain (tests/fixtures/
# xtf/reference_model.ili : ASSOCIATION Location_Note embeds rNote on
# Location - CLASS SpecialLocation EXTENDS Location (already used by the
# tests above) must therefore INHERIT this pseudo-attribute, confirmed real
# (RULE #4) as the cause of 93-95% of the warnings on
# IVS_V2_1_national/regional_lokal_LV95.xtf - CLASS ivs_punkt-
# objekte_base (ABSTRACT) carries the embedded role, the real XTF objects
# are all the concrete subclass ivs_punktobjekte_lv95/_lv03.) ---

SPECIAL_LOCATION_CLASS = "RefTest.MainTopic.SpecialLocation"


def test_embedded_role_from_base_class_is_inherited_by_subclass(ref_builder):
    from interlis.xtf.schema import embedded_roles_of, resolve_class

    location_cls = resolve_class(LOCATION_CLASS, symbol_table=ref_builder.symbol_table, repository=None)
    special_cls = resolve_class(SPECIAL_LOCATION_CLASS, symbol_table=ref_builder.symbol_table, repository=None)
    assert "rNote" in embedded_roles_of(location_cls, ref_builder.symbol_table)
    assert "rNote" in embedded_roles_of(special_cls, ref_builder.symbol_table)


def test_embedded_role_from_base_class_resolved_on_subclass_instance_has_no_issue(ref_builder):
    """Regression bout-en-bout (RULE #4, meme forme que le corpus reel
    IVS_V2_1) : un objet de la SOUS-CLASSE porte le REF du role embarque
    declare sur la classe de BASE - doit resoudre sans issue, PAS
    "attribut absent du schema"."""
    note = XtfObject(
        tid="note-1", qualified_class="RefTest.MainTopic.Note", attributes=dict([_text_attr("Text", "hello")])
    )
    special = XtfObject(
        tid="special-1",
        qualified_class=SPECIAL_LOCATION_CLASS,
        attributes=dict([_text_attr("Name", "Bern"), _ref_attr("rNote", "note-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[note, special])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="rNote") == []


# --- EXTERNAL status of an embedded association role itself
# (tests/fixtures/xtf/reference_model.ili : ASSOCIATION
# Location_ExternalIndicator, rExtLocation (EXTERNAL) -<#> Location) ---


def test_embedded_role_external_unresolved_ref_reports_catalogue_expected(ref_builder):
    """rExtLocation carries its OWN (EXTERNAL) clause on the role - an
    unresolved REF must be flagged as the EXPECTED normal situation
    (same message as an ordinary REFERENCE TO (EXTERNAL)), not as a
    signal of bad data."""
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rExtLocation", "ext.catalog.1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rExtLocation", severity="warning")
    assert any("declared reference (EXTERNAL)" in m and "normal" in m for m in msgs)


def test_embedded_role_non_external_unresolved_ref_flags_data_issue(ref_builder):
    """rLocation (meme association Location_Indicator qu'avant) n'a PAS de
    clause EXTERNAL - un REF non resolu doit continuer a signaler que la
    cible DEVRAIT normalement etre dans ce meme panier, PAS regresser vers
    le message neutre "indetermine" maintenant que le statut des roles est
    resolu."""
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rLocation", severity="warning")
    assert any("NOT declared as EXTERNAL" in m for m in msgs)


# --- Catalogue objects (REFERENCE TO (EXTERNAL), --catalog) ---


def test_non_external_unresolved_ref_flags_data_issue(ref_builder):
    """RefLocation (pas de clause EXTERNAL) : un REF non resolu doit rester
    `warning` (RULE #5, jamais `error` sans catalogue charge) mais le
    message doit signaler que la cible DEVRAIT normalement etre dans ce
    meme panier (eCH-0031 V2.1.0 3.6.3)."""
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="warning")
    assert any("NOT declared as EXTERNAL" in m for m in msgs)


def test_external_unresolved_ref_reports_catalogue_expected(ref_builder):
    """RefCatalogItem : REFERENCE TO (EXTERNAL) - un REF non resolu doit
    rester `warning` mais le message doit signaler que c'est la situation
    NORMALE attendue pour une reference-catalogue (pas un signal de donnee
    incorrecte)."""
    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefCatalogItem", "ext.catalog.99")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefCatalogItem", severity="warning")
    assert any("declared reference (EXTERNAL)" in m and "normal" in m for m in msgs)


def test_external_ref_resolved_via_catalog_argument_has_no_issue(ref_builder):
    """The TID of an EXTERNAL catalogue object typically lives in a
    SEPARATE `.xtf` file from the main transfer (`--catalog`) - passing
    that catalogue transfer via `catalogs=` must make it resolvable,
    exactly like an object of the main transfer."""
    catalog_item = XtfObject(
        tid="ext.catalog.99", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Catalogue")])
    )
    catalog_basket = XtfBasket(
        bid="cat", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[catalog_item]
    )
    catalog_transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[catalog_basket])

    indicator = XtfObject(
        tid="ind-1",
        qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefCatalogItem", "ext.catalog.99")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    issues_without_catalog = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues_without_catalog, attribute="RefCatalogItem") != []

    issues_with_catalog = validate_transfer(
        transfer,
        symbol_table=ref_builder.symbol_table,
        catalogs=[catalog_transfer],
    )
    assert _messages(issues_with_catalog, attribute="RefCatalogItem") == []


# --- 3rd XTF encoding form (CLASS RESTRICTION(A; B; ...) on 1-attribute
# STRUCTUREs, bare text value - tests/fixtures/xtf/restriction_model.ili :
# `Selector = CLASS RESTRICTION(sColor; sSize)` (2 fully verifiable
# candidates, each an inline enum) and `SelectorWithExternal = CLASS
# RESTRICTION(sColor; sExternal)` (1 verifiable candidate + 1 whose
# internal type is a reference, never verifiable by this mechanism) ---

RESTRICTION_FIXTURE = Path(__file__).parent / "fixtures/xtf/restriction_model.ili"
WIDGET_CLASS = "RestrictionTest.MainTopic.Widget"
WIDGET_EXTERNAL_CLASS = "RestrictionTest.MainTopic.WidgetWithExternal"


@pytest.fixture(scope="module")
def restriction_builder():
    return build_from_file(RESTRICTION_FIXTURE)


def test_restriction_text_matching_first_candidate_has_no_issue(restriction_builder):
    """La valeur "Red" appartient au domaine inline de sColor - le 1er candidat de
    `RESTRICTION(sColor; sSize)`."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Red")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    assert _messages(issues, attribute="Sel") == []


def test_restriction_text_matching_second_candidate_has_no_issue(restriction_builder):
    """The value "Small" belongs to sSize's inline domain - the 2nd
    candidate, NOT the 1st (regression guard: the naive SEMI-splitting
    used to silently truncate `_build_domain_class_restriction` to its
    1st candidate only, before the LPAR/RPAR depth fix)."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Small")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    assert _messages(issues, attribute="Sel") == []


def test_restriction_text_matching_no_candidate_is_warning(restriction_builder):
    """La valeur "Purple" n'appartient a AUCUN des 2 domaines inline (sColor:
    Red/Blue, sSize: Small/Large) - les 2 candidats sont PLEINEMENT
    verifiables (enums inline, rien d'externe) -> `warning`, pas `info`."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Purple")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    msgs = _messages(issues, attribute="Sel", severity="warning")
    assert any("CLASS RESTRICTION" in m and "matches none of the 2 declared candidate" in m for m in msgs)
    assert _messages(issues, attribute="Sel", severity="error") == []


def test_restriction_text_with_unverifiable_candidate_is_info_not_warning(restriction_builder):
    """`SelectorWithExternal = CLASS RESTRICTION(sColor; sExternal)` : une
    valeur qui ne correspond pas au candidat verifiable (sColor) NE DOIT
    PAS devenir `warning` si l'AUTRE candidat (sExternal, dont l'attribut
    interne est une REFERENCE, jamais verifiable par ce mecanisme) reste
    non tranche - RULE #5, rester `info` (statut reellement indetermine)
    plutot que d'affirmer a tort une non-conformite."""
    obj = XtfObject(
        tid="w1", qualified_class=WIDGET_EXTERNAL_CLASS, attributes=dict([_text_attr("Sel", "not-a-color")])
    )
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    msgs = _messages(issues, attribute="Sel", severity="info")
    assert any("1/2 verifiable candidate" in m for m in msgs)
    assert _messages(issues, attribute="Sel", severity="warning") == []


# --- Geometry/coordinates (tests/fixtures/xtf/geometry_model.ili :
# Coord2 = COORD 0..100, 0..200 ; MultiCoord2 = MULTICOORD (same range) ;
# Line = POLYLINE VERTEX Coord2 ; MultiLine = MULTIPOLYLINE VERTEX Coord2 ;
# Area = SURFACE VERTEX Coord2 - Point.Pos/MultiPoint.Pos/Way.Geom/
# MultiWay.Geom/Zone.Geom, one attribute of each form) ---

GEOMETRY_FIXTURE = Path(__file__).parent / "fixtures/xtf/geometry_model.ili"
POINT_CLASS = "GeomTest.MainTopic.Point"
MULTIPOINT_CLASS = "GeomTest.MainTopic.MultiPoint"
WAY_CLASS = "GeomTest.MainTopic.Way"
DIRECTEDWAY_CLASS = "GeomTest.MainTopic.DirectedWay"
MULTIWAY_CLASS = "GeomTest.MainTopic.MultiWay"
ZONE_CLASS = "GeomTest.MainTopic.Zone"


@pytest.fixture(scope="module")
def geometry_builder():
    return build_from_file(GEOMETRY_FIXTURE)


def _coord_node(*components: str, tag: str = "COORD") -> RawNode:
    children = [RawNode(tag=f"C{i + 1}", text=v, attrib={}, children=[]) for i, v in enumerate(components)]
    return RawNode(tag=tag, text=None, attrib={}, children=children)


def _geom_attr(name: str, inner: RawNode) -> tuple[str, list[RawNode]]:
    return name, [RawNode(tag=name, text=None, attrib={}, children=[inner])]


def _obj(cls: str, tid: str, *attr_pairs: tuple[str, list[RawNode]]) -> XtfObject:
    return XtfObject(tid=tid, qualified_class=cls, attributes=dict(attr_pairs))


def _validate_one(obj: XtfObject, symbol_table):
    basket = XtfBasket(bid="b1", qualified_topic="GeomTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    return validate_transfer(transfer, symbol_table=symbol_table)


def test_coord_within_range_has_no_issue(geometry_builder):
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("50.0", "100.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Pos") == []


def test_coord_within_rounding_tolerance_has_no_issue(geometry_builder):
    """RULE #4, eCH-0031 V2.1.0 §2.8/§4.3.11.4 (same reasoning as
    test_numeric_within_rounding_tolerance_has_no_issue): Coord2 (3
    decimals, `0.000 .. 100.000`) - a value transferred with higher
    precision that still rounds into range (100.0004 -> 100.000, exactly
    Max) must NOT be flagged."""
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("100.0004", "100.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Pos") == []


def test_coord_beyond_rounding_tolerance_still_flagged(geometry_builder):
    """Regression-guard : au-dela de la tolerance (0.0005 pour 3
    decimales), la valeur arrondit encore AU-DELA du Max declare -
    100.0006 -> 100.001, toujours hors plage."""
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("100.0006", "100.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Pos", severity="error")
    assert any("Max" in m for m in msgs)


def test_coord_out_of_range_flagged(geometry_builder):
    """C1 (plage 0..100) depasse le Max declare."""
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("150.0", "100.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Pos", severity="error")
    assert any("Max" in m for m in msgs)


def test_coord_non_numeric_component_flagged(geometry_builder):
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("abc", "100.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Pos", severity="error")
    assert any("not numeric" in m for m in msgs)


def test_coord_component_count_mismatch_flagged(geometry_builder):
    """CoordType.Axis declares 2 axes - only C1 present must be flagged
    (direct regression guard for the CoordType.Axis/wrap fix - without
    it, `axes` always stayed empty and this mismatch could never have
    been detected)."""
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("50.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Pos", severity="error")
    assert any("C component(s)" in m for m in msgs)


def test_multicoord_valid_has_no_issue(geometry_builder):
    inner = RawNode(
        tag="MULTICOORD",
        text=None,
        attrib={},
        children=[
            _coord_node("10.0", "20.0"),
            _coord_node("30.0", "40.0"),
        ],
    )
    obj = _obj(MULTIPOINT_CLASS, "mp1", _geom_attr("Pos", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Pos") == []


def test_polyline_valid_has_no_issue(geometry_builder):
    """Also exercises the LineType.CoordType fix (VERTEX Coord2, never
    attached before it) - without it, `axes` would be empty and C1's Max
    (100.0, deliberately tested out of range below by contrast) would
    stay invisible."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("50.0", "50.0"),
        ],
    )
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_polyline_out_of_range_vertex_flagged(geometry_builder):
    """Direct proof that the LineType.CoordType fix actually feeds the
    range check on a POLYLINE segment, not just the structure."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("999.0", "50.0"),
        ],
    )
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Geom", severity="error")
    assert any("Max" in m for m in msgs)


def test_polyline_inherited_coord_type_via_extends_has_no_issue(geometry_builder):
    """DirectedLine EXTENDS Line = DIRECTED POLYLINE; (no VERTEX clause of
    its own, same shape as the real CHBase) - the axis range must be
    found by walking Super up to Line, not just come back absent."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("50.0", "50.0"),
        ],
    )
    obj = _obj(DIRECTEDWAY_CLASS, "dw1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_polyline_inherited_coord_type_out_of_range_flagged(geometry_builder):
    """Preuve directe (pas seulement l'absence de faux positif) que la
    plage HERITEE de Line est reellement appliquee a DirectedLine, pas
    seulement que la structure passe faute de plage connue."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("999.0", "50.0"),
        ],
    )
    obj = _obj(DIRECTEDWAY_CLASS, "dw1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Geom", severity="error")
    assert any("Max" in m for m in msgs)


def test_polyline_wrong_structure_flagged(geometry_builder):
    """Way.Geom attend POLYLINE (LineType.Kind=Polyline, Multi=False) -
    une balise SURFACE a sa place doit etre signalee structurellement."""
    inner = RawNode(tag="SURFACE", text=None, attrib={}, children=[])
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Geom", severity="error")
    assert any("expected POLYLINE geometry" in m for m in msgs)


def test_polyline_custom_line_form_segment_reported_as_info(geometry_builder):
    """A segment tag that's neither COORD nor ARC (eCH-0031 V2.1.0
    §4.3.11.14's LineFormSegment alternative, e.g. a custom LINE FORM
    declared via lineFormTypeDef) is not structurally validated (no
    confirmed real-world tag encoding to check it against), but must no
    longer be silently dropped from the report - it's surfaced as `info`."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            RawNode(tag="ZIGZAG", text=None, attrib={}, children=[]),
        ],
    )
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom", severity="error") == []
    infos = _messages(issues, attribute="Geom", severity="info")
    assert any("ZIGZAG" in m and "not validated" in m for m in infos)


def test_polyline_only_coord_and_arc_has_no_info_issue(geometry_builder):
    """Contrast case: a POLYLINE made only of COORD/ARC segments must not
    trigger the custom LINE FORM `info` notice."""
    inner = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("50.0", "50.0"),
        ],
    )
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom", severity="info") == []


def test_surface_boundary_custom_line_form_segment_reported_as_info(geometry_builder):
    """Same custom LINE FORM surfacing, nested inside a SURFACE/BOUNDARY
    (eCH-0031's SegmentSequence applies uniformly to both POLYLINE-typed
    attributes and SURFACE/AREA boundaries)."""
    polyline = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            RawNode(tag="ZIGZAG", text=None, attrib={}, children=[]),
            _coord_node("50.0", "50.0"),
            _coord_node("0.0", "0.0"),
        ],
    )
    boundary = RawNode(tag="BOUNDARY", text=None, attrib={}, children=[polyline])
    inner = RawNode(tag="SURFACE", text=None, attrib={}, children=[boundary])
    obj = _obj(ZONE_CLASS, "z1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom", severity="error") == []
    infos = _messages(issues, attribute="Geom", severity="info")
    assert any("ZIGZAG" in m for m in infos)


def test_surface_valid_has_no_issue(geometry_builder):
    polyline = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("50.0", "0.0"),
            _coord_node("50.0", "50.0"),
            _coord_node("0.0", "0.0"),
        ],
    )
    boundary = RawNode(tag="BOUNDARY", text=None, attrib={}, children=[polyline])
    inner = RawNode(tag="SURFACE", text=None, attrib={}, children=[boundary])
    obj = _obj(ZONE_CLASS, "z1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_multipolyline_valid_has_no_issue(geometry_builder):
    """MultiWay.Geom (the Multi fix - `presence: true` on a composed
    field was previously ALWAYS ignored by the engine - Multi held the
    LITERAL matched-token TEXT instead of a boolean)."""
    polyline_a = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("0.0", "0.0"),
            _coord_node("10.0", "10.0"),
        ],
    )
    polyline_b = RawNode(
        tag="POLYLINE",
        text=None,
        attrib={},
        children=[
            _coord_node("20.0", "20.0"),
            _coord_node("30.0", "30.0"),
        ],
    )
    inner = RawNode(tag="MULTIPOLYLINE", text=None, attrib={}, children=[polyline_a, polyline_b])
    obj = _obj(MULTIWAY_CLASS, "mw1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


# --- Embedded association role defined in an IMPORTED model
# (tests/fixtures/embedded_roles_cross_model/{base,importer}.ili) - EmbedBase
# declares ASSOCIATION Parent_Child (Children -- {0..*} Child; Parent -<#> {1}
# Parent;), thus embedding the role "Parent" on Child. EmbedImporter (the
# validation root, like SectoralPlanForRoadInfrastructure_LV95_V1_4 in the
# real corpus) IMPORTS EmbedBase but has NO association of its own - only
# EmbedBase's own symbol table (via ModelRepository, NOT EmbedImporter's)
# holds Parent_Child. Confirmed real (RULE #4) on
# 2021-01-12_SectoralPlanForRoadInfrastructure_LV95.xtf: 770 pseudo-
# attributes (Object/SectoralPlan) wrongly reported "absent from schema"
# before this fix, correctly recognized after. ---

CROSS_MODEL_FIXTURES_DIR = Path(__file__).parent / "fixtures/embedded_roles_cross_model"
EMBED_PARENT_CLASS = "EmbedBase.MainTopic.Parent"
EMBED_CHILD_CLASS = "EmbedBase.MainTopic.Child"


@pytest.fixture(scope="module")
def cross_model_builder():
    repository = ModelRepository([CROSS_MODEL_FIXTURES_DIR])
    b = build_from_file(CROSS_MODEL_FIXTURES_DIR / "importer.ili", repository=repository)
    return b, repository


def test_embedded_role_from_imported_model_is_not_unknown_attribute(cross_model_builder):
    builder, repository = cross_model_builder
    parent = XtfObject(tid="p1", qualified_class=EMBED_PARENT_CLASS, attributes=dict([_text_attr("Name", "P")]))
    child = XtfObject(
        tid="c1",
        qualified_class=EMBED_CHILD_CLASS,
        attributes=dict([_text_attr("Name", "C"), _ref_attr("Parent", "p1")]),
    )
    basket = XtfBasket(
        bid="b1", qualified_topic="EmbedBase.MainTopic", kind=None, endstate=None, objects=[parent, child]
    )
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table, repository=repository)
    assert _messages(issues, attribute="Parent") == []


def test_embedded_role_from_imported_model_missing_without_home_table_lookup(cross_model_builder):
    """Direct regression guard on schema.py: looking up the association in
    the ROOT table (EmbedImporter, with no association of its own) rather
    than the table that ACTUALLY declares it (EmbedBase, via
    home_symbol_table) fails to find the embedded role - the fault, in
    isolation from the whole validate.py layer."""
    from interlis.xtf.schema import embedded_roles_of, home_symbol_table, resolve_class

    builder, repository = cross_model_builder
    cls = resolve_class(EMBED_CHILD_CLASS, symbol_table=builder.symbol_table, repository=repository)
    assert embedded_roles_of(cls, builder.symbol_table) == {}
    home_table = home_symbol_table(EMBED_CHILD_CLASS, symbol_table=builder.symbol_table, repository=repository)
    assert set(embedded_roles_of(cls, home_table)) == {"Parent"}


# --- Recursive validation of STRUCTURE content (own attributes + BAG/LIST
# of structure, MultiValue) - tests/fixtures/xtf/
# structure_content_model.ili : STRUCTURE Note (Text: MANDATORY TEXT) ;
# STRUCTURE Address (Street: MANDATORY TEXT ; Notes: BAG {0..*} OF Note) ;
# CLASS Person (Name, HomeAddress: Address, Tags: BAG {0..*} OF Note).
# Confirmed real (RULE #1/#4) as the cause of nearly all the remaining
# `info` "type not verified"/"structure with no recognized REF" on the
# full XTF corpus before this fix (MultilingualText/ModInfo/Point/Surface/
# KGS_PBC.Objektart-EGID-Adressen...). ---

STRUCT_CONTENT_FIXTURE = Path(__file__).parent / "fixtures/xtf/structure_content_model.ili"
PERSON_CLASS = "StructContentTest.MainTopic.Person"


@pytest.fixture(scope="module")
def struct_content_builder():
    return build_from_file(STRUCT_CONTENT_FIXTURE)


def _struct_wrapper(*attr_pairs: tuple[str, list[RawNode]]) -> RawNode:
    """Une occurrence de structure DEJA "enveloppee" - equivalent XML d'un
    `<QualifiedStructName>...</QualifiedStructName>` - le nom du wrapper
    lui-meme n'est jamais verifie (voir _validate_resolved_attr/RULE #5,
    meme tolerance deja etablie pour COORD/POLYLINE sans namespace)."""
    children: list[RawNode] = []
    for _, nodes in attr_pairs:
        children.extend(nodes)
    return RawNode(tag="Wrapper", text=None, attrib={}, children=children)


def _multi_attr(name: str, *occurrences: RawNode) -> tuple[str, list[RawNode]]:
    """Attribut BAG/LIST (MultiValue) - UN SEUL element porte le nom de
    l'attribut, contenant chaque occurrence EN ENFANT DIRECT (confirme
    reel, RULE #1, `ID65.1_KGS_PBC_V2_2__20250506.xtf` : `Objektart`)."""
    return name, [RawNode(tag=name, text=None, attrib={}, children=list(occurrences))]


def _validate_struct_person(obj: XtfObject, symbol_table):
    basket = XtfBasket(bid="b1", qualified_topic="StructContentTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    return validate_transfer(transfer, symbol_table=symbol_table)


def test_nested_structure_own_attribute_validated(struct_content_builder):
    """`HomeAddress.Street` (MANDATORY, present) must be recognized and
    validated - NOT "type not verified" (the previous behavior for any
    STRUCTURE without a REF)."""
    wrapper = _struct_wrapper(_text_attr("Street", "Bahnhofstrasse 1"))
    obj = _obj(PERSON_CLASS, "p1", _text_attr("Name", "Alice"), _geom_attr("HomeAddress", wrapper))
    issues = _validate_struct_person(obj, struct_content_builder.symbol_table)
    assert issues == []


def test_nested_structure_missing_mandatory_sub_attribute_flagged(struct_content_builder):
    """`HomeAddress` present but WITHOUT its `Street` MANDATORY - must be
    flagged at the nested path `HomeAddress.Street`, NOT silently
    ignored (no check existed on this content before this fix)."""
    wrapper = _struct_wrapper()
    obj = _obj(PERSON_CLASS, "p1", _text_attr("Name", "Alice"), _geom_attr("HomeAddress", wrapper))
    issues = _validate_struct_person(obj, struct_content_builder.symbol_table)
    msgs = _messages(issues, attribute="HomeAddress.Street", severity="error")
    assert any("MANDATORY" in m for m in msgs)


def test_nested_structure_unknown_sub_attribute_flagged(struct_content_builder):
    wrapper = _struct_wrapper(_text_attr("Street", "Bahnhofstrasse 1"), _text_attr("Ghost", "x"))
    obj = _obj(PERSON_CLASS, "p1", _text_attr("Name", "Alice"), _geom_attr("HomeAddress", wrapper))
    issues = _validate_struct_person(obj, struct_content_builder.symbol_table)
    msgs = _messages(issues, attribute="HomeAddress.Ghost", severity="warning")
    assert any("absent du schema" in m for m in msgs)


def test_multivalue_root_attribute_each_occurrence_validated(struct_content_builder):
    """`Tags` (BAG {0..*} OF Note, attribut RACINE) - CHAQUE occurrence est
    validee independamment, avec un chemin indexe `Tags[i]` (confirme reel,
    RULE #1 : `KGS_PBC_V2_2.ili` `Objektart`/`EGID`/`Adressen`)."""
    valid = _struct_wrapper(_text_attr("Text", "hello"))
    invalid = _struct_wrapper()  # Text MANDATORY absent
    obj = _obj(PERSON_CLASS, "p1", _text_attr("Name", "Alice"), _multi_attr("Tags", valid, invalid))
    issues = _validate_struct_person(obj, struct_content_builder.symbol_table)
    assert _messages(issues, attribute="Tags[0].Text") == []
    msgs = _messages(issues, attribute="Tags[1].Text", severity="error")
    assert any("MANDATORY" in m for m in msgs)


def test_multivalue_nested_inside_structure_validated(struct_content_builder):
    """`HomeAddress.Notes` (BAG {0..*} OF Note, attribut d'une STRUCTURE
    imbriquee, PAS racine) - meme mecanisme, chemin `HomeAddress.Notes[i].Text`
    (confirme reel : `LocalisationCH_V1.MultilingualText.LocalisedText`)."""
    note_ok = _struct_wrapper(_text_attr("Text", "hello"))
    note_bad = _struct_wrapper()
    home = _struct_wrapper(_text_attr("Street", "Bahnhofstrasse 1"), _multi_attr("Notes", note_ok, note_bad))
    obj = _obj(PERSON_CLASS, "p1", _text_attr("Name", "Alice"), _geom_attr("HomeAddress", home))
    issues = _validate_struct_person(obj, struct_content_builder.symbol_table)
    assert _messages(issues, attribute="HomeAddress.Notes[0].Text") == []
    msgs = _messages(issues, attribute="HomeAddress.Notes[1].Text", severity="error")
    assert any("MANDATORY" in m for m in msgs)
