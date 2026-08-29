"""Real-corpus regression: FGDM4GS derived VIEW models (`tests/fixtures/fgdm4gs/`, see NOTICE).

Backlog item 8's Lot A2 left `viewAttributes`'s `Name := expression` form
(`bare_redefinition_list`/`modifier_reassignment`) `status: unresolved`
in `InterlisModelBuilder`, citing "no real corpus evidence" for it. These
5 real, author-confirmed VIEW models (HEIG-VD's FGDM4GS project) are that
evidence - ALL FIVE use this form exclusively, none use `ALL OF`.
`InterlisModelBuilder._build_view_bare_attributes`/`_apply_pending_view_bare_attr_types`
now build one `AttrOrParam` per occurrence, with `Type` resolved by
statically walking the assigned expression's attribute path against the
metamodel (own attributes and, for a PROJECTION OF an ASSOCIATION,
association Roles - see `Planungszonen_V2_d_B.ili`'s multi-hop
`TypPZ_Planungszone -> Planungszone -> Geometrie` below).

Only the 3 fixtures resolvable from `tests/fixtures/fgdm4gs/` alone are
covered here (`IVS_V3_d`/`Planungszonen_V2_d_A`/`Planungszonen_V2_d_B` -
their base models `IVS_V3`/`Planungszonen_V2` are vendored alongside
them). `Axis_V1_1_d`/`SectoralPlanForRoadInfrastructure_V1_4_d` need
their base MGDM from the real `ili_corpus/` scratch corpus (gitignored,
not always present) - not exercised here to keep this test hermetic; see
the NOTICE file for how to build them manually against `ili_corpus/`.
"""
import warnings
from pathlib import Path

import pytest

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "fgdm4gs"


def _build(fname: str):
    tree, errors = parse_file(FIXTURES_DIR / fname)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    repo = ModelRepository([FIXTURES_DIR])
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repo)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _views(builder) -> list[MetaInstance]:
    return [
        inst for inst in builder.symbol_table.all_registered()
        if isinstance(inst, MetaInstance) and inst._qualified_class == "IlisMeta16.ModelData.View"
    ]


@pytest.mark.parametrize(
    "fname,view_name,base_count,kind",
    [
        ("IVS_V3_d.ili", "ivs_nat", 7, "Join"),
        ("Planungszonen_V2_d_A.ili", "view_pz", 2, "Join"),
        ("Planungszonen_V2_d_B.ili", "view_pz", 1, "Projection"),
    ],
)
def test_view_structure_builds_correctly(fname, view_name, base_count, kind):
    """FormationKind/RenamedBaseView/Where build correctly - only ClassAttribute is the known gap."""
    builder = _build(fname)
    [view] = [v for v in _views(builder) if v.Name == view_name]
    assert view.FormationKind == kind
    assert len(view.RenamedBaseView or []) == base_count


@pytest.mark.parametrize(
    "fname,view_name,expected",
    [
        (
            "IVS_V3_d.ili", "ivs_nat",
            {
                "wkb_geometry": "LineType", "ivs_nummer": "TextType", "ivs_signatur": "TextType",
                "ivs_kanton": None,  # external CHAdminCodes_V2.CHCantonCode - not loaded by this hermetic repository
                "ivs_sladatehist": "FormattedType", "ivs_sladatemorph": "FormattedType",
                "ivs_slabedeutung": "EnumType", "ivs_sortsla": "TextType", "ivs_slaname": "TextType",
            },
        ),
        (
            "Planungszonen_V2_d_A.ili", "view_pz",
            {
                "wkb_geometry": "LineType",
                "publiziert_ab": "FormattedType", "gueltig_bis": "FormattedType",  # INTERLIS.XMLDate, now resolved via the predefined namespace
                "rechtsstatus": "EnumType", "bemerkungen": "TextType", "code_typ": "TextType",
                "bezeichnung_typ": "TextType", "abkuerzung_typ": "TextType",
                "festlegung_stufe_typ": "EnumType", "bemerkung_typ": "TextType",
            },
        ),
        (
            # PROJECTION OF an ASSOCIATION: hop 1 selects the association
            # (TypPZ_Planungszone), hop 2 is one of ITS ROLES (Planungszone/
            # TypPZ, resolved via AssocRole/BaseClass, not a plain
            # ClassAttribute), hop 3 is a plain attribute on the role's
            # target class - same expected Types as the JOIN OF form above.
            "Planungszonen_V2_d_B.ili", "view_pz",
            {
                "wkb_geometry": "LineType",
                "publiziert_ab": "FormattedType", "gueltig_bis": "FormattedType",
                "rechtsstatus": "EnumType", "bemerkungen": "TextType", "code_typ": "TextType",
                "bezeichnung_typ": "TextType", "abkuerzung_typ": "TextType",
                "festlegung_stufe_typ": "EnumType", "bemerkung_typ": "TextType",
            },
        ),
    ],
)
def test_name_assign_expression_view_attributes_are_built_with_resolved_types(fname, view_name, expected):
    """`Name := expression` view attributes now build one `AttrOrParam` each, `Final=True`, `Type` resolved when possible.

    `Type` stays unset for `ivs_kanton` only - the ONE attribute referencing
    a domain from a real external model this hermetic test's
    `ModelRepository` doesn't load (`CHAdminCodes_V2.CHCantonCode`), a
    pre-existing external-import resolution characteristic unrelated to
    this feature (confirmed: the SAME attribute is already an
    `UnresolvedNamedReference` directly on the base `ivs_kantone` class,
    before any VIEW machinery is involved). `INTERLIS.XMLDate` used to be
    unresolved too, but now resolves via the predefined `INTERLIS`
    namespace (`builder/repository.py`'s `_PREDEFINED_INTERLIS_SOURCE`).
    """
    builder = _build(fname)
    [view] = [v for v in _views(builder) if v.Name == view_name]
    attrs = getattr(view, "ClassAttribute", None) or []
    assert {a.Name: a for a in attrs}.keys() == expected.keys()
    for attr in attrs:
        assert attr.Final is True
        type_instance = getattr(attr, "Type", None)
        expected_kind = expected[attr.Name]
        if expected_kind is None:
            assert not isinstance(type_instance, MetaInstance)
        else:
            assert isinstance(type_instance, MetaInstance)
            assert type_instance._qualified_class == f"IlisMeta16.ModelData.{expected_kind}"
