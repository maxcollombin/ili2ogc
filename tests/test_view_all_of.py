"""VIEW ... ATTRIBUTE ALL OF <Name>; -> View.ClassAttribute.

Real gap found while adding VIEW support to InterlisModelBuilder (backlog
item 8, .claude/PROGRESS.md): spec/grammar/mapping/09_views_graphics.yml's
`viewAttributes.all_of_redefinition` binding is `status: not_applicable` by
design ("ALL OF Name" has no dedicated metamodel construct of its own - see
that entry's note) and documents that the ModelBuilder itself must expand
it into real `ClassAttr`/`AttrOrParam` instances - never implemented before
this. Without it, a View built via `ALL OF` structurally exists but exposes
no attribute list at all, unusable by a future View -> JSON Schema stage.

`InterlisModelBuilder._expand_view_all_of`/`_apply_pending_view_all_of`
copy the OWN attributes (Name + Type, via the non-composite
`AttrOrParamType` association - the same `Type` instance is safely shared,
never duplicated) of the `RenamedBaseView` matched by "ALL OF <Name>" (by
its rename alias if any, else the base Class's own short name). Deferred
until after `forward_refs.resolve_all()` (the matched RenamedBaseView.BaseView
can still be an unresolved ForwardRef at construction time for a
forward/cross-file base).

Real corpus caveat, confirmed by direct AST inspection and since FIXED at
the grammar level (2026-08-24, `vendor/interlis-antlr4/InterlisParser.g4`,
matching the official EBNF, Reference Manual eCH-0031 V2.1.0 §3.15):
`ili_corpus/ERKAS_Strassen_V2_0.ili`'s `VIEW vVA`/`vER` each have TWO
consecutive "ALL OF <Name>;" statements (one per JOIN OF base) - the
grammar's OLD `viewAttributes()` only supported ONE "ALL OF" per call (a
single pick among 4 mutually exclusive top-level alternatives), silently
swallowing the 2nd (and any further) "ALL OF" into a bogus
`constraintDef()` match instead (confirmed: it became its own
`ConstraintDefContext` with no usable content) - losing an entire base's
worth of attributes for such a View. Fixed by rewriting the rule as a
proper `(ALL OF Name SEMI | attributeDef | Name (Properties)? ASSIGN
expression SEMI)*` loop (0..* repetition of 3 alternatives), per the EBNF:
`ViewAttributes = [ATTRIBUTE] {'ALL' 'OF' Base-Name ';' | AttributeDef |
Attribute-Name Properties<...> ':=' Expression ';'}.` - which needed a
matching `InterlisModelBuilder` change too
(`_all_of_base_names`/`_apply_pending_view_all_of`): `Name` is now a MULTI
accessor shared by both the "ALL OF Name" and "Name := expression" forms,
so extracting "which Name belongs to which ALL OF" needs a positional walk
(an `ALL` terminal is always immediately followed by `OF` then its base
`Name`), not a naive first-match. `test_multiple_all_of_all_captured`
below reproduces the exact real-corpus pattern synthetically (no
dependency on the gitignored `ili_corpus/` being present) and locks in the
fixed behavior - both bases' attributes now recoverable, in order.
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


def test_bare_attributedef_already_generic_no_change_needed():
    """3rd viewAttributes() alternative (bare `attributeDef`, e.g. `MyAttr:
    TEXT*30;`) already populates View.ClassAttribute via the existing
    generic engine (attributeDef_alt's own `parent: {association: ClassAttr,
    role: ClassAttribute}`, same mechanism already used by classDef) -
    confirmed empirically, no builder change was needed for this
    alternative specifically (unlike all_of_redefinition above)."""
    src = """INTERLIS 2.3;
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
        MyAttr: TEXT*30;
    END VP;
  END Views;
END Test.
"""
    view = _views(_build(src))["VP"]
    assert [a.Name for a in (view.ClassAttribute or [])] == ["MyAttr"]


def test_single_all_of_copies_own_attributes_with_type():
    src = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
      Attr2: NUMERIC;
    END B;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
  END Views;
END Test.
"""
    view = _views(_build(src))["VP"]
    attrs = view.ClassAttribute or []
    assert [a.Name for a in attrs] == ["Attr1", "Attr2"]
    assert attrs[0].Type._qualified_class == "IlisMeta16.ModelData.TextType"
    assert attrs[1].Type._qualified_class == "IlisMeta16.ModelData.NumType"


def test_all_of_forward_referenced_base_still_resolves():
    """The "ALL OF" base is declared in a TOPIC textually AFTER the VIEW's
    own TOPIC (reached via DEPENDS ON) - RenamedBaseView.BaseView is still
    a ForwardRef when the View itself is built, only resolved at the very
    end of build() (see _apply_pending_view_all_of's docstring)."""
    src = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
  END Views;
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
  END Base;
END Test.
"""
    view = _views(_build(src))["VP"]
    assert [a.Name for a in (view.ClassAttribute or [])] == ["Attr1"]


def test_multiple_all_of_all_captured():
    src = """INTERLIS 2.3;
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
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJ;
  END Views;
END Test.
"""
    view = _views(_build(src))["VJ"]
    # Both bases' attributes recoverable, in source order (real-corpus
    # pattern from ERKAS_Strassen_V2_0.ili - see module docstring).
    assert [a.Name for a in (view.ClassAttribute or [])] == ["Attr1", "Attr3"]


def test_all_of_interleaved_with_bare_attributedef():
    """The EBNF's repeated group allows "ALL OF"/attributeDef/"Name :="
    freely interleaved, in any order - exercises that "ALL"'s positional
    walk (_all_of_base_names) isn't thrown off by an attributeDef sitting
    between two "ALL OF" clauses.

    Known, documented limitation (no real corpus evidence of this exact
    interleaving to justify more effort, RULE #7 - ERKAS_Strassen_V2_0.ili
    only ever has consecutive "ALL OF" clauses, never interleaved with a
    bare attributeDef): relative SOURCE ORDER between "ALL OF" and
    attributeDef attributes is NOT preserved in `View.ClassAttribute` -
    attributeDef attaches immediately during normal tree construction (the
    existing generic engine), while "ALL OF" expansion is deferred to the
    very end of build() (needs forward refs resolved first - see
    `_apply_pending_view_all_of`'s docstring), so ALL "ALL OF"-sourced
    attributes always end up AFTER every attributeDef-sourced one,
    regardless of their relative position in the source text. Harmless for
    a future View -> JSON Schema stage (JSON object key order isn't
    semantically significant), asserted here as a SET rather than a
    sequence for that reason."""
    src = """INTERLIS 2.3;
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
      =
      ATTRIBUTE
        ALL OF B;
        Extra: TEXT*10;
        ALL OF C;
    END VJ;
  END Views;
END Test.
"""
    view = _views(_build(src))["VJ"]
    assert {a.Name for a in (view.ClassAttribute or [])} == {"Attr1", "Extra", "Attr3"}
