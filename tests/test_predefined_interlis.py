"""Modele "INTERLIS" predefini (Lot 23) : Reference Manual eCH-0031 V2.1.0,
Annexe A - NOOID/ANYOID/I32OID/STANDARDOID/UUIDOID, toujours disponible
qualifie (`INTERLIS.I32OID`) et disponible non qualifie uniquement via
`IMPORTS UNQUALIFIED INTERLIS;` (voir ModelRepository._BUILTIN_SOURCES,
ForwardRefResolver._resolve_one, InterlisModelBuilder._register_unqualified_imports).

Couvre aussi la regression corrigee au passage (has_prefix) : une reference
NON qualifiee introuvable localement, dans un fichier SANS aucun `IMPORTS
UNQUALIFIED`, doit rester une erreur (BuildError) - pas etre masquee."""
import warnings
from pathlib import Path

import pytest

from interlis.builder.errors import BuildError, UnresolvedNamedReference
from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_file

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURES_DIR = Path(__file__).parent / "fixtures/predefined_interlis"


def _build(filename: str, *, repository=None):
    tree, errors = parse_file(FIXTURES_DIR / filename)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # gaps de spec connus, hors de portee de ce test
        model = builder.build(tree)
    return builder, model


def _ident_type(model):
    topic = model.Element[0]
    thing = topic.Element[0]
    ident_attr = thing.ClassAttribute[0]
    assert ident_attr.Name == "Ident"
    return ident_attr.Type


def test_qualified_reference_resolves_without_any_repository():
    # Aucun --repo/repository= fourni : le modele INTERLIS predefini doit
    # quand meme resoudre, qualifie - il ne depend d'aucun repertoire disque.
    _, model = _build("qualified_ref.ili", repository=None)
    type_value = _ident_type(model)
    assert not isinstance(type_value, UnresolvedNamedReference)
    assert type_value._qualified_class == "IlisMeta16.ModelData.NumType"
    assert type_value.Name == "I32OID"
    # Min/Max : corrige au Lot 25 (voir test_numeric_domain_min_max.py) -
    # verifie ici aussi puisque I32OID est un NumType construit via le
    # meme chemin (domainDef -> numeric() nu -> visit_wrapped).
    assert type_value.Min == "0"
    assert type_value.Max == "2147483647"


def test_unqualified_reference_resolves_with_imports_unqualified():
    builder, model = _build("unqualified_ref.ili", repository=None)
    assert builder.symbol_table.unqualified_imports == {"INTERLIS"}
    type_value = _ident_type(model)
    assert not isinstance(type_value, UnresolvedNamedReference)
    assert type_value._qualified_class == "IlisMeta16.ModelData.NumType"
    assert type_value.Name == "I32OID"


def test_import_instance_resolves_to_real_interlis_model():
    _, model = _build("unqualified_ref.ili", repository=None)
    imports = model.model_extra.get("imports")
    assert imports and len(imports) == 1
    imported_model = imports[0].ImportedP
    assert not isinstance(imported_model, UnresolvedNamedReference)
    assert imported_model.Name == "INTERLIS"


def test_qualified_gregorian_year_resolves_to_numtype_with_manual_range():
    """Lot 47 point 3 : `INTERLIS.GregorianYear` (RULE #4, eCH-0031 V2.1.0
    §3.8.7 "Datum und Zeit", `DOMAIN GregorianYear = 1582 .. 2999 [Y]
    {GregorianCalendar};` - unite/annotation volontairement omises, voir
    repository.py) doit resoudre en NumType Min=1582/Max=2999, comme
    I32OID ci-dessus - PAS un type_kind=None jamais verifie (bug confirme
    reel sur RoadTrafficAccidentLocation_V2.ili/RoadTrafficCensus_V1_1.ili
    avant ce lot)."""
    _, model = _build("gregorian_year_ref.ili", repository=None)
    topic = model.Element[0]
    thing = topic.Element[0]
    year_attr = next(a for a in thing.ClassAttribute if a.Name == "Year")
    type_value = year_attr.Type
    assert not isinstance(type_value, UnresolvedNamedReference)
    assert type_value._qualified_class == "IlisMeta16.ModelData.NumType"
    assert type_value.Name == "GregorianYear"
    assert type_value.Min == "1582"
    assert type_value.Max == "2999"


def test_qualified_xml_date_time_resolve_to_formattedtype():
    """`INTERLIS.XMLDate`/`XMLTime`/`XMLDateTime` (eCH-0031 V2.1.0 §3.8.7 "Datum und Zeit") must resolve to a real FormattedType, not type_kind=None.

    Confirmed a real, corpus-wide gap (2026-08-27, distinct from the
    ANYOID/UUIDOID/BOOLEAN grammar-level exclusions documented in
    docs/dev-notes/predefined-interlis-namespace.md): 30 real `ili_corpus/`
    files declare an attribute directly as `INTERLIS.XMLDate`, all of which
    resolved to `type_kind=None` ("-- NOTE: unsupported type None" in
    `convert-sql`, `"x-unsupported": "unknown"` in `convert`) before this
    fix - unlike `GregorianYear`, XMLDate/XMLTime/XMLDateTime were simply
    never added to `_PREDEFINED_INTERLIS_SOURCE`, not a deliberate
    exclusion. Modeled via the grammar's existing `FORMAT INTERLIS.<Name>
    STRING..STRING` form (`formattedType`'s alternative 1, already
    exercised directly by attributes per the manual's own example) rather
    than the manual's literal `FORMAT BASED ON GregorianDate(...)` citation
    - reduced scope, same "faithful enough, no functional loss" precedent
    as `GregorianYear`'s own omitted `[Y]`/`{GregorianCalendar}` (Min/Max
    aren't consumed by any range check today, see `xtf/validate.py`
    - only `Format` drives the SQL/JSON Schema type mapping)."""
    _, model = _build("xml_date_ref.ili", repository=None)
    topic = model.Element[0]
    thing = topic.Element[0]
    expected_format = {"Erstellungsdatum": "XMLDate", "Uhrzeit": "XMLTime", "Zeitstempel": "XMLDateTime"}
    for attr_name, expected in expected_format.items():
        attr = next(a for a in thing.ClassAttribute if a.Name == attr_name)
        type_value = attr.Type
        assert not isinstance(type_value, UnresolvedNamedReference)
        assert type_value._qualified_class == "IlisMeta16.ModelData.FormattedType"
        assert type_value.Format == expected


def test_unqualified_reference_without_imports_unqualified_still_raises():
    # Regression guard : le bug corrige (has_prefix toujours True pour un nom
    # non qualifie) masquait TOUTE reference locale non resolue derriere un
    # UnresolvedNamedReference silencieux plutot qu'une erreur - un nom
    # jamais declare localement, sans aucun IMPORTS UNQUALIFIED dans le
    # fichier, doit rester un vrai BuildError (RULE #5 : ne pas masquer un
    # bug reel derriere une degradation gracieuse trop large).
    tree, errors = parse_file(FIXTURES_DIR / "broken_unqualified_ref.ili")
    assert not errors
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(BuildError):
            builder.build(tree)
