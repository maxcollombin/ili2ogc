"""CONSTRAINT attachment and Expression tree correctness.

Investigation triggered by backlog item 8's WHERE-clause scope decision
(.claude/PROGRESS.md, item 8 Lot C/D): a real `MANDATORY CONSTRAINT` build
revealed several real, previously undetected bugs - nothing in this
runtime evaluated an Expression tree before, so none of this was ever
exercised:

- 4 of 5 constraint-producing rules (`mandatoryConstraint`/
  `plausibilityConstraint`/`uniquenessConstraint`/`setConstraint`) had no
  `parent:` binding at all - a constraint declared INSIDE a CLASS/STRUCTURE
  body was built then silently discarded, never reachable from
  `Class.Constraint`.
- `_relay`'s single-key "bag" unwrap shortcut discarded a `feeds_into:`
  rule's real content whenever it had exactly one non-underscore
  attribute_bindings key (e.g. `objectOrAttributePath.PathEls`) - the root
  cause of `PathOrInspFactor.PathEls` staying empty for every plain
  attribute-name factor.
- `_build_conditional`'s presence check never caught a MULTI token
  accessor's empty list (only `is None`), so `term0`/`term1` ALWAYS
  wrapped their single operand in a spurious `CompoundExpr` even with no
  OR/AND/MUL/DIV operator present at all.
- `term`'s Implication branch keyed on a nonexistent `EQ_GT` accessor
  (TermContext exposes separate `EQ()`/`GT()`) - permanently dead.
- `term2`'s `CompoundExpr.Operation` stayed the literal group name
  "Relation" instead of the real matched sub-value (Equal/NotEqual/...) -
  `relation()`'s own binding used composed field names ("EQ+EQ"/"LT+GT")
  that don't match any real ANTLR accessor.
- `DEFINED(...)` always produced `UnaryExpr.SubExpression = None`.
- `PathEl.Kind` always resolved to `None` (same composed-field-name
  issue as `relation()`).
- `Constant.Value` for an enumerated literal (`#false`) was the raw,
  un-joined `enumerationConst` bag `{"Value": ["false"], "Others": False}`
  instead of the plain dotted-path string the metamodel declares.
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


def _class_with_constraint(constraint_src: str, extra_attrs: str = ""):
    src = f"""INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      KBfrei: BOOLEAN;
      Datum: TEXT*20;
      ASV: TEXT*10;
{extra_attrs}
      {constraint_src}
    END A;
  END T;
END Test.
"""
    builder = _build(src)
    return builder.symbol_table.resolve("Test.T.A")


def test_mandatory_constraint_attaches_to_its_class():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    assert len(cls.Constraint) == 1
    assert cls.Constraint[0]._qualified_class.endswith("SimpleConstraint")
    assert cls.Constraint[0].Kind == "MandC"


def test_plausibility_constraint_attaches_to_its_class():
    cls = _class_with_constraint("CONSTRAINT >= 50 % KBfrei == #false;")
    assert len(cls.Constraint) == 1
    assert cls.Constraint[0].Kind == "HighPercC"


def test_uniqueness_constraint_attaches_to_its_class():
    cls = _class_with_constraint("UNIQUE Datum;")
    assert len(cls.Constraint) == 1
    assert cls.Constraint[0]._qualified_class.endswith("UniqueConstraint")


def test_set_constraint_attaches_to_its_class():
    cls = _class_with_constraint("SET CONSTRAINT KBfrei == #false;")
    assert len(cls.Constraint) == 1
    assert cls.Constraint[0]._qualified_class.endswith("SetConstraint")


def test_simple_relation_produces_no_phantom_wrapper():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    expr = cls.Constraint[0].LogicalExpression
    assert expr._qualified_class.endswith("CompoundExpr")
    assert expr.Operation == "Equal"


def test_relation_operators_resolve_to_the_right_operation():
    cases = {
        "==": "Equal",
        "!=": "NotEqual",
        "<>": "NotEqual",
        "<": "Less",
        ">": "Greater",
        "<=": "LessOrEqual",
        ">=": "GreaterOrEqual",
    }
    for op, expected in cases.items():
        cls = _class_with_constraint(f"MANDATORY CONSTRAINT 1 {op} 2;")
        assert cls.Constraint[0].LogicalExpression.Operation == expected, op


def test_implication_operator_builds_compound_expr():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false => Datum != \"x\";")
    expr = cls.Constraint[0].LogicalExpression
    assert expr._qualified_class.endswith("CompoundExpr")
    assert expr.Operation == "Implication"
    assert len(expr.SubExpressions) == 2


def test_and_or_with_no_operator_is_a_plain_pass_through():
    cls = _class_with_constraint("MANDATORY CONSTRAINT DEFINED(KBfrei);")
    expr = cls.Constraint[0].LogicalExpression
    # a lone predicate (no AND/OR/MUL/DIV) must NOT be wrapped in a spurious CompoundExpr
    assert expr._qualified_class.endswith("UnaryExpr")
    assert expr.Operation == "Defined"


def test_path_or_insp_factor_has_a_populated_path_el():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    factor = cls.Constraint[0].LogicalExpression.SubExpressions[0]
    assert factor._qualified_class.endswith("PathOrInspFactor")
    assert len(factor.PathEls) == 1
    path_el = factor.PathEls[0]
    assert path_el.Kind == "ReferenceAttr"
    assert path_el.Ref == "KBfrei"


def test_defined_predicate_sets_subexpression():
    cls = _class_with_constraint("MANDATORY CONSTRAINT DEFINED(ASV);")
    unary = cls.Constraint[0].LogicalExpression
    assert unary._qualified_class.endswith("UnaryExpr")
    assert unary.Operation == "Defined"
    assert unary.SubExpression is not None
    assert unary.SubExpression._qualified_class.endswith("PathOrInspFactor")
    assert unary.SubExpression.PathEls[0].Ref == "ASV"


def test_not_predicate_still_sets_subexpression():
    cls = _class_with_constraint("MANDATORY CONSTRAINT NOT (KBfrei == #false);")
    unary = cls.Constraint[0].LogicalExpression
    assert unary.Operation == "Not"
    assert unary.SubExpression is not None
    assert unary.SubExpression._qualified_class.endswith("CompoundExpr")


def test_enumeration_constant_value_is_a_plain_dotted_string():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    const = cls.Constraint[0].LogicalExpression.SubExpressions[1]
    assert const._qualified_class.endswith("Constant")
    assert const.Type == "Enumeration"
    assert const.Value == "false"
    assert isinstance(const.Value, str)


def test_others_enumeration_constant_value():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #OTHERS;")
    const = cls.Constraint[0].LogicalExpression.SubExpressions[1]
    assert const.Value == "OTHERS"
