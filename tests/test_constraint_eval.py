"""CONSTRAINT evaluation against a real Feature `properties` dict.

Real corpus pattern exercised end-to-end here (`ERKAS_Strassen_V2_0.ili`,
cited in mappings/ilismeta16-to-jsonschema-rules.yml's `Constraint` entry):
`NOT (KBfrei == #false AND Datum != "1991-02-27") OR DEFINED (REPflichtPers)`.
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.constraint_eval import check_feature_constraints, describe_expression, evaluate_expression
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
      REPflichtPers: TEXT*20;
      Anz: 0 .. 9999;
{extra_attrs}
      {constraint_src}
    END A;
  END T;
END Test.
"""
    builder = _build(src)
    return builder.symbol_table.resolve("Test.T.A")


def test_mandatory_constraint_satisfied_produces_no_message():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    assert check_feature_constraints({"KBfrei": False}, cls) == []


def test_mandatory_constraint_violated_produces_one_message():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    messages = check_feature_constraints({"KBfrei": True}, cls)
    assert len(messages) == 1
    assert "KBfrei == #false" in messages[0]


def test_numeric_relational_operators():
    cls = _class_with_constraint("MANDATORY CONSTRAINT Anz >= 10;")
    assert check_feature_constraints({"Anz": 10}, cls) == []
    assert check_feature_constraints({"Anz": 15}, cls) == []
    assert len(check_feature_constraints({"Anz": 5}, cls)) == 1


def test_text_relational_operator():
    cls = _class_with_constraint('MANDATORY CONSTRAINT Datum != "1991-02-27";')
    assert check_feature_constraints({"Datum": "2000-01-01"}, cls) == []
    assert len(check_feature_constraints({"Datum": "1991-02-27"}, cls)) == 1


def test_real_corpus_pattern_short_circuits_on_the_not_branch():
    # NOT (KBfrei == #false AND Datum != "1991-02-27") is True here (Datum
    # equals the excluded literal) - the OR DEFINED(...) branch is never
    # needed, so REPflichtPers being absent from properties must not matter.
    cls = _class_with_constraint(
        'MANDATORY CONSTRAINT NOT (KBfrei == #false AND Datum != "1991-02-27") OR DEFINED (REPflichtPers);',
    )
    assert check_feature_constraints({"KBfrei": False, "Datum": "1991-02-27"}, cls) == []


def test_real_corpus_pattern_falls_through_to_defined_branch():
    cls = _class_with_constraint(
        'MANDATORY CONSTRAINT NOT (KBfrei == #false AND Datum != "1991-02-27") OR DEFINED (REPflichtPers);',
    )
    # NOT(...) is False here (both operands of AND are true) - satisfying
    # the constraint now depends entirely on DEFINED(REPflichtPers).
    props_without = {"KBfrei": False, "Datum": "2000-01-01"}
    assert len(check_feature_constraints(props_without, cls)) == 1
    props_with = {"KBfrei": False, "Datum": "2000-01-01", "REPflichtPers": "x"}
    assert check_feature_constraints(props_with, cls) == []


def test_unique_constraint_is_skipped_not_evaluated():
    cls = _class_with_constraint("UNIQUE Datum;")
    # A UNIQUE constraint needs the whole collection, not one Feature - this
    # function must never report a false violation for it.
    assert check_feature_constraints({"Datum": "anything"}, cls) == []


def test_plausibility_percentage_constraint_is_skipped():
    cls = _class_with_constraint("CONSTRAINT >= 50 % KBfrei == #false;")
    # Population-level statistic, not evaluable against a single Feature -
    # must not be reported as violated even though this Feature fails it.
    assert check_feature_constraints({"KBfrei": True}, cls) == []


def test_relational_comparison_against_a_missing_attribute_is_skipped():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    assert check_feature_constraints({}, cls) == []


def test_evaluate_expression_and_describe_expression_directly():
    cls = _class_with_constraint("MANDATORY CONSTRAINT KBfrei == #false;")
    expr = cls.Constraint[0].LogicalExpression
    assert evaluate_expression(expr, {"KBfrei": False}) is True
    assert evaluate_expression(expr, {"KBfrei": True}) is False
    assert describe_expression(expr) == "KBfrei == #false"
