"""NumType.Min/Max (Lot 25) : numeric()._refsys_clause (spec/grammar/mapping/06_types.yml)
always builds a NumsRefSys instance via _build_nested, even when no refsys
clause is present in the source - all its fields stay None (hollow). Before
the fix, InterlisModelBuilder._relay's "single wrapped MetaInstance" fast
path grabbed that hollow instance and returned it AS THE WHOLE RESULT of
numeric(), instead of the bag dict {Min, Max, Circular, Clockwise, Unit} -
silently losing all of it, including on ordinary domains like
`DOMAIN Code = 0..255;` (reproducible on models/IlisMeta16.ili itself).
Fixed by excluding hollow instances from that fast path (SymbolTable's own
`_is_hollow` filtering in visit_wrapped's bag loop, one level up, already
anticipated exactly this case per its own comment - it just never used to
be reached)."""
import warnings
from pathlib import Path

import pytest

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


def test_bare_numeric_range_domain_gets_min_max():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    Bar = 0..255;
END Foo.
"""
    )
    bar = builder.symbol_table.resolve("Bar")
    assert bar.Min == "0"
    assert bar.Max == "255"
    assert bar.Circular is False


def test_multi_declaration_domain_block_all_get_min_max():
    # Reproduit models/IlisMeta16.ili tel quel (bloc DOMAIN multi-declarations).
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    Code = 0..255;
    MultRange = 0..2147483647;
    LengthRange EXTENDS MultRange = 1..2147483647;
END Foo.
"""
    )
    code = builder.symbol_table.resolve("Code")
    mult = builder.symbol_table.resolve("MultRange")
    length = builder.symbol_table.resolve("LengthRange")
    assert (code.Min, code.Max) == ("0", "255")
    assert (mult.Min, mult.Max) == ("0", "2147483647")
    assert (length.Min, length.Max) == ("1", "2147483647")


def test_numeric_domain_with_unit_clause_attaches_unit():
    # Corollaire du fix : Unit (NumUnit association) etait AUSSI silencieusement
    # perdu avant (meme bag jamais atteint), pas seulement Min/Max.
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  UNIT Meter = m;
  DOMAIN
    Length = 0..1000 [Meter];
END Foo.
"""
    )
    length = builder.symbol_table.resolve("Length")
    assert length.Min == "0"
    assert length.Max == "1000"
    unit = length.model_extra.get("Unit")
    assert unit is not None
    assert unit.Name == "Meter"
