"""`GRAPHIC`/`DrawingRule` construction (item 6 CartoSym precursor).

Found via a defensive sweep for the same bug class as `existenceConstraint`'s
`Attr` gap (a `.claude/PROGRESS.md` item 8/9 sweep task): `signParamAssignment`
is the only OTHER real call site of `attributePath()` outside `factor()`'s own
wrapping branch. Investigating it (no fixture/test touched `GRAPHIC` anywhere
in this project before) surfaced that `GRAPHIC` was 100% unbuildable, in
either direction:

- `vendor/interlis-antlr4/InterlisParser.g4`'s `graphicDef` wrote
  `(BASED ON viewableRef)?` using the two SEPARATE tokens `BASED`/`ON`, but
  `InterlisLexer.g4` also declares a combined `BASED_ON : 'BASED ON'` token
  (correctly used elsewhere, `formattedType`) - ANTLR's lexer always prefers
  the longer literal match, so any real "BASED ON" text is ALWAYS lexed as
  the single `BASED_ON` token, never as separate `BASED`+`ON` - the
  `graphicDef` branch could never match. Fixed grammar-side (`BASED_ON`
  instead of `BASED ON`), regenerated (`docs/grammar-regeneration.md`); see
  `docs/upstream-grammar-changes.md` fix #15.
- Even with `BASED ON` omitted (the only way a `GraphicDefContext` could
  previously parse at all), the build then failed: `graphicDef.based_on`
  (`spec/grammar/mapping/09_views_graphics.yml`) read `viewableRef` without
  `optional: true`, even though the grammar clause is `(BASED_ON
  viewableRef)?` - every `GRAPHIC` without a base raised `BuildError`.
- Two further, same-class `optional: true` gaps one level down, both
  confirmed empirically (real `.ili` snippets that BuildError'd before the
  fix): `condSignParamAssignment.Where` (leading `[WHERE] expression` is
  entirely optional, not just the `WHERE` keyword) and
  `drawingRule.restriction_of_class` (`(OF classRef)?` is optional).

Confirms the `Sign := {metaObjectRef}` form (`signParamAssignment`'s 1st
alternative) resolves correctly end-to-end via the generic `ForwardRef`
machinery - no bespoke wrapping needed there, unlike `existenceConstraint`.

**Not covered here, confirmed but NOT fixed (no corpus evidence, RULE #7)**:
the 3rd alternative, `ACCORDING attributePath LPAR enumAssignment
(COMMA enumAssignment)* RPAR` - 0 real occurrences anywhere in this
project's corpus (same as the sub-keys `according_alt`/`metaobjectref_alt`
in the spec, whose `target:`+sibling-`source:` shape `_build_nested` cannot
actually populate - dead documentation, superseded by the generic
`Assignment` alt_rule dispatch this test exercises). A quick probe (not kept
as a test) additionally hit `enumAssignment.MinEnumValue`/`MaxEnumValue`:
`field: enumRange, index: 0/1` applies an index to a single (non-multi)
accessor - `TypeError: enumRange on EnumAssignmentContext does not accept an
index`. Left as a documented gap (`.claude/PROGRESS.md`) for whoever scopes
item 6 (CartoSym), not fixed speculatively without a real example to
validate against.
"""

import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_MODEL_HEADER = """INTERLIS 2.3;

MODEL {name} (en)
AT "mailto:test@example.org"
VERSION "2024-01-01" =
  TOPIC TestTopic =
    CLASS MyClass =
      Attr1: TEXT*10;
    END MyClass;

    GRAPHIC MyGraphic {based_on}=
      MyRule: (Sign := {{MyClass}});
    END MyGraphic;

  END TestTopic;
END {name}.
"""


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def test_graphicdef_based_on_class_builds():
    src = _MODEL_HEADER.format(name="TestGraphicBasedOn", based_on="BASED ON MyClass ")
    builder = _build(src)
    graphic = builder.symbol_table.resolve("TestGraphicBasedOn.TestTopic.MyGraphic")
    assert graphic.Base is not None
    assert graphic.Base.Name == "MyClass"


def test_graphicdef_without_based_on_builds():
    src = _MODEL_HEADER.format(name="TestGraphicNoBase", based_on="")
    builder = _build(src)
    graphic = builder.symbol_table.resolve("TestGraphicNoBase.TestTopic.MyGraphic")
    assert getattr(graphic, "Base", None) is None


def test_sign_param_assignment_meta_object_ref_resolves_to_real_instance():
    src = _MODEL_HEADER.format(name="TestGraphicSignAssign", based_on="BASED ON MyClass ")
    builder = _build(src)
    graphic = builder.symbol_table.resolve("TestGraphicSignAssign.TestTopic.MyGraphic")
    rule = graphic.DrawingRule[0] if isinstance(graphic.DrawingRule, list) else graphic.DrawingRule
    assert rule.Name == "MyRule"
    cond = rule.Rule[0] if isinstance(rule.Rule, list) else rule.Rule
    assert getattr(cond, "Where", None) is None
    spa = cond.Assignments[0] if isinstance(cond.Assignments, list) else cond.Assignments
    assert spa.Param == "Sign"
    # Resolved via the generic ForwardRef machinery (signParamAssignment's
    # own `Assignment` alt_rule binding), not a raw {"PathEls": [...]} bag.
    assert spa.Assignment is not None
    assert spa.Assignment.Name == "MyClass"


def test_drawing_rule_without_of_class_restriction_builds():
    src = """INTERLIS 2.3;

MODEL TestGraphicNoRestriction (en)
AT "mailto:test@example.org"
VERSION "2024-01-01" =
  TOPIC TestTopic =
    CLASS MyClass =
      Attr1: TEXT*10;
    END MyClass;

    GRAPHIC MyGraphic BASED ON MyClass =
      MyRule: (Sign := {MyClass});
    END MyGraphic;

  END TestTopic;
END TestGraphicNoRestriction.
"""
    builder = _build(src)
    graphic = builder.symbol_table.resolve("TestGraphicNoRestriction.TestTopic.MyGraphic")
    rule = graphic.DrawingRule[0] if isinstance(graphic.DrawingRule, list) else graphic.DrawingRule
    assert getattr(rule, "Class", None) is None
