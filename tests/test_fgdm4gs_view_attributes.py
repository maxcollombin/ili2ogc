"""Real-corpus regression: FGDM4GS derived VIEW models (`tests/fixtures/fgdm4gs/`, see NOTICE).

Backlog item 8's Lot A2 left `viewAttributes`'s `Name := expression` form
(`bare_redefinition_list`/`modifier_reassignment`) `status: unresolved`
in `InterlisModelBuilder`, citing "no real corpus evidence" for it. These
5 real, author-confirmed VIEW models (HEIG-VD's FGDM4GS project) are that
evidence - ALL FIVE use this form exclusively, none use `ALL OF`. This
test documents the CURRENT gap directly (`ClassAttribute` stays empty)
rather than `xfail`, so it fails loudly - and needs a one-line update,
not silent re-enabling - once the form is implemented. See
`.claude/PROGRESS.md` for the backlog item this tracks.

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


@pytest.mark.parametrize("fname,view_name", [
    ("IVS_V3_d.ili", "ivs_nat"),
    ("Planungszonen_V2_d_A.ili", "view_pz"),
    ("Planungszonen_V2_d_B.ili", "view_pz"),
])
def test_name_assign_expression_view_attributes_not_yet_built(fname, view_name):
    """Known gap (2026-08-24): `Name := expression` view attributes are not built at all.

    `View.ClassAttribute` stays empty even though every one of these real
    models defines 9-13 attributes this way - meaning `.ili -> JSON
    Schema`/`.xtf -> JSON-FG` would currently produce an EMPTY properties
    object for any of these real VIEWs. Update this assertion (to the
    real expected attribute names/count) once `viewAttributes`'s
    `Name := expression` form is implemented - do not just delete the test.
    """
    builder = _build(fname)
    [view] = [v for v in _views(builder) if v.Name == view_name]
    assert (getattr(view, "ClassAttribute", None) or []) == []
