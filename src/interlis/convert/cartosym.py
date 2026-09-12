"""Convert a resolved INTERLIS `StandardSymbology` sign instance into a pycartosym `Symbolizer`/`StylingRule`.

`FontSymbol`'s composite geometry (`Font.Type = symbol`) has no target
here yet. `Fill.hatch`/`Stroke.casing`/`centerLine`/`pattern` are not
attempted at all - they raise `NotImplementedError` in pycartosym's
current SLD writer regardless of dialect (confirmed empirically), so no
value built here for them could ever be written out.
"""

from typing import Any

from pycartosym.models.styles import StylingRule, Symbolizer
from pycartosym.models.symbolizers import Fill, Font, Label, Stroke, TextAlignment, TextGraphic, Transform2D
from pycartosym.models.types import Angle, AngleUnit, RGBColor, UnitType, UnitValue

from interlis.convert.color import lch_to_srgb
from interlis.convert.cql2 import to_cql2
from interlis.metamodel.instance import MetaInstance

_STROKE_SIGN_CLASS = "PolylineSign"
_FILL_SIGN_CLASS = "SurfaceSign"
_TEXT_SIGN_CLASS = "TextSign"
_MARKER_SIGN_CLASS = "SymbolSign"

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
    text: Any = None,
    rotation: Any = None,
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
    `text`/`rotation` accept either a literal value or a CQL2 `PropertyRef`
    dict (`{"property": "..."}`, e.g. from `to_cql2` on a `DrawingRule`'s
    `Txt := Street -> Name`/`Rotation := NamOri` attribute-path assignment)
    - both are attribute-driven per real corpus usage, not fixed per style.
    """
    alignment = TextAlignment(
        h_alignment=_H_ALIGNMENT.get(h_alignment) if h_alignment else None,
        v_alignment=_V_ALIGNMENT.get(v_alignment) if v_alignment else None,
    )
    font = Font(face=font_face, size=meters(height), italic=italic, underline=underline)
    transform = Transform2D(orientation=rotation) if rotation is not None else None
    graphic = TextGraphic(text=text, font=font, alignment=alignment, transform=transform)
    return Label(elements=[graphic])


def symbolizer_z_order(priority: float | None) -> float | None:
    """Carry a `PARAMETER Priority` through as `Symbolizer.z_order` - marked "temporary" by pycartosym itself."""
    return priority


def styling_rule_from_drawing_rule(drawing_rule: MetaInstance) -> StylingRule:
    """Build one pycartosym `StylingRule` from a built INTERLIS `DrawingRule`.

    Covers what a `DrawingRule` can express WITHOUT resolving a `Sign :=
    {name}` reference's own library-object data (not yet wired): the
    `WHERE` selector (compiled
    via `cql2.to_cql2`, the same CQL2-JSON shape `StylingRule.selector`
    expects natively), `Priority` -> `z_order`, and - for a `TextSign`
    only, since `Txt`/`Rotation`/`HAli`/`VAli` are genuine `PARAMETER`s
    there (`AbstractSymbology.Signs.TextSign`), unlike `Height`/`Font`/
    color which are OWN attributes only ever set via the referenced Sign
    object - a `Label`. `PolylineSign`/`SurfaceSign`/`SymbolSign` get no
    `Stroke`/`Fill`/`Marker` here yet: their color/width/symbol all come
    from the `Sign` reference, not a direct `PARAMETER`.

    A `DrawingRule` with more than one `CondSignParamAssignment` (several
    independent `WHERE (...)` blocks under the same rule name, which would
    need `StylingRule.nested_rules`) has no real corpus example - only the
    first is used here (RULE #7).
    """
    conditions = drawing_rule.Rule if isinstance(drawing_rule.Rule, list) else [drawing_rule.Rule]
    cond = conditions[0]
    where = getattr(cond, "Where", None)
    selector = to_cql2(where) if where is not None else None

    raw_assignments = cond.Assignments if isinstance(cond.Assignments, list) else [cond.Assignments]
    params: dict[str, Any] = {}
    for assignment in raw_assignments:
        if assignment.Param in ("Sign", "Geometry"):
            # Sign: resolves to a MetaObjectDef, not an Expression - its own
            # data needs XTF wiring, not yet done. Geometry: which attribute
            # carries the geometry, not a Symbolizer field.
            continue
        params[assignment.Param] = to_cql2(assignment.Assignment)

    symbolizer_kwargs: dict[str, Any] = {}
    if "Priority" in params:
        symbolizer_kwargs["z_order"] = symbolizer_z_order(params["Priority"])
    sign_class = getattr(getattr(drawing_rule, "Class", None), "Name", None)
    if sign_class == _TEXT_SIGN_CLASS and "Txt" in params:
        symbolizer_kwargs["label"] = text_sign_to_label(
            text=params.get("Txt"),
            rotation=params.get("Rotation"),
            h_alignment=params.get("HAli"),
            v_alignment=params.get("VAli"),
        )

    return StylingRule(
        name=getattr(drawing_rule, "Name", None),
        selector=selector,
        symbolizer=Symbolizer(**symbolizer_kwargs),
    )
