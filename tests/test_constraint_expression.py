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
  `Class.Constraint`. The 5th (`existenceConstraint`) had the same gap for
  a different reason (a `parent:` binding WAS present, but named the
  wrong association - `ExistenceDef`/`ExistenceConstraint`, its OWN
  `ExistsIn` link, instead of `ClassConstraint`/`Constraint` like the
  other 4) - fixed separately, see `test_existence_constraint_...` below.
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


def test_existence_constraint_attaches_to_its_class_with_a_real_attr_and_existsin():
    """Real-corpus shape (`Axis_V1_1.ili`'s `Owner REQUIRED IN AxisCatalogs...RoadOwner: OwnerCode`), reduced to a
    single model: `Attr` must be a real `PathOrInspFactor` (not the raw Container bag), `ExistsIn` the target class.
    """
    builder = _build("""INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Country =
      Code: TEXT*3;
    END Country;
    CLASS City =
      CountryCode: TEXT*3;
      EXISTENCE CONSTRAINT CountryCode REQUIRED IN Country: Code;
    END City;
  END T;
END Test.
""")
    cls = builder.symbol_table.resolve("Test.T.City")
    assert len(cls.Constraint) == 1
    constraint = cls.Constraint[0]
    assert constraint._qualified_class.endswith("ExistenceConstraint")
    assert constraint.Attr._qualified_class.endswith("PathOrInspFactor")
    assert [pe.Ref for pe in constraint.Attr.PathEls] == ["CountryCode"]
    assert [c.Name for c in constraint.ExistsIn] == ["Country"]


def test_existence_constraint_or_clause_populates_every_target_class():
    """The repeatable `OR ViewableRef : AttributePath` form (eCH-0031 SS3.12) resolves every branch's target class
    into `ExistsIn`, not just the first.
    """
    builder = _build("""INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Country =
      Code: TEXT*3;
    END Country;
    CLASS Region =
      Code: TEXT*3;
    END Region;
    CLASS City =
      CountryCode: TEXT*3;
      EXISTENCE CONSTRAINT CountryCode REQUIRED IN Country: Code OR Region: Code;
    END City;
  END T;
END Test.
""")
    cls = builder.symbol_table.resolve("Test.T.City")
    constraint = cls.Constraint[0]
    assert [c.Name for c in constraint.ExistsIn] == ["Country", "Region"]


def test_local_uniqueness_builds_kind_and_uniquedef_from_the_real_corpus_shape():
    """Real corpus shape (`ili_corpus/CHBase_Part4_ADMINISTRATIVEUNITS_V2.ili`): `UNIQUE (LOCAL) Entries: Code;` -
    `Entries` the BAG/LIST OF STRUCTURE attribute, `Code` a member of its element structure. Confirmed via
    `ParserATNSimulator.adaptivePredict` instrumentation that ANTLR's own grammar ambiguity resolves the WRONG way for
    this exact shape (greedily consumes `Entries:` as the rule's optional, unused leading label) -
    `_build_local_uniqueness_def` re-derives the correct split from the raw children instead, ignoring that internal
    choice entirely.
    """
    # reuses this fixture's own ASV/Datum attributes as stand-ins for a role hop + struct member
    cls = _class_with_constraint("UNIQUE (LOCAL) ASV: Datum;")
    constraint = cls.Constraint[0]
    assert constraint._qualified_class.endswith("UniqueConstraint")
    assert constraint.Kind == "LocalU"
    assert len(constraint.UniqueDef) == 1
    path_els = constraint.UniqueDef[0].PathEls
    assert [(pe.Kind, pe.Ref) for pe in path_els] == [("ReferenceAttr", "ASV"), ("ReferenceAttr", "Datum")]


def test_local_uniqueness_multiple_trailing_attributes_become_separate_uniquedef_entries():
    cls = _class_with_constraint("UNIQUE (LOCAL) ASV: Datum, KBfrei;")
    constraint = cls.Constraint[0]
    assert [[(pe.Kind, pe.Ref) for pe in p.PathEls] for p in constraint.UniqueDef] == [
        [("ReferenceAttr", "ASV"), ("ReferenceAttr", "Datum")],
        [("ReferenceAttr", "ASV"), ("ReferenceAttr", "KBfrei")],
    ]


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
    cls = _class_with_constraint('MANDATORY CONSTRAINT KBfrei == #false => Datum != "x";')
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


def test_predefined_function_call_builds_a_real_functioncall_not_its_bare_argument():
    """Real corpus bug (2026-08-27, `ili_corpus/Naturereigniskataster_MGDM_V1.ili`): `factor`'s `INTERLIS DOT
    (Name|URI|UUIDOID) (LPAR ... RPAR)?` alternative had no `when_present` branch, so the generic pass-through swept
    past the `INTERLIS.len` wrapper and returned the single argument's OWN built value - `INTERLIS.len(ASV) == 3` built
    identically to plain `ASV == 3`, silently evaluating/serializing the wrong condition downstream (see
    spec/grammar/mapping/07_constraints.yml's `factor.INTERLIS` entry).
    """
    cls = _class_with_constraint("MANDATORY CONSTRAINT (INTERLIS.len(ASV)) == 3;")
    call = cls.Constraint[0].LogicalExpression.SubExpressions[0]
    assert call._qualified_class.endswith("FunctionCall")
    assert call.Function == "INTERLIS.len"
    assert len(call.Arguments) == 1
    argument = call.Arguments[0]
    assert argument.Kind == "Expression"
    assert argument.Expression._qualified_class.endswith("PathOrInspFactor")
    assert argument.Expression.PathEls[0].Ref == "ASV"
