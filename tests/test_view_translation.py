"""`TRANSLATION OF` positional alignment + the `--lang` output overlay.

An INTERLIS translation model re-declares the base's whole structure with
translated identifiers, matched positionally (the grammar has no `==`
rename syntax). The transfer format is unchanged (refman SS4.3.3), so the
translation only drives a `--lang` rename of `convert` / `convert-jsonfg`
output - the .ili input and the .xtf wire tags stay in the base language.
"""
import json
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.cli import main
from interlis.convert.translation import load_translation
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIX = Path(__file__).resolve().parent / "fixtures" / "translation"


def _build(name: str) -> InterlisModelBuilder:
    tree, errors = parse_file(FIX / name)
    assert not errors, errors
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=ModelRepository([FIX]))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def test_positional_alignment_builds_the_name_map():
    builder = _build("CheminsPedestres_V1.ili")
    model = next(
        i for i in builder.symbol_table.all_registered()
        if isinstance(i, MetaInstance) and i._qualified_class == "IlisMeta16.ModelData.Model"
        and i.Name == "CheminsPedestres_V1"
    )
    tr = model._translation
    assert tr["language"] == "fr" and tr["of"] == "Wanderwege_V1"
    assert tr["elements"]["Wegabschnitt"] == "TronconChemin"
    assert tr["elements"]["Netz"] == "Reseau"
    assert tr["elements"]["Wegkategorie"] == "CategorieChemin"
    assert tr["attributes"][("Wegabschnitt", "Bezeichnung")] == "Designation"
    assert tr["attributes"][("Wegweiser", "Standort")] == "Emplacement"


def test_load_translation_finds_it_by_language_in_the_repo():
    repo = _build("Wanderwege_V1.ili").repository  # binds the sub-builder factory
    tr = load_translation("Wanderwege_V1", "fr", repo)
    assert tr is not None and tr.element("Wegabschnitt") == "TronconChemin"
    assert tr.attribute("Wegabschnitt", "Kategorie") == "Categorie"
    assert load_translation("Wanderwege_V1", "it", repo) is None


def test_convert_lang_renames_defs_properties_and_required(capsys):
    assert main(["convert", str(FIX / "Wanderwege_V1.ili"), "--lang", "fr", "--repo", str(FIX)]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert set(schema["$defs"]) == {"TronconChemin", "Indicateur"}
    troncon = schema["$defs"]["TronconChemin"]
    assert set(troncon["properties"]) == {"Designation", "Categorie", "TypeRevetement"}
    assert troncon["required"] == ["Designation", "Categorie"]


def test_convert_jsonfg_lang_renames_featuretype_and_property_keys_but_not_the_wire_values(capsys):
    rc = main([
        "convert-jsonfg", str(FIX / "wanderwege.xtf"),
        "--model", str(FIX / "Wanderwege_V1.ili"), "--lang", "fr", "--repo", str(FIX),
    ])
    assert rc == 0
    features = {f["featureType"]: f["properties"] for f in json.loads(capsys.readouterr().out)["features"]}
    assert set(features) == {"TronconChemin", "Indicateur"}
    assert set(features["TronconChemin"]) == {"Designation", "Categorie", "TypeRevetement"}
    # the coded value stays in the base language - `--lang` renames identifiers, not enumeration values
    assert features["TronconChemin"]["Categorie"] == "Wanderweg"


def test_convert_lang_with_no_translation_is_a_clear_error(capsys):
    assert main(["convert", str(FIX / "Wanderwege_V1.ili"), "--lang", "it", "--repo", str(FIX)]) == 1
    assert "no TRANSLATION OF 'Wanderwege_V1' for 'it'" in capsys.readouterr().err
