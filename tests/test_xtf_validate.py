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
from interlis.builder.repository import ModelRepository
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


def test_numeric_within_rounding_tolerance_has_no_issue(builder):
    """Lot 48 - RULE #4, eCH-0031 V2.1.0 §2.8 "Umgang mit Rundung von
    numerischen Werten und Koordinaten" / §4.3.11.4 "Codierung von
    numerischen Datentypen" : une valeur peut etre transferee avec une
    precision SUPERIEURE a celle du domaine (BirthYear: 1800..2100, 0
    decimale) - seul compte qu'elle arrondit dans la plage. 1799.6
    arrondit a 1800, ne doit PAS etre signalee < Min."""
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


# --- Lot 40 : compatibilite de classe d'une reference RESOLUE (RefLocation
# declare REFERENCE TO Location - fixture etendue avec SpecialLocation
# EXTENDS Location, tests/fixtures/xtf/reference_model.ili) ---


def test_reference_resolved_to_declared_class_has_no_issue(ref_builder):
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_to_subclass_is_compatible(ref_builder):
    """Polymorphisme INTERLIS standard : une reference declaree vers
    Location doit accepter une cible reelle de type SpecialLocation
    (EXTENDS Location) sans le signaler comme incompatible."""
    special = XtfObject(
        tid="loc-1", qualified_class="RefTest.MainTopic.SpecialLocation",
        attributes=dict([_text_attr("Name", "Bern"), _text_attr("Detail", "capital")]),
    )
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "loc-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[special, indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="RefLocation") == []


def test_reference_resolved_to_incompatible_class_is_error(ref_builder):
    """RefLocation resout vers un objet REELLEMENT present dans le
    transfert (donc information COMPLETE, pas ambigu comme le cas "REF
    introuvable") mais de classe Indicator, sans rapport avec Location
    (ni identique, ni sous-classe) - doit devenir une `error`."""
    other_indicator = XtfObject(
        tid="ind-2", qualified_class=INDICATOR_CLASS, attributes=dict([_text_attr("Value", "1")]),
    )
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "ind-2")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[other_indicator, indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="error")
    assert any("incompatible" in m for m in msgs)


# --- Lot 32 : roles d'association embarques comme pseudo-attributs
# (tests/fixtures/xtf/reference_model.ili : ASSOCIATION Location_Indicator
# = rLocation -<#> Location; rIndicator -- {0..*} Indicator; - meme forme
# que ASSOCIATION MeasurementLocation_Indicator sur le corpus reel
# RoadTrafficCensus_V1_1) ---

def test_embedded_role_resolved_is_not_unknown_attribute(ref_builder):
    """rLocation s'embarque sur Indicator (role rIndicator, cote {0..*}) -
    doit etre reconnu comme un attribut de schema valide, PAS "inconnu"."""
    location = XtfObject(tid="loc-1", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Bern")]))
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "loc-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[location, indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="rLocation") == []


def test_embedded_role_not_exposed_on_opposite_class(ref_builder):
    """rLocation ne doit PAS apparaitre comme pseudo-attribut de Location
    elle-meme (il s'embarque uniquement cote Indicator, la classe dont le
    role oppose - rIndicator - a une cardinalite {0..*}) - meme si Location
    porte par ailleurs un AUTRE role reellement embarque sur elle-meme
    (rNote, ASSOCIATION Location_Note, voir tests Lot 47 ci-dessous)."""
    from interlis.xtf.schema import embedded_roles_of, resolve_class

    location_cls = resolve_class(LOCATION_CLASS, symbol_table=ref_builder.symbol_table, repository=None)
    assert "rLocation" not in embedded_roles_of(location_cls, ref_builder.symbol_table)


def test_embedded_role_unresolved_ref_is_warning(ref_builder):
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rLocation", severity="warning")
    assert any("introuvable dans ce transfert" in m for m in msgs)


# --- Lot 47 : embedded_roles_of doit suivre la chaine EXTENDS (tests/fixtures/
# xtf/reference_model.ili : ASSOCIATION Location_Note embarque rNote sur
# Location - CLASS SpecialLocation EXTENDS Location (deja utilisee par les
# tests Lot 40 ci-dessus) doit donc HERITER ce pseudo-attribut, confirme reel
# (RULE #4) responsable de 93-95% des avertissements sur
# IVS_V2_1_national/regional_lokal_LV95.xtf avant ce lot - CLASS ivs_punkt-
# objekte_base (ABSTRACT) porte le role embarque, les objets XTF reels sont
# tous de la sous-classe concrete ivs_punktobjekte_lv95/_lv03.) ---

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
    note = XtfObject(tid="note-1", qualified_class="RefTest.MainTopic.Note", attributes=dict([_text_attr("Text", "hello")]))
    special = XtfObject(
        tid="special-1", qualified_class=SPECIAL_LOCATION_CLASS,
        attributes=dict([_text_attr("Name", "Bern"), _ref_attr("rNote", "note-1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[note, special])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues, attribute="rNote") == []


# --- Lot 43 (suite Lot 45) : statut EXTERNAL d'un role d'association
# embarque lui-meme (tests/fixtures/xtf/reference_model.ili : ASSOCIATION
# Location_ExternalIndicator, rExtLocation (EXTERNAL) -<#> Location) ---

def test_embedded_role_external_unresolved_ref_reports_catalogue_expected(ref_builder):
    """rExtLocation porte sa PROPRE clause (EXTERNAL) sur le role - un REF
    non resolu doit etre signale comme la situation NORMALE attendue
    (meme message qu'une REFERENCE TO (EXTERNAL) ordinaire, Lot 35), pas
    comme un signal de donnee incorrecte."""
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rExtLocation", "ext.catalog.1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rExtLocation", severity="warning")
    assert any("declaree (EXTERNAL)" in m and "situation normale" in m for m in msgs)


def test_embedded_role_non_external_unresolved_ref_flags_data_issue(ref_builder):
    """rLocation (meme association Location_Indicator qu'avant) n'a PAS de
    clause EXTERNAL - un REF non resolu doit continuer a signaler que la
    cible DEVRAIT normalement etre dans ce meme panier, PAS regresser vers
    le message neutre "indetermine" maintenant que le statut des roles est
    resolu."""
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("rLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="rLocation", severity="warning")
    assert any("NON declaree (EXTERNAL)" in m for m in msgs)


# --- Lot 35 : catalogue objects (REFERENCE TO (EXTERNAL), --catalog) ---

def test_non_external_unresolved_ref_flags_data_issue(ref_builder):
    """RefLocation (pas de clause EXTERNAL) : un REF non resolu doit rester
    `warning` (RULE #5, jamais `error` sans catalogue charge) mais le
    message doit signaler que la cible DEVRAIT normalement etre dans ce
    meme panier (eCH-0031 V2.1.0 3.6.3)."""
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefLocation", "does-not-exist")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefLocation", severity="warning")
    assert any("NON declaree (EXTERNAL)" in m for m in msgs)


def test_external_unresolved_ref_reports_catalogue_expected(ref_builder):
    """RefCatalogItem : REFERENCE TO (EXTERNAL) - un REF non resolu doit
    rester `warning` mais le message doit signaler que c'est la situation
    NORMALE attendue pour une reference-catalogue (pas un signal de donnee
    incorrecte)."""
    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefCatalogItem", "ext.catalog.99")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    msgs = _messages(issues, attribute="RefCatalogItem", severity="warning")
    assert any("declaree (EXTERNAL)" in m and "situation normale" in m for m in msgs)


def test_external_ref_resolved_via_catalog_argument_has_no_issue(ref_builder):
    """Le TID d'un objet-catalogue EXTERNAL vit typiquement dans un fichier
    .xtf SEPARE du transfert principal (Lot 35, `--catalog`) - passer ce
    transfert-catalogue via `catalogs=` doit le rendre resoluble, exactement
    comme un objet du transfert principal."""
    catalog_item = XtfObject(tid="ext.catalog.99", qualified_class=LOCATION_CLASS, attributes=dict([_text_attr("Name", "Catalogue")]))
    catalog_basket = XtfBasket(bid="cat", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[catalog_item])
    catalog_transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[catalog_basket])

    indicator = XtfObject(
        tid="ind-1", qualified_class=INDICATOR_CLASS,
        attributes=dict([_text_attr("Value", "42"), _ref_attr("RefCatalogItem", "ext.catalog.99")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="RefTest.MainTopic", kind=None, endstate=None, objects=[indicator])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])

    issues_without_catalog = validate_transfer(transfer, symbol_table=ref_builder.symbol_table)
    assert _messages(issues_without_catalog, attribute="RefCatalogItem") != []

    issues_with_catalog = validate_transfer(
        transfer, symbol_table=ref_builder.symbol_table, catalogs=[catalog_transfer],
    )
    assert _messages(issues_with_catalog, attribute="RefCatalogItem") == []


# --- Lot 41 : 3e forme d'encodage XTF (CLASS RESTRICTION(A; B; ...) sur des
# STRUCTUREs a 1 attribut, valeur texte nue - tests/fixtures/xtf/restriction_model.ili :
# `Selector = CLASS RESTRICTION(sColor; sSize)` (2 candidats pleinement
# verifiables, chacun un enum inline) et `SelectorWithExternal = CLASS
# RESTRICTION(sColor; sExternal)` (1 candidat verifiable + 1 dont le type
# interne est une reference, jamais verifiable par ce mecanisme) ---

RESTRICTION_FIXTURE = Path(__file__).parent / "fixtures/xtf/restriction_model.ili"
WIDGET_CLASS = "RestrictionTest.MainTopic.Widget"
WIDGET_EXTERNAL_CLASS = "RestrictionTest.MainTopic.WidgetWithExternal"


@pytest.fixture(scope="module")
def restriction_builder():
    tree, errors = parse_file(RESTRICTION_FIXTURE)
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


def test_restriction_text_matching_first_candidate_has_no_issue(restriction_builder):
    """"Red" appartient au domaine inline de sColor - le 1er candidat de
    `RESTRICTION(sColor; sSize)`."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Red")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    assert _messages(issues, attribute="Sel") == []


def test_restriction_text_matching_second_candidate_has_no_issue(restriction_builder):
    """"Small" appartient au domaine inline de sSize - le 2e candidat,
    PAS le 1er (regression Lot 41 : la segmentation SEMI-naive tronquait
    silencieusement `_build_domain_class_restriction` a son 1er candidat
    seulement, avant le fix de profondeur LPAR/RPAR)."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Small")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    assert _messages(issues, attribute="Sel") == []


def test_restriction_text_matching_no_candidate_is_warning(restriction_builder):
    """"Purple" n'appartient a AUCUN des 2 domaines inline (sColor:
    Red/Blue, sSize: Small/Large) - les 2 candidats sont PLEINEMENT
    verifiables (enums inline, rien d'externe) -> `warning`, pas `info`."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_CLASS, attributes=dict([_text_attr("Sel", "Purple")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    msgs = _messages(issues, attribute="Sel", severity="warning")
    assert any("CLASS RESTRICTION" in m and "ne correspond a aucun des 2 candidat" in m for m in msgs)
    assert _messages(issues, attribute="Sel", severity="error") == []


def test_restriction_text_with_unverifiable_candidate_is_info_not_warning(restriction_builder):
    """`SelectorWithExternal = CLASS RESTRICTION(sColor; sExternal)` : une
    valeur qui ne correspond pas au candidat verifiable (sColor) NE DOIT
    PAS devenir `warning` si l'AUTRE candidat (sExternal, dont l'attribut
    interne est une REFERENCE, jamais verifiable par ce mecanisme) reste
    non tranche - RULE #5, rester `info` (statut reellement indetermine)
    plutot que d'affirmer a tort une non-conformite."""
    obj = XtfObject(tid="w1", qualified_class=WIDGET_EXTERNAL_CLASS, attributes=dict([_text_attr("Sel", "not-a-color")]))
    basket = XtfBasket(bid="b1", qualified_topic="RestrictionTest.MainTopic", kind=None, endstate=None, objects=[obj])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=restriction_builder.symbol_table)
    msgs = _messages(issues, attribute="Sel", severity="info")
    assert any("1/2 candidat" in m for m in msgs)
    assert _messages(issues, attribute="Sel", severity="warning") == []


# --- Lot 42 : geometrie/coordonnees (tests/fixtures/xtf/geometry_model.ili :
# Coord2 = COORD 0..100, 0..200 ; MultiCoord2 = MULTICOORD (meme plage) ;
# Line = POLYLINE VERTEX Coord2 ; MultiLine = MULTIPOLYLINE VERTEX Coord2 ;
# Area = SURFACE VERTEX Coord2 - Point.Pos/MultiPoint.Pos/Way.Geom/
# MultiWay.Geom/Zone.Geom, un attribut de chaque forme) ---

GEOMETRY_FIXTURE = Path(__file__).parent / "fixtures/xtf/geometry_model.ili"
POINT_CLASS = "GeomTest.MainTopic.Point"
MULTIPOINT_CLASS = "GeomTest.MainTopic.MultiPoint"
WAY_CLASS = "GeomTest.MainTopic.Way"
DIRECTEDWAY_CLASS = "GeomTest.MainTopic.DirectedWay"
MULTIWAY_CLASS = "GeomTest.MainTopic.MultiWay"
ZONE_CLASS = "GeomTest.MainTopic.Zone"


@pytest.fixture(scope="module")
def geometry_builder():
    tree, errors = parse_file(GEOMETRY_FIXTURE)
    assert not errors
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b


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
    """Lot 48 - RULE #4, eCH-0031 V2.1.0 §2.8/§4.3.11.4 (meme raisonnement
    que test_numeric_within_rounding_tolerance_has_no_issue) : Coord2 (3
    decimales, `0.000 .. 100.000`) - une valeur transferee avec une
    precision superieure qui arrondit encore dans la plage (100.0004 ->
    100.000, exactement Max) ne doit PAS etre signalee."""
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
    assert any("non numerique" in m for m in msgs)


def test_coord_component_count_mismatch_flagged(geometry_builder):
    """CoordType.Axis declare 2 axes - un seul C1 present doit etre signale
    (regression directe du fix Lot 42 sur CoordType.Axis/wrap - sans lui,
    `axes` restait toujours vide et ce desaccord n'aurait jamais pu etre
    detecte)."""
    obj = _obj(POINT_CLASS, "p1", _geom_attr("Pos", _coord_node("50.0")))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Pos", severity="error")
    assert any("composante(s) C" in m for m in msgs)


def test_multicoord_valid_has_no_issue(geometry_builder):
    inner = RawNode(tag="MULTICOORD", text=None, attrib={}, children=[
        _coord_node("10.0", "20.0"), _coord_node("30.0", "40.0"),
    ])
    obj = _obj(MULTIPOINT_CLASS, "mp1", _geom_attr("Pos", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Pos") == []


def test_polyline_valid_has_no_issue(geometry_builder):
    """Exerce aussi le fix Lot 42 LineType.CoordType (VERTEX Coord2, jamais
    attache avant ce lot) - sans lui, `axes` serait vide et le Max de C1
    (100.0, hors plage volontairement testee ci-dessous par contraste)
    resterait invisible."""
    inner = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("50.0", "50.0"),
    ])
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_polyline_out_of_range_vertex_flagged(geometry_builder):
    """Preuve directe que le fix LineType.CoordType (Lot 42) alimente
    reellement la verification de plage sur un segment de POLYLINE, pas
    seulement la structure."""
    inner = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("999.0", "50.0"),
    ])
    obj = _obj(WAY_CLASS, "w1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    msgs = _messages(issues, attribute="Geom", severity="error")
    assert any("Max" in m for m in msgs)


def test_polyline_inherited_coord_type_via_extends_has_no_issue(geometry_builder):
    """Lot 44 : DirectedLine EXTENDS Line = DIRECTED POLYLINE; (aucune
    clause VERTEX propre, meme forme que CHBase reel) - la plage d'axe doit
    etre retrouvee en remontant Super jusqu'a Line, pas seulement absente."""
    inner = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("50.0", "50.0"),
    ])
    obj = _obj(DIRECTEDWAY_CLASS, "dw1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_polyline_inherited_coord_type_out_of_range_flagged(geometry_builder):
    """Preuve directe (pas seulement l'absence de faux positif) que la
    plage HERITEE de Line est reellement appliquee a DirectedLine, pas
    seulement que la structure passe faute de plage connue."""
    inner = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("999.0", "50.0"),
    ])
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
    assert any("POLYLINE attendue" in m for m in msgs)


def test_surface_valid_has_no_issue(geometry_builder):
    polyline = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("50.0", "0.0"), _coord_node("50.0", "50.0"), _coord_node("0.0", "0.0"),
    ])
    boundary = RawNode(tag="BOUNDARY", text=None, attrib={}, children=[polyline])
    inner = RawNode(tag="SURFACE", text=None, attrib={}, children=[boundary])
    obj = _obj(ZONE_CLASS, "z1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


def test_multipolyline_valid_has_no_issue(geometry_builder):
    """MultiWay.Geom (Lot 42 - correctif Multi, `presence: true` sur un
    field compose etait auparavant TOUJOURS ignore par le moteur - Multi
    contenait le TEXTE LITTERAL du token matche au lieu d'un booleen)."""
    polyline_a = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("0.0", "0.0"), _coord_node("10.0", "10.0"),
    ])
    polyline_b = RawNode(tag="POLYLINE", text=None, attrib={}, children=[
        _coord_node("20.0", "20.0"), _coord_node("30.0", "30.0"),
    ])
    inner = RawNode(tag="MULTIPOLYLINE", text=None, attrib={}, children=[polyline_a, polyline_b])
    obj = _obj(MULTIWAY_CLASS, "mw1", _geom_attr("Geom", inner))
    issues = _validate_one(obj, geometry_builder.symbol_table)
    assert _messages(issues, attribute="Geom") == []


# --- Lot 46 : role d'association embarque, defini dans un modele IMPORTE
# (tests/fixtures/embedded_roles_cross_model/{base,importer}.ili) - EmbedBase
# declare ASSOCIATION Parent_Child (Children -- {0..*} Child; Parent -<#> {1}
# Parent;), donc embarque le role "Parent" sur Child. EmbedImporter (racine
# de la validation, comme SectoralPlanForRoadInfrastructure_LV95_V1_4 dans le
# corpus reel) IMPORTS EmbedBase mais n'a AUCUNE association propre - seule
# la table de symboles d'EmbedBase (via ModelRepository, PAS celle
# d'EmbedImporter) contient Parent_Child. Confirme reel (RULE #4) sur
# 2021-01-12_SectoralPlanForRoadInfrastructure_LV95.xtf : 770 pseudo-
# attributs (Object/SectoralPlan) faussement "absents du schema" avant ce
# lot, correctement reconnus apres. ---

CROSS_MODEL_FIXTURES_DIR = Path(__file__).parent / "fixtures/embedded_roles_cross_model"
EMBED_PARENT_CLASS = "EmbedBase.MainTopic.Parent"
EMBED_CHILD_CLASS = "EmbedBase.MainTopic.Child"


@pytest.fixture(scope="module")
def cross_model_builder():
    tree, errors = parse_file(CROSS_MODEL_FIXTURES_DIR / "importer.ili")
    assert not errors
    repository = ModelRepository([CROSS_MODEL_FIXTURES_DIR])
    b = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        b.build(tree)
    return b, repository


def test_embedded_role_from_imported_model_is_not_unknown_attribute(cross_model_builder):
    builder, repository = cross_model_builder
    parent = XtfObject(tid="p1", qualified_class=EMBED_PARENT_CLASS, attributes=dict([_text_attr("Name", "P")]))
    child = XtfObject(
        tid="c1", qualified_class=EMBED_CHILD_CLASS,
        attributes=dict([_text_attr("Name", "C"), _ref_attr("Parent", "p1")]),
    )
    basket = XtfBasket(bid="b1", qualified_topic="EmbedBase.MainTopic", kind=None, endstate=None, objects=[parent, child])
    transfer = XtfTransfer(sender=None, ili_version=None, models=[], baskets=[basket])
    issues = validate_transfer(transfer, symbol_table=builder.symbol_table, repository=repository)
    assert _messages(issues, attribute="Parent") == []


def test_embedded_role_from_imported_model_missing_without_home_table_lookup(cross_model_builder):
    """Regression-guard direct sur schema.py : chercher l'association dans
    la table RACINE (EmbedImporter, sans association propre) plutot que
    dans la table qui la declare REELLEMENT (EmbedBase, via
    home_symbol_table) ne trouve pas le role embarque - la faute corrigee
    par ce lot, isolee de toute la couche validate.py."""
    from interlis.xtf.schema import embedded_roles_of, home_symbol_table, resolve_class

    builder, repository = cross_model_builder
    cls = resolve_class(EMBED_CHILD_CLASS, symbol_table=builder.symbol_table, repository=repository)
    assert embedded_roles_of(cls, builder.symbol_table) == {}
    home_table = home_symbol_table(EMBED_CHILD_CLASS, symbol_table=builder.symbol_table, repository=repository)
    assert set(embedded_roles_of(cls, home_table)) == {"Parent"}
