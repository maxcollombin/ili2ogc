"""UNIT bracketed short-name alias (unitDef grammar: `UNIT? Name (LSBR Name RSBR)? ...`).

Real corpus bug (PlanerischerGewaesserschutz_V1_1.ili/Hazard_Mapping_V1_3.ili):
`CubicMeterPerSecond [m3sec] = (Units.m3 / INTERLIS.s);` then, elsewhere in
the SAME file, `Menge = 0 .. 100000 [m3sec];` - the alias `m3sec` was never
registered anywhere (only the primary Name), so the later `[m3sec]` unitRef
raised `BuildError: unresolved reference, not attributable to an import`.
Fixed by InterlisModelBuilder._register_unit_alias, called right after the
normal _maybe_register_symbol registration for a unitDef instance.
"""

from conftest import build_from_text as _build


def test_bracketed_unit_alias_resolves_a_later_reference():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  UNIT CubicMeterPerSecond [m3sec] = (Units.m3 / INTERLIS.s);
  DOMAIN
    Menge = 0 .. 100000 [m3sec];
END Foo.
""")
    menge = builder.symbol_table.resolve("Menge")
    unit = menge.model_extra.get("Unit")
    assert unit is not None
    assert unit.Name == "CubicMeterPerSecond"


def test_bracketed_unit_alias_and_primary_name_resolve_to_the_same_instance():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  UNIT Meter [m] = m;
  DOMAIN
    ByPrimary = 0 .. 100 [Meter];
    ByAlias = 0 .. 100 [m];
END Foo.
""")
    by_primary = builder.symbol_table.resolve("ByPrimary").model_extra.get("Unit")
    by_alias = builder.symbol_table.resolve("ByAlias").model_extra.get("Unit")
    assert by_primary is by_alias


def test_unit_without_bracketed_alias_is_unaffected():
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  UNIT Meter = m;
  DOMAIN
    Length = 0..1000 [Meter];
END Foo.
""")
    length = builder.symbol_table.resolve("Length")
    unit = length.model_extra.get("Unit")
    assert unit is not None
    assert unit.Name == "Meter"
