"""Resolution multi-fichiers (IMPORTS) : voir le plan de conception
/home/maxime/.claude/plans/rippling-kindling-swing.md. `importer.ili`
importe `base.ili` et reference une de ses DOMAIN par nom qualifie
(`Base.PersonKind`) - sans ModelRepository, cette reference resterait un
UnresolvedNamedReference (comportement V1) ; avec un ModelRepository
pointant sur le repertoire des fixtures, elle doit resoudre vers
l'instance EnumType reelle construite depuis base.ili."""
import warnings
from pathlib import Path

import pytest

from interlis.builder.errors import UnresolvedNamedReference
from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.runtime.parse import parse_file

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURES_DIR = Path(__file__).parent / "fixtures/multi_file"


def _build(repository):
    tree, errors = parse_file(FIXTURES_DIR / "importer.ili")
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # gaps de spec connus, hors de portee de ce test
        model = builder.build(tree)
    return builder, model


def _employee_kind_type(model):
    topic = model.Element[0]
    employee = topic.Element[0]
    kind_attr = employee.ClassAttribute[0]
    assert kind_attr.Name == "Kind"
    return kind_attr.Type


def test_without_repository_stays_unresolved():
    _, model = _build(repository=None)
    type_value = _employee_kind_type(model)
    assert isinstance(type_value, UnresolvedNamedReference)
    assert type_value.name == "Base.PersonKind"


def test_with_repository_resolves_cross_file_reference():
    repository = ModelRepository([FIXTURES_DIR])
    _, model = _build(repository=repository)
    type_value = _employee_kind_type(model)
    assert not isinstance(type_value, UnresolvedNamedReference)
    assert type_value._qualified_class == "IlisMeta16.ModelData.EnumType"
    assert type_value.Name == "PersonKind"


def test_import_instance_resolves_to_real_model():
    repository = ModelRepository([FIXTURES_DIR])
    _, model = _build(repository=repository)
    imports = model.model_extra.get("imports")
    assert imports and len(imports) == 1
    imported_model = imports[0].ImportedP
    assert not isinstance(imported_model, UnresolvedNamedReference)
    assert imported_model.Name == "Base"


@pytest.fixture
def repository():
    return ModelRepository([FIXTURES_DIR])


def test_repository_indexes_declared_model_names(repository):
    assert repository._index.get("Base") == FIXTURES_DIR / "base.ili"
    assert repository._index.get("Importer") == FIXTURES_DIR / "importer.ili"


def test_availability_builtin_available_missing_and_failed(repository):
    """Lot 39 - `ModelRepository.availability()`, verification proactive de
    completude header-vs-resolu pour `interlis validate` : les 4 etats
    possibles, chacun exerce concretement (pas seulement "un des 4 marche",
    RULE #1)."""
    _build(repository)  # lie bind_builder_factory (necessaire a _get_table)
    assert repository.availability("INTERLIS") == "builtin"
    assert repository.availability("Base") == "available"
    assert repository.availability("NoSuchModel") == "missing"
    assert repository.availability("BrokenSyntax") == "indexed_but_failed"


# --- Lot 46 point 2 : `TOPIC ... EXTENDS ...` (DataUnit.Super, y compris
# cross-modele via IMPORTS) - tests/fixtures/topic_extends/{base,importer}.ili.
# ExtendsBase.MainTopic (CLASS Item) + ExtendsBase.DependsOnly (DEPENDS ON
# MainTopic, PAS d'EXTENDS - regression-guard sur le bug `index: 0` separe,
# qui confondait la 1ere cible DEPENDS ON avec une cible EXTENDS avant ce
# lot). ExtendsImporter.MainTopic EXTENDS ExtendsBase.MainTopic (corps vide,
# meme forme que SectoralPlanForRoadInfrastructure_V1_4.ili reel). ---

TOPIC_EXTENDS_FIXTURES_DIR = Path(__file__).parent / "fixtures/topic_extends"


def _build_topic_extends(entry_file: str):
    tree, errors = parse_file(TOPIC_EXTENDS_FIXTURES_DIR / entry_file)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    repository = ModelRepository([TOPIC_EXTENDS_FIXTURES_DIR])
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = builder.build(tree)
    return builder, model


def test_topic_extends_super_resolves_to_base_dataunit_cross_model():
    """`TOPIC MainTopic EXTENDS ExtendsBase.MainTopic` (modele IMPORTE) doit
    attacher `Super` a la DataUnit JUMELLE du SubModel de base resolu - pas
    au SubModel lui-meme (type incompatible avec Inheritance.Super, qui
    cible ExtendableME), et pas rester `None` (bug avant ce lot : la cle de
    binding, sans `association`/`role`, faisait toujours echouer
    `attach()`, silencieusement absorbe par `source.optional: true`)."""
    _builder, model = _build_topic_extends("importer.ili")
    topic = model.Element[0]
    du = topic._twin
    assert du.Super is not None
    assert du.Super._qualified_class == "IlisMeta16.ModelData.DataUnit"
    assert du.Super.Name == "BASKET"


def test_topic_without_extends_but_with_depends_on_has_no_super():
    """Regression-guard sur le bug SEPARE trouve en corrigeant celui
    ci-dessus : `ExtendsBase.DependsOnly` n'a AUCUN `EXTENDS`, seulement un
    `DEPENDS ON MainTopic` - `topicRef(0)` (avant ce lot) aurait capture a
    tort cette cible DEPENDS ON comme si c'etait une cible EXTENDS.
    `anchor: EXTENDS` doit laisser `Super` a `None` ici."""
    _builder, model = _build_topic_extends("base.ili")
    depends_only = next(t for t in model.Element if getattr(t, "Name", None) == "DependsOnly")
    assert getattr(depends_only._twin, "Super", None) is None


# --- Lot 49 : SymbolTable.resolve() sur une collision de nom court entre un
# symbole LOCAL et un symbole IMPORTE (tests/fixtures/cross_model_short_name_
# collision/{base,importer}.ili) - CollisionBase.ModInfo (LatestModification:
# MANDATORY TEXT) + CollisionImporter.MainTopic.ModInfo EXTENDS
# CollisionBase.ModInfo (vide, aucun attribut propre) : meme forme EXACTE que
# le cas reel trouve sur BaseModel_SectoralPlans_V1_4.ili/
# CHBase_Part5_MODIFICATIONINFO_V1.ili (WithLatestModification_V1.ModInfo),
# "ModInfo" existant LOCALEMENT ET dans le modele importe. ---

COLLISION_FIXTURES_DIR = Path(__file__).parent / "fixtures/cross_model_short_name_collision"


def test_extends_cross_model_short_name_collision_resolves_to_imported_class():
    """`STRUCTURE ModInfo EXTENDS CollisionBase.ModInfo` (nom qualifie, PAS
    le nom court "ModInfo" seul) doit resoudre vers le ModInfo IMPORTE - pas
    vers le ModInfo LOCAL lui-meme (self-loop, bug avant ce lot : `resolve()`
    retombait sur le repli par nom court AVANT que le repli cross-fichier
    n'ait sa chance, des qu'une correspondance qualifiee exacte manquait -
    meme si le prefixe qualifie ("CollisionBase") ne designait PAS du tout
    ce fichier)."""
    tree, errors = parse_file(COLLISION_FIXTURES_DIR / "importer.ili")
    assert not errors, errors
    repository = ModelRepository([COLLISION_FIXTURES_DIR])
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)

    from interlis.xtf.schema import attributes_of, resolve_class

    cls = resolve_class(
        "CollisionImporter.MainTopic.ModInfo", symbol_table=builder.symbol_table, repository=repository,
    )
    sup = getattr(cls, "Super", None)
    assert sup is not None
    assert sup is not cls  # RULE #6 - le self-loop exact du bug
    assert sup.Name == "ModInfo"
    assert list(attributes_of(cls)) == ["LatestModification"]


# --- `IMPORTS UNQUALIFIED` + short-name collision entre un modele de base
# et son extension DANS LE MEME FICHIER (forme reelle de
# Localisation_V1.MultilingualText / LocalisationCH_V1.MultilingualText,
# importe non qualifie par WasserBase_Codelisten_V1_1 et consorts) : la
# reference nue `Multiling` doit se lier a DerivedLoc.Multiling, PAS rester
# UnresolvedNamedReference parce que la table du fichier partage porte
# aussi BaseLoc.Multiling (meme nom court, meme kind `Class`). ---

UNQUALIFIED_COLLISION_DIR = Path(__file__).parent / "fixtures/imports_unqualified_short_name_collision"


def test_imports_unqualified_reference_resolves_to_the_named_model_not_its_same_named_base():
    tree, errors = parse_file(UNQUALIFIED_COLLISION_DIR / "consumer.ili")
    assert not errors, errors
    repository = ModelRepository([UNQUALIFIED_COLLISION_DIR])
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)

    from interlis.xtf.schema import attributes_of, resolve_class

    cls = resolve_class(
        "Consumer.T.Item", symbol_table=builder.symbol_table, repository=repository,
    )
    label_type = cls.ClassAttribute[0].Type
    assert not isinstance(label_type, UnresolvedNamedReference)
    assert label_type.Name == "Multiling"
    # DerivedLoc.Multiling, not BaseLoc.Multiling: it carries the extension's
    # own `Extra` on top of the inherited `Txt`.
    assert list(attributes_of(label_type)) == ["Txt", "Extra"]
