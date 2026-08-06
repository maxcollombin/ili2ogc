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
    # NumType.Min/Max non couverts ici : bug pre-existant, non lie a ce lot,
    # confirme y compris sur models/IlisMeta16.ili (ex. "Code = 0..255;") -
    # a traiter dans un lot dedie.


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
