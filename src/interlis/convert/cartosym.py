"""Convert a resolved INTERLIS `StandardSymbology` sign instance into a pycartosym `Symbolizer`.

`FontSymbol`'s composite geometry (`Font.Type = symbol`) has no target
here yet. `Fill.hatch`/`Stroke.casing`/`centerLine`/`pattern` are not
attempted at all - they raise `NotImplementedError` in pycartosym's
current SLD writer regardless of dialect (confirmed empirically), so no
value built here for them could ever be written out.
"""

from pycartosym.models.symbolizers import Fill, Font, Label, Stroke, TextAlignment, TextGraphic
from pycartosym.models.types import Angle, AngleUnit, RGBColor, UnitType, UnitValue

from interlis.convert.color import lch_to_srgb

_H_ALIGNMENT = {"Left": "left", "Center": "center", "Right": "right"}
# VALIGNMENT has 5 levels (Top/Cap/Half/Base/Bottom), pycartosym's v_alignment
# only 3 (top/middle/bottom) - Cap/Base approximate to the nearest of the
# two, losing the box-vs-baseline distinction (no finer SLD/SE target
# exists: se:AnchorPoint only has 3 vertical positions).
_V_ALIGNMENT = {"Top": "top", "Cap": "top", "Half": "middle", "Base": "bottom", "Bottom": "bottom"}


def color_to_rgb(lum: float, c: float, h: float) -> RGBColor:
    """Convert an INTERLIS `Color` (`L`/`C`/`H`) to `RGBColor` - `T` (transparency) is the caller's job, per site."""
    r, g, b = lch_to_srgb(lum, c, h)
    return RGBColor(r=r, g=g, b=b)


def meters(value: float | None) -> UnitValue | None:
    return None if value is None else UnitValue(value=value, unit=UnitType.METERS)


def degrees(value: float | None) -> Angle | None:
    return None if value is None else Angle(value=value, unit=AngleUnit.DEGREES)


def polyline_sign_to_stroke(
    *,
    color: RGBColor | None = None,
    opacity: float | None = None,
    width: float | None = None,
    join: str | None = None,
    cap: str | None = None,
    dash_pattern: list[float] | None = None,
) -> Stroke:
    """Build a `Stroke` from a `PolylineSign` + its resolved `Style` (`LineStyle_Solid`/`LineStyle_Dashed`) and `Color`.

    `join`/`cap` pass through unchanged: `PolylineAttrs.Join`
    (`bevel`/`round`/`miter`) and `.Caps` (`round`/`butt`) already use the
    exact `stroke-linejoin`/`stroke-linecap` keywords pycartosym's SLD
    writer expects.
    """
    return Stroke(
        color=color,
        opacity=opacity,
        width=meters(width),
        join=join,
        cap=cap,
        dash_pattern=[round(d) for d in dash_pattern] if dash_pattern else None,
    )


def surface_sign_to_fill(*, fill_color: RGBColor | None = None, opacity: float | None = None) -> Fill:
    """Build a `Fill` from a `SurfaceSign`'s resolved `FillColor` - `HatchSymb`/`Clip`/`HatchOrg` have no target."""
    return Fill(color=fill_color, opacity=opacity)


def font_symbol_text_to_graphic(*, character: str, font_face: str | None = None) -> TextGraphic:
    """Build a `TextGraphic` for a text `FontSymbol` (`Font.Type = text`) - the character stands in for the glyph."""
    return TextGraphic(text=character, font=Font(face=font_face))


def text_sign_to_label(
    *,
    text: str,
    font_face: str | None = None,
    height: float | None = None,
    italic: bool | None = None,
    underline: bool | None = None,
    h_alignment: str | None = None,
    v_alignment: str | None = None,
) -> Label:
    """Build a `Label` from a `TextSign` - `Striked`/`ClipBox`/`ClipFont` have no pycartosym target, dropped.

    `h_alignment`/`v_alignment` take INTERLIS's own `HALIGNMENT`/
    `VALIGNMENT` enum values, translated via `_H_ALIGNMENT`/`_V_ALIGNMENT`.
    """
    alignment = TextAlignment(
        h_alignment=_H_ALIGNMENT.get(h_alignment) if h_alignment else None,
        v_alignment=_V_ALIGNMENT.get(v_alignment) if v_alignment else None,
    )
    font = Font(face=font_face, size=meters(height), italic=italic, underline=underline)
    graphic = TextGraphic(text=text, font=font, alignment=alignment)
    return Label(elements=[graphic])


def symbolizer_z_order(priority: float | None) -> float | None:
    """Carry a `PARAMETER Priority` through as `Symbolizer.z_order` - marked "temporary" by pycartosym itself."""
    return priority
