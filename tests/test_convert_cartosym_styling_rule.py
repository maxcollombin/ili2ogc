"""`convert/cartosym.py::styling_rule_from_drawing_rule` - built from real `DrawingRule` instances, via the SLD writer.

Model shapes mirror `RoadsExgm2ien.ili` (the INTERLIS reference manual's
own canonical `GRAPHIC` example, `github.com/MediaComem/FGDM4GS`):
`Building OF ... SurfaceSign: WHERE Type == #building (Sign := {...};
Geometry := Geometry; Priority := 100)` and `StreetName OF ... TextSign:
(Sign := {...}; Txt := Street -> Name; Geometry := NamPos; Rotation :=
NamOri; Priority := 120)` - `Sign := {...}` omitted here (resolves to a
`MetaObjectDef`, deliberately skipped by this function, see its
docstring) to isolate what's under test.
"""

import pycartosym
import pytest
from conftest import build_from_text

from interlis.convert.cartosym import styling_rule_from_drawing_rule

_MODEL = """INTERLIS 2.3;

MODEL TestGraphicStyling (en)
AT "mailto:test@example.org"
VERSION "2024-01-01" =
  TOPIC T =
    CLASS LandCover =
      Type: (building, street, water, other);
      Geometry: TEXT*40;
    END LandCover;

    CLASS SurfaceSign =
      Dummy: TEXT*1;
    END SurfaceSign;

    GRAPHIC Surface_Graphics BASED ON LandCover =
      Building OF SurfaceSign:
        WHERE Type == #building (
          Geometry := Geometry;
          Priority := 100
        );
    END Surface_Graphics;

    CLASS StreetNamePosition =
      NamPos: TEXT*40;
      NamOri: 0.0 .. 359.9;
      Street: TEXT*40;
    END StreetNamePosition;

    CLASS TextSign =
      Dummy: TEXT*1;
    END TextSign;

    GRAPHIC Text_Graphics BASED ON StreetNamePosition =
      StreetName OF TextSign: (
          Txt := Street;
          Geometry := NamPos;
          Rotation := NamOri;
          Priority := 120
      );
    END Text_Graphics;
  END T;
END TestGraphicStyling.
"""

_SLD = pycartosym.get_codec("sld")


def _write(rule) -> str:
    style = pycartosym.models.styles.Style(styling_rules=[rule])
    out = _SLD.write(style)
    return out if isinstance(out, str) else out.decode()


def _drawing_rule(graphic_name: str, rule_name: str):
    builder = build_from_text(_MODEL)
    graphic = builder.symbol_table.resolve(f"TestGraphicStyling.T.{graphic_name}")
    rules = graphic.DrawingRule if isinstance(graphic.DrawingRule, list) else [graphic.DrawingRule]
    return next(r for r in rules if r.Name == rule_name)


def test_surface_sign_where_clause_compiles_to_a_cql2_selector():
    """`SurfaceSign` gets no `Fill` here (color only comes from an unresolved `Sign := {...}`, see module docstring).

    Confirms a real, useful pycartosym behavior found while testing this:
    a `StylingRule` with no symbolizer content (`z_order` only) cannot be
    written to SLD/SE at all - `se:Rule` requires a symbolizer, so
    pycartosym's writer drops it with a warning rather than emit invalid
    SLD. Resolving `Sign := {...}` (this function's documented open point)
    is therefore a HARD prerequisite for real output, not an enhancement.
    """
    styling_rule = styling_rule_from_drawing_rule(_drawing_rule("Surface_Graphics", "Building"))
    assert styling_rule.name == "Building"
    assert styling_rule.selector == {"op": "=", "args": [{"property": "Type"}, "building"]}
    assert styling_rule.symbolizer.z_order == 100
    assert styling_rule.symbolizer.fill is None


def test_text_sign_txt_and_rotation_attribute_paths_become_property_refs():
    """`Txt`/`Rotation` become `PropertyRef`s on the pycartosym model - only `Txt` is currently writable to SLD.

    `Transform2D.orientation` accepts a `PropertyRef` at the model level
    (coerced by pydantic), but pycartosym's SLD writer's angle formatter
    does not yet support an attribute-driven rotation, only a literal
    number - confirmed empirically (raises `NotImplementedError:
    Unsupported angle value shape`). Real corpus data (`RoadsExgm2ien.ili`:
    `Rotation := NamOri`) needs exactly this - noted in the mapping doc.
    """
    styling_rule = styling_rule_from_drawing_rule(_drawing_rule("Text_Graphics", "StreetName"))
    assert styling_rule.selector is None
    assert styling_rule.symbolizer.z_order == 120
    label = styling_rule.symbolizer.label
    graphic = label.elements[0]
    # TextGraphic.text is typed `str | Any` - a PropertyRef-shaped dict stays
    # a raw dict there (no `str` match to coerce against), unlike
    # `transform.orientation` (typed with `PropertyRef` in its union).
    assert graphic.text == {"property": "Street"}
    assert graphic.transform.orientation.property == "NamOri"

    with pytest.raises(NotImplementedError, match="Unsupported angle value shape"):
        _write(styling_rule)
