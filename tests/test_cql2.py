"""`CONSTRAINT` Expression -> CQL2-JSON filter (OGC API - Features Part 3: Filtering, `convert/cql2.py`).

Mirrors `tests/test_constraint_eval.py`'s own hermetic models one level
earlier: same AST, compiled instead of evaluated. The real corpus pattern
(`ERKAS_Strassen_V2_0.ili`, cited in
`mappings/ilismeta16-to-jsonschema-rules.yml`'s `Constraint` entry) is
reused here too, to keep both modules' test coverage aligned.
"""

import pytest
from conftest import build_from_text as _build

from interlis.convert.constraint_eval import UnsupportedExpressionError
from interlis.convert.cql2 import constraint_to_cql2, cql2_unsupported_reason


def _constraint(constraint_src: str, extra_attrs: str = ""):
    src = f"""INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE S =
      Sub: TEXT*10;
    END S;
    CLASS A =
      KBfrei: BOOLEAN;
      Datum: TEXT*20;
      REPflichtPers: TEXT*20;
      Anz: 0 .. 9999;
      Nested: S;
{extra_attrs}
      {constraint_src}
    END A;
  END T;
END Test.
"""
    cls = _build(src).symbol_table.resolve("Test.T.A")
    [constraint] = [c for c in cls.Constraint if c._qualified_class.endswith("SimpleConstraint")]
    return constraint


def test_equality_operator():
    filt = constraint_to_cql2(_constraint("MANDATORY CONSTRAINT KBfrei == #false;"))
    assert filt == {"op": "=", "args": [{"property": "KBfrei"}, False]}


def test_all_relational_operators():
    cases = {
        "==": "=",
        "!=": "<>",
        "<": "<",
        ">": ">",
        "<=": "<=",
        ">=": ">=",
    }
    for interlis_op, cql2_op in cases.items():
        filt = constraint_to_cql2(_constraint(f"MANDATORY CONSTRAINT Anz {interlis_op} 10;"))
        assert filt == {"op": cql2_op, "args": [{"property": "Anz"}, 10]}


def test_and_or_not():
    filt = constraint_to_cql2(_constraint("MANDATORY CONSTRAINT NOT (KBfrei == #false AND Anz >= 10);"))
    assert filt == {
        "op": "not",
        "args": [
            {
                "op": "and",
                "args": [
                    {"op": "=", "args": [{"property": "KBfrei"}, False]},
                    {"op": ">=", "args": [{"property": "Anz"}, 10]},
                ],
            }
        ],
    }


def test_implication_rewritten_as_not_or():
    """CQL2 has no direct implication operator - `A => B` == `NOT A OR B` (module docstring)."""
    filt = constraint_to_cql2(_constraint("MANDATORY CONSTRAINT (Anz >= 10) => (KBfrei == #false);"))
    assert filt == {
        "op": "or",
        "args": [
            {"op": "not", "args": [{"op": ">=", "args": [{"property": "Anz"}, 10]}]},
            {"op": "=", "args": [{"property": "KBfrei"}, False]},
        ],
    }


def test_defined_compiles_to_negated_is_null():
    filt = constraint_to_cql2(_constraint("MANDATORY CONSTRAINT DEFINED (REPflichtPers);"))
    assert filt == {"op": "not", "args": [{"op": "isNull", "args": [{"property": "REPflichtPers"}]}]}


def test_multi_hop_structure_path_becomes_a_dotted_property():
    filt = constraint_to_cql2(_constraint('MANDATORY CONSTRAINT Nested->Sub == "x";'))
    assert filt == {"op": "=", "args": [{"property": "Nested.Sub"}, "x"]}


def test_real_corpus_pattern_compiles_full_tree():
    # NOT (KBfrei == #false AND Datum != "1991-02-27") OR DEFINED (REPflichtPers)
    filt = constraint_to_cql2(
        _constraint('MANDATORY CONSTRAINT NOT (KBfrei == #false AND Datum != "1991-02-27") OR DEFINED (REPflichtPers);')
    )
    assert filt == {
        "op": "or",
        "args": [
            {
                "op": "not",
                "args": [
                    {
                        "op": "and",
                        "args": [
                            {"op": "=", "args": [{"property": "KBfrei"}, False]},
                            {"op": "<>", "args": [{"property": "Datum"}, "1991-02-27"]},
                        ],
                    }
                ],
            },
            {"op": "not", "args": [{"op": "isNull", "args": [{"property": "REPflichtPers"}]}]},
        ],
    }


def test_function_call_raises_unsupported():
    """`INTERLIS.len(...)` (real corpus pattern, `Naturereigniskataster_*` - also used in VIEW WHERE tests)
    builds as a `FunctionCall` node - outside constraint_eval.py's scope, so outside CQL2 Lot 1 too."""
    with pytest.raises(UnsupportedExpressionError):
        constraint_to_cql2(_constraint("MANDATORY CONSTRAINT INTERLIS.len(Datum) >= 3;"))


def test_unique_constraint_is_unsupported_not_applicable():
    src = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Code: TEXT*10;
      UNIQUE Code;
    END A;
  END T;
END Test.
"""
    cls = _build(src).symbol_table.resolve("Test.T.A")
    [constraint] = cls.Constraint
    reason = cql2_unsupported_reason(constraint)
    assert reason is not None
    assert "population" in reason or "basket" in reason
    with pytest.raises(ValueError, match="cannot compile constraint to CQL2"):
        constraint_to_cql2(constraint)


def test_plausibility_percentage_constraint_is_unsupported():
    constraint = _constraint("CONSTRAINT >= 50 % KBfrei == #false;")
    reason = cql2_unsupported_reason(constraint)
    assert reason is not None
    with pytest.raises(ValueError):
        constraint_to_cql2(constraint)
