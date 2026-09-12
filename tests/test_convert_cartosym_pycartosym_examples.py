"""INTERLIS models/signatures for 5 of pycartosym's own `examples/sld/*.sld` fixtures, round-tripped through ili2ogc.

`tests/fixtures/cartosym/pycartosym_examples.ili`/`.xtf` are hand-built
(see the `.ili`'s own header and `tests/fixtures/cartosym/NOTICE` for why
- no real `.ili` pairs a live `SIGN BASKET`+`Sign := {name}` with a
buildable schema), but every wire shape they use matches real corpus
usage found elsewhere this session (`RoadsExgm2ien.ili`,
`Point_Graphics_Signatures.xtf`). Colors are real LCh conversions of each
target example's own hex values (`tests/test_convert_color.py`'s
independent inverse pipeline).

Not attempted, with reasons: `15-image-marker` (tiled/arbitrary external
graphics - `StandardSymbology` has no external-image reference mechanism
at all, only a font glyph or a composite vector geometry), `5-else-rule`
(CartoSym-CSS cascade vs. SLD `ElseFilter` - a structural mismatch, not
an INTERLIS mapping question), `8-comparisons` (`Between`/`Like` - the
INTERLIS grammar itself has neither operator), `9-metadata`/`10-14`
raster (no `StandardSymbology` equivalent at all).
"""

from pathlib import Path

import pycartosym
from conftest import build_from_text

from interlis.convert.cartosym import SignLibrary, styling_rule_from_drawing_rule, write_sld
from interlis.xtf.parse import parse_xtf

_ILI = Path(__file__).parent / "fixtures" / "cartosym" / "pycartosym_examples.ili"
_XTF = Path(__file__).parent / "fixtures" / "cartosym" / "pycartosym_examples.xtf"


def _sign_library() -> SignLibrary:
    transfer = parse_xtf(_XTF)
    return SignLibrary(transfer.baskets[0])


def _drawing_rules(graphic_name: str):
    builder = build_from_text(_ILI.read_text())
    graphic = builder.symbol_table.resolve(f"PycartosymExamples.T.{graphic_name}")
    return graphic.DrawingRule if isinstance(graphic.DrawingRule, list) else [graphic.DrawingRule], graphic


def _write_rules(rules) -> str:
    return write_sld(pycartosym.models.styles.Style(styling_rules=rules))


def test_example_1_polygon_fill_stroke():
    """Target: `examples/sld/1-polygon-fill-stroke.sld` - `se:Fill`(#808080/0.5) + `se:Stroke`(#202020/2)."""
    rules, graphic = _drawing_rules("Landuse1_Graphics")
    styling_rule = styling_rule_from_drawing_rule(
        rules[0], sign_library=_sign_library(), feature_type=graphic.Base.Name
    )
    assert styling_rule.selector == {
        "op": "and",
        "args": [
            {"op": "=", "args": [{"sysId": "dataLayer.id"}, "Landuse1"]},
            {"op": "=", "args": [{"property": "FunctionCode"}, "parking"]},
        ],
    }
    xml = _write_rules([styling_rule])
    assert "<se:PolygonSymbolizer>" in xml
    assert '"fill">#808080<' in xml
    assert '"fill-opacity">0.5<' in xml
    assert '"stroke">#202020<' in xml
    assert '"stroke-width">2<' in xml
    assert "<ogc:PropertyIsEqualTo>" in xml


def test_example_2_line_stroke_dash():
    """Target: `examples/sld/2-line-stroke-dash.sld` - `se:Stroke`(#a9a9a9, width 3, dash "4 2")."""
    rules, graphic = _drawing_rules("Roads_Graphics")
    styling_rule = styling_rule_from_drawing_rule(
        rules[0], sign_library=_sign_library(), feature_type=graphic.Base.Name
    )
    xml = _write_rules([styling_rule])
    assert "<se:LineSymbolizer>" in xml
    assert '"stroke">#a9a9a9<' in xml
    assert '"stroke-width">3<' in xml
    assert '"stroke-dasharray">4 2<' in xml


def test_example_4_text_label():
    """Target: `examples/sld/4-text-label.sld` - `se:TextSymbolizer`/`se:Font`(Arial/12)/`se:AnchorPoint`.

    `se:Fill` on the text itself (its color) is NOT reproduced -
    `pycartosym`'s SLD writer reads `font.color`/`font.opacity` but its
    own `Font` pydantic model has neither field (`extra="forbid"`,
    confirmed empirically) - a real, current gap in `pycartosym`, not an
    INTERLIS mapping question. `se:Displacement` also has no INTERLIS
    `TextSign` PARAMETER to source it from (only `HAli`/`VAli`, i.e.
    `AnchorPoint`) - not attempted.
    """
    rules, graphic = _drawing_rules("Amenities_Graphics")
    styling_rule = styling_rule_from_drawing_rule(
        rules[0], sign_library=_sign_library(), feature_type=graphic.Base.Name
    )
    xml = _write_rules([styling_rule])
    assert "<se:TextSymbolizer>" in xml
    assert "<ogc:PropertyName>Name</ogc:PropertyName>" in xml
    assert '"font-family">Arial<' in xml
    assert '"font-size">12<' in xml
    assert "<se:AnchorPointX>0</se:AnchorPointX>" in xml
    assert "<se:AnchorPointY>0.5</se:AnchorPointY>" in xml


def test_example_3_point_dot_mark():
    """Target: `examples/sld/3-point-dot-mark.sld` - one `se:Rule`, 2 stacked `se:PointSymbolizer`/`se:Mark`.

    A single `SymbolSign` referencing one composite `FontSymbol` (2
    stacked circular `FontSymbol_Surface` items, `Font.Type = symbol`)
    produces one `Marker` with 2 `CircleGraphic` elements - pycartosym's
    writer emits one `se:PointSymbolizer` per marker element, all as
    siblings under the same `se:Rule`, matching the target's "stacked
    Dots" structure exactly (unlike `example_7`'s 2-`DrawingRule` case,
    which produces 2 sibling `se:Rule`s instead).
    """
    rules, graphic = _drawing_rules("Amenities_Dots_Graphics")
    styling_rule = styling_rule_from_drawing_rule(
        rules[0], sign_library=_sign_library(), feature_type=graphic.Base.Name
    )
    xml = _write_rules([styling_rule])
    assert xml.count("<se:PointSymbolizer>") == 2
    assert xml.count("<se:WellKnownName>circle</se:WellKnownName>") == 2
    assert '"fill">#ffffff<' in xml
    assert '"fill">#ffa500<' in xml
    assert "<se:Size>10</se:Size>" in xml
    assert "<se:Size>8</se:Size>" in xml


def test_example_6_feature_type_name():
    """Target: `examples/sld/6-feature-type-name.sld` - `se:FeatureTypeName` with no extra filter."""
    rules, graphic = _drawing_rules("Buildings_Graphics")
    styling_rule = styling_rule_from_drawing_rule(
        rules[0], sign_library=_sign_library(), feature_type=graphic.Base.Name
    )
    assert styling_rule.selector == {"op": "=", "args": [{"sysId": "dataLayer.id"}, "Buildings"]}
    xml = _write_rules([styling_rule])
    assert "<se:FeatureTypeName>Buildings</se:FeatureTypeName>" in xml
    assert "<ogc:Filter>" not in xml  # no leftover filter once dataLayer.id is extracted
    assert '"fill">#f5f5dc<' in xml


def test_example_7_multi_symbolizer_rule():
    """Target: `examples/sld/7-multi-symbolizer-rule.sld` - one `se:Rule`, Polygon(fill+stroke) + Text symbolizers.

    2 `DrawingRule`s under ONE `GRAPHIC` (`OF SurfaceSign`/`OF TextSign`,
    both unconditioned) become 2 top-level `StylingRule`s here - checks
    empirically whether pycartosym's writer merges same-`feature_type`,
    selector-less rules into one `se:Rule`, or keeps them as 2 sibling
    rules (either renders identically per-feature: SLD/SE has no cascade,
    every matching `se:Rule` renders, so 1 rule with 2 symbolizers and 2
    rules with 1 symbolizer each are visually equivalent).
    """
    rules, graphic = _drawing_rules("Landuse7_Graphics")
    styling_rules = [
        styling_rule_from_drawing_rule(r, sign_library=_sign_library(), feature_type=graphic.Base.Name) for r in rules
    ]
    xml = _write_rules(styling_rules)
    assert "<se:FeatureTypeName>Landuse7</se:FeatureTypeName>" in xml
    assert '"fill">#90ee90<' in xml
    assert '"fill-opacity">0.6<' in xml
    assert '"stroke">#006400<' in xml
    assert "<se:TextSymbolizer>" in xml
    assert "<ogc:PropertyName>FunctionTitle</ogc:PropertyName>" in xml
    assert '"font-family">Arial<' in xml
