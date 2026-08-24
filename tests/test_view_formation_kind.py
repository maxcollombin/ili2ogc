"""VIEW ... PROJECTION OF / JOIN OF ... WHERE: FormationKind + RenamedBaseView.

Real gap found while adding VIEW support to InterlisModelBuilder (backlog
item 8, .claude/PROGRESS.md): spec/grammar/mapping/09_views_graphics.yml's
`viewDef.FormationKind` binding used a plain `source: {field: formationDef,
...}`, which made the generic engine `visit()` `formationDef()` a SECOND
time on top of the natural unclaimed-children sweep that already builds and
attaches every `RenamedBaseView` reachable from it - doubling every
`RenamedBaseView` instance built, and assigning `FormationKind` to whatever
that extra visit returned (a `RenamedBaseView` instance, or a list of them)
instead of an enum string. A second, distinct duplication source affected
`PROJECTION OF` specifically (single base): `_attach_unclaimed_results`
re-attached the single `RenamedBaseView` bubbling up from `formationDef` a
second time, since neither `formationDef` nor its own sub-rules
(projection/join/...) declare a `parent:` of their own - the check that
normally skips an already-self-attached child never fired. `JOIN OF`/
`UNION OF` never hit this because their bubbled-up value is a list, not a
`MetaInstance`. Fixed by removing `FormationKind`'s `source:` (now set by
`InterlisModelBuilder._set_view_formation_kind`, reading the raw
`ViewDefContext` directly) and excluding `formationDef` explicitly in
`_attach_unclaimed_results`.
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _views(builder):
    return {
        inst.Name: inst
        for inst in builder.symbol_table.all_registered()
        if getattr(inst, "_qualified_class", None) == "IlisMeta16.ModelData.View"
    }


PROJECTION_SRC = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        Attr1: TEXT*20;
    END VP;
  END Views;
END Test.
"""

JOIN_SRC = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
    CLASS C =
      Attr3: TEXT*30;
    END C;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VJ
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE
        B->Attr1 == C->Attr3;
      =
      ATTRIBUTE
        Attr1: TEXT*20;
    END VJ;
  END Views;
END Test.
"""


def test_projection_of_sets_formation_kind_and_single_base_no_duplicate():
    view = _views(_build(PROJECTION_SRC))["VP"]
    assert view.FormationKind == "Projection"
    bases = view.RenamedBaseView or []
    assert len(bases) == 1
    assert bases[0].BaseView.Name == "B"


def test_join_of_where_sets_formation_kind_and_both_bases_no_duplicate():
    view = _views(_build(JOIN_SRC))["VJ"]
    assert view.FormationKind == "Join"
    bases = view.RenamedBaseView or []
    assert [b.Name for b in bases] == ["B", "C"]
    assert len({id(b) for b in bases}) == len(bases)
    assert view.Where is not None
