"""`TRANSLATION OF` positional alignment + the `--lang` output overlay.

An INTERLIS translation model re-declares the base's whole structure with
translated identifiers, matched positionally (the grammar has no `==`
rename syntax). The transfer format is unchanged (refman SS4.3.3), so the
translation only drives a `--lang` rename of `convert` / `convert-jsonfg`
output - the .ili input and the .xtf wire tags stay in the base language.
"""

import json
from pathlib import Path

from conftest import build_from_file

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.cli import main
from interlis.cli_style import ExitCode
from interlis.convert.translation import load_translation
from interlis.metamodel.instance import MetaInstance

FIX = Path(__file__).resolve().parent / "fixtures" / "translation"


def _build(name: str) -> InterlisModelBuilder:
    return build_from_file(FIX / name, repository=ModelRepository([FIX]))


def test_positional_alignment_builds_the_name_map():
    builder = _build("CheminsPedestres_V1.ili")
    model = next(
        i
        for i in builder.symbol_table.all_registered()
        if isinstance(i, MetaInstance)
        and i._qualified_class == "IlisMeta16.ModelData.Model"
        and i.Name == "CheminsPedestres_V1"
    )
    tr = model._translation
    assert tr["language"] == "fr" and tr["of"] == "Wanderwege_V1"
    assert tr["elements"]["Wegabschnitt"] == "TronconChemin"
    assert tr["elements"]["Netz"] == "Reseau"
    assert tr["elements"]["Wegkategorie"] == "CategorieChemin"
    assert tr["elements"]["Wegabschnitt_MitWegweiser"] == "TronconChemin_AvecIndicateur"
    assert tr["attributes"][("Wegabschnitt", "Bezeichnung")] == "Designation"
    assert tr["attributes"][("Wegweiser", "Standort")] == "Emplacement"

    # the formal IlisMeta16.ModelTranslation.Translation (IlisMeta16.ili's TOPIC ModelTranslation)
    obj = model._translation_object
    assert obj._qualified_class == "IlisMeta16.ModelTranslation.Translation"
    assert obj.Language == "fr"
    by_of = {me.Of: me.TranslatedName for me in obj.Translations}
    assert by_of["Wanderwege_V1.Netz.Wegabschnitt"] == "TronconChemin"
    assert by_of["Wanderwege_V1.Netz.Wegabschnitt.Bezeichnung"] == "Designation"


def test_load_translation_finds_it_by_language_in_the_repo():
    repo = _build("Wanderwege_V1.ili").repository  # binds the sub-builder factory
    tr = load_translation("Wanderwege_V1", "fr", repo)
    assert tr is not None and tr.element("Wegabschnitt") == "TronconChemin"
    assert tr.attribute("Wegabschnitt", "Kategorie") == "Categorie"
    assert load_translation("Wanderwege_V1", "it", repo) is None


def test_convert_lang_renames_defs_properties_required_and_view(capsys):
    assert main(["convert", str(FIX / "Wanderwege_V1.ili"), "--lang", "fr", "--repo", str(FIX)]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert {"TronconChemin", "Indicateur", "TronconChemin_AvecIndicateur"} <= set(schema["$defs"])
    troncon = schema["$defs"]["TronconChemin"]
    assert set(troncon["properties"]) == {"Designation", "Categorie", "TypeRevetement"}
    assert troncon["required"] == ["Designation", "Categorie"]


def test_convert_sql_lang_renames_tables_columns_views_and_fk_refs(capsys):
    assert (
        main(
            [
                "convert-sql",
                str(FIX / "Wanderwege_V1.ili"),
                "--dialect",
                "postgresql",
                "--lang",
                "fr",
                "--repo",
                str(FIX),
            ]
        )
        == 0
    )
    ddl = capsys.readouterr().out
    assert 'CREATE TABLE "tronconchemin"' in ddl
    assert '"designation" varchar(40)' in ddl
    assert 'CREATE VIEW "tronconchemin_avecindicateur"' in ddl
    # the FK column and its REFERENCES target renamed consistently
    assert 'FOREIGN KEY ("tronconchemin") REFERENCES "tronconchemin" ("id")' in ddl
    # the view's WHERE EXISTS references the renamed child table + FK column
    assert 'FROM "indicateur" "v1" WHERE "v1"."tronconchemin" = "tronconchemin"."id"' in ddl
    for german in ("wegabschnitt", "wegweiser", "bezeichnung", "belagsart"):
        assert f'"{german}"' not in ddl


def test_convert_jsonfg_lang_renames_featuretype_and_property_keys_but_not_the_wire_values(capsys):
    rc = main(
        [
            "convert-jsonfg",
            str(FIX / "wanderwege.xtf"),
            "--model",
            str(FIX / "Wanderwege_V1.ili"),
            "--lang",
            "fr",
            "--repo",
            str(FIX),
        ]
    )
    assert rc == 0
    features = {f["featureType"]: f["properties"] for f in json.loads(capsys.readouterr().out)["features"]}
    assert set(features) == {"TronconChemin", "Indicateur"}
    assert set(features["TronconChemin"]) == {"Designation", "Categorie", "TypeRevetement"}
    # the coded value stays in the base language - `--lang` renames identifiers, not enumeration values
    assert features["TronconChemin"]["Categorie"] == "Wanderweg"


def test_convert_lang_with_no_translation_is_a_clear_error(capsys):
    assert main(["convert", str(FIX / "Wanderwege_V1.ili"), "--lang", "it", "--repo", str(FIX)]) == ExitCode.NOT_FOUND
    assert "no TRANSLATION OF 'Wanderwege_V1' for 'it'" in capsys.readouterr().err
