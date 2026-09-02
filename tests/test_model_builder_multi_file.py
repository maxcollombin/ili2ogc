"""Multi-file resolution (IMPORTS): `importer.ili` imports `base.ili` and
references one of its DOMAIN by qualified name (`Base.PersonKind`) -
without a ModelRepository this reference stays an
UnresolvedNamedReference (V1 behavior); with a ModelRepository pointing at
the fixtures directory, it must resolve to the actual EnumType instance
built from base.ili."""

from pathlib import Path

import pytest
from conftest import build_from_file, build_from_file_with_model

from interlis.builder.errors import UnresolvedNamedReference
from interlis.builder.repository import ModelRepository

FIXTURES_DIR = Path(__file__).parent / "fixtures/multi_file"


def _build(repository):
    # known spec gaps, out of scope for this test (warnings suppressed by build_from_file_with_model)
    return build_from_file_with_model(FIXTURES_DIR / "importer.ili", repository=repository)


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
    """`ModelRepository.availability()`, proactively verifying header-vs-resolved
    completeness for `interlis validate`: all 4 possible states, each
    exercised concretely (not just "one of the 4 works", RULE #1)."""
    _build(repository)  # binds bind_builder_factory (needed by _get_table)
    assert repository.availability("INTERLIS") == "builtin"
    assert repository.availability("Base") == "available"
    assert repository.availability("NoSuchModel") == "missing"
    assert repository.availability("BrokenSyntax") == "indexed_but_failed"


# --- `TOPIC ... EXTENDS ...` (DataUnit.Super, including cross-model via
# IMPORTS) - tests/fixtures/topic_extends/{base,importer}.ili.
# ExtendsBase.MainTopic (CLASS Item) + ExtendsBase.DependsOnly (DEPENDS ON
# MainTopic, no EXTENDS - regression guard for the separate `index: 0` bug,
# which confused the 1st DEPENDS ON target with an EXTENDS target).
# ExtendsImporter.MainTopic EXTENDS ExtendsBase.MainTopic (empty body, same
# shape as the real SectoralPlanForRoadInfrastructure_V1_4.ili). ---

TOPIC_EXTENDS_FIXTURES_DIR = Path(__file__).parent / "fixtures/topic_extends"


def _build_topic_extends(entry_file: str):
    repository = ModelRepository([TOPIC_EXTENDS_FIXTURES_DIR])
    return build_from_file_with_model(TOPIC_EXTENDS_FIXTURES_DIR / entry_file, repository=repository)


def test_topic_extends_super_resolves_to_base_dataunit_cross_model():
    """`TOPIC MainTopic EXTENDS ExtendsBase.MainTopic` (IMPORTED model) must
    attach `Super` to the TWIN DataUnit of the resolved base SubModel - not
    to the SubModel itself (type-incompatible with Inheritance.Super, which
    targets ExtendableME), and must not stay `None` (bug: the binding key,
    missing `association`/`role`, always made `attach()` fail, silently
    absorbed by `source.optional: true`)."""
    _builder, model = _build_topic_extends("importer.ili")
    topic = model.Element[0]
    du = topic._twin
    assert du.Super is not None
    assert du.Super._qualified_class == "IlisMeta16.ModelData.DataUnit"
    assert du.Super.Name == "BASKET"


def test_topic_without_extends_but_with_depends_on_has_no_super():
    """Regression guard for a SEPARATE bug found while fixing the one
    above: `ExtendsBase.DependsOnly` has NO `EXTENDS` at all, only a
    `DEPENDS ON MainTopic` - a bare `topicRef(0)` would wrongly capture
    that DEPENDS ON target as if it were an EXTENDS target.
    `anchor: EXTENDS` must leave `Super` at `None` here."""
    _builder, model = _build_topic_extends("base.ili")
    depends_only = next(t for t in model.Element if getattr(t, "Name", None) == "DependsOnly")
    assert getattr(depends_only._twin, "Super", None) is None


# --- SymbolTable.resolve() on a short-name collision between a LOCAL
# symbol and an IMPORTED one (tests/fixtures/cross_model_short_name_
# collision/{base,importer}.ili) - CollisionBase.ModInfo (LatestModification:
# MANDATORY TEXT) + CollisionImporter.MainTopic.ModInfo EXTENDS
# CollisionBase.ModInfo (empty, no attribute of its own): the EXACT same
# shape as the real case found in BaseModel_SectoralPlans_V1_4.ili/
# CHBase_Part5_MODIFICATIONINFO_V1.ili (WithLatestModification_V1.ModInfo),
# where "ModInfo" exists both LOCALLY and in the imported model. ---

COLLISION_FIXTURES_DIR = Path(__file__).parent / "fixtures/cross_model_short_name_collision"


def test_extends_cross_model_short_name_collision_resolves_to_imported_class():
    """`STRUCTURE ModInfo EXTENDS CollisionBase.ModInfo` (qualified name, NOT
    the bare short name "ModInfo") must resolve to the IMPORTED ModInfo -
    not to the LOCAL ModInfo itself (self-loop bug: `resolve()` fell back
    to the short-name match BEFORE the cross-file fallback got a chance,
    as soon as an exact qualified match was missing - even when the
    qualified prefix ("CollisionBase") didn't name this file at all)."""
    repository = ModelRepository([COLLISION_FIXTURES_DIR])
    builder = build_from_file(COLLISION_FIXTURES_DIR / "importer.ili", repository=repository)

    from interlis.xtf.schema import attributes_of, resolve_class

    cls = resolve_class(
        "CollisionImporter.MainTopic.ModInfo",
        symbol_table=builder.symbol_table,
        repository=repository,
    )
    sup = getattr(cls, "Super", None)
    assert sup is not None
    assert sup is not cls  # RULE #6 - the exact self-loop from the bug
    assert sup.Name == "ModInfo"
    assert list(attributes_of(cls)) == ["LatestModification"]


# --- `IMPORTS UNQUALIFIED` + short-name collision between a base model
# and its extension WITHIN THE SAME FILE (real shape of
# Localisation_V1.MultilingualText / LocalisationCH_V1.MultilingualText,
# imported unqualified by WasserBase_Codelisten_V1_1 and others): the bare
# reference `Multiling` must bind to DerivedLoc.Multiling, NOT stay an
# UnresolvedNamedReference just because the shared file's table also
# carries BaseLoc.Multiling (same short name, same kind `Class`). ---

UNQUALIFIED_COLLISION_DIR = Path(__file__).parent / "fixtures/imports_unqualified_short_name_collision"


def test_imports_unqualified_reference_resolves_to_the_named_model_not_its_same_named_base():
    repository = ModelRepository([UNQUALIFIED_COLLISION_DIR])
    builder = build_from_file(UNQUALIFIED_COLLISION_DIR / "consumer.ili", repository=repository)

    from interlis.xtf.schema import attributes_of, resolve_class

    cls = resolve_class(
        "Consumer.T.Item",
        symbol_table=builder.symbol_table,
        repository=repository,
    )
    label_type = cls.ClassAttribute[0].Type
    assert not isinstance(label_type, UnresolvedNamedReference)
    assert label_type.Name == "Multiling"
    # DerivedLoc.Multiling, not BaseLoc.Multiling: it carries the extension's
    # own `Extra` on top of the inherited `Txt`.
    assert list(attributes_of(label_type)) == ["Txt", "Extra"]
