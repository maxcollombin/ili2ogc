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
