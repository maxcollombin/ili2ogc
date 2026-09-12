"""Convert a resolved INTERLIS `StandardSymbology` sign instance into a pycartosym `Symbolizer`/`StylingRule`.

`FontSymbol`'s composite geometry (`Font.Type = symbol`) has no target
here yet. `Fill.hatch`/`Stroke.casing`/`centerLine`/`pattern` are not
attempted at all - they raise `NotImplementedError` in pycartosym's
current SLD writer regardless of dialect (confirmed empirically), so no
value built here for them could ever be written out.
"""

from typing import Any

from lxml import etree
from pycartosym import get_codec
from pycartosym.models.styles import Style, StylingRule, Symbolizer
from pycartosym.models.symbolizers import Fill, Font, Label, Marker, Stroke, TextAlignment, TextGraphic, Transform2D
from pycartosym.models.types import Angle, AngleUnit, RGBColor, UnitType, UnitValue
from pycartosym.models.value_expressions import PropertyRef

from interlis.convert.color import lch_to_srgb
from interlis.convert.cql2 import to_cql2
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import XtfBasket, XtfObject
from interlis.xtf.validate import _extract_reference

_SE_NS = "http://www.opengis.net/se"
_OGC_NS = "http://www.opengis.net/ogc"

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
    alignment = (
        TextAlignment(
            h_alignment=_H_ALIGNMENT.get(h_alignment) if h_alignment else None,
            v_alignment=_V_ALIGNMENT.get(v_alignment) if v_alignment else None,
        )
        if h_alignment or v_alignment
        else None
    )
    font = Font(face=font_face, size=meters(height), italic=italic, underline=underline)
    transform = Transform2D(orientation=rotation) if rotation is not None else None
    graphic = TextGraphic(text=text, font=font, alignment=alignment, transform=transform)
    return Label(elements=[graphic])


def symbolizer_z_order(priority: float | None) -> float | None:
    """Carry a `PARAMETER Priority` through as `Symbolizer.z_order` - marked "temporary" by pycartosym itself."""
    return priority


class SignLibrary:
    """A parsed `SIGN BASKET` data section, indexed for `Sign := {name}` resolution.

    `by_name` resolves the metaobject identity (`ili:Name`, a raw wire
    child every real SIGN BASKET data object carries even though it is
    never a declared `StandardSymbology` class attribute - confirmed
    against `Point_Graphics_Signatures.xtf`). `by_tid` resolves the
    `ili:ref` target of a REFERENCE-typed attribute (`Color`/`Symbol`/
    `FillColor`/...) to the object it points at, within the same basket.
    """

    def __init__(self, basket: XtfBasket) -> None:
        self.by_name: dict[str, XtfObject] = {}
        self.by_tid: dict[str, XtfObject] = {}
        for obj in basket.objects:
            if obj.tid is not None:
                self.by_tid[obj.tid] = obj
            name_nodes = obj.attributes.get("Name")
            if name_nodes and name_nodes[0].text:
                self.by_name[name_nodes[0].text] = obj

    def scalar(self, obj: XtfObject, attr: str) -> str | None:
        nodes = obj.attributes.get(attr)
        return nodes[0].text if nodes else None

    def resolve_ref(self, obj: XtfObject, attr: str) -> XtfObject | None:
        nodes = obj.attributes.get(attr)
        if not nodes:
            return None
        tid = _extract_reference(nodes[0])
        return self.by_tid.get(tid) if tid else None

    def resolve_color(self, obj: XtfObject, attr: str) -> tuple[RGBColor | None, float | None]:
        """Resolve a `Color`-typed reference attribute to `(RGBColor, opacity)` - `opacity` is `Color.T`."""
        color_obj = self.resolve_ref(obj, attr)
        if color_obj is None:
            return None, None
        lum, c, h, t = (self.scalar(color_obj, name) for name in ("L", "C", "H", "T"))
        color = color_to_rgb(float(lum), float(c), float(h)) if lum and c and h else None
        return color, (float(t) if t else None)

    def dash_pattern(self, obj: XtfObject) -> list[float] | None:
        """Read a `LineStyle_Dashed.Dashes` (`LIST OF DashRec`) as a flat `DLength` list, in wire order."""
        nodes = obj.attributes.get("Dashes")
        if not nodes:
            return None
        lengths = []
        for occurrence in nodes[0].children:
            length_node = next((c for c in occurrence.children if c.tag == "DLength"), None)
            if length_node is not None and length_node.text is not None:
                lengths.append(float(length_node.text))
        return lengths or None


def symbol_sign_object_to_marker(library: SignLibrary, obj: XtfObject) -> Marker:
    """Build a `Marker` from a real `SymbolSign` data object (`Color`/`Symbol` resolved within the same SIGN BASKET).

    Only the `Font.Type = text` case (`Symbol` -> `FontSymbol` -> `Font`)
    has a target - a `Font.Type = symbol` `FontSymbol` (composite
    geometry) has none yet. Verified against real corpus data
    (`Point_Graphics_Signatures.xtf`'s `SymbolSign`/`FontSymbol`/`Font`).
    """
    # `color` (from `SymbolSignColorAssoc`) has no confirmed pycartosym
    # target for a text-glyph Marker yet (neither `TextGraphic` nor `Font`
    # has a color field) - only `opacity` (`Color.T`) is applied here,
    # left as a follow-up rather than guessed.
    _color, opacity = library.resolve_color(obj, "Color")
    graphic = None
    symbol_obj = library.resolve_ref(obj, "Symbol")
    if symbol_obj is not None:
        font_obj = library.resolve_ref(symbol_obj, "Font")
        ucs4 = library.scalar(symbol_obj, "UCS4")
        if font_obj is not None and ucs4 and library.scalar(font_obj, "Type") == "text":
            graphic = font_symbol_text_to_graphic(character=chr(int(ucs4)), font_face=library.scalar(font_obj, "Name"))
    return Marker(elements=[graphic] if graphic else None, opacity=opacity)


def text_sign_object_to_font_kwargs(library: SignLibrary, obj: XtfObject) -> dict[str, Any]:
    """Build `text_sign_to_label` kwargs (`font_face`/`height`/`italic`) from a real `TextSign` data object.

    `Font` (`TextSignFontAssoc`) gives the face; `Height`/`Slanted` are
    `TextSign`'s own attributes directly (not references). `Underlined`
    is deliberately NOT read: pycartosym's SLD writer raises
    `NotImplementedError` for `Font.underline` unconditionally (confirmed
    reading its source) - wiring it would only ever produce a crash, not
    a silently-wrong value, but there is no real `TextSign` data to
    verify against either way.
    """
    kwargs: dict[str, Any] = {}
    font_obj = library.resolve_ref(obj, "Font")
    if font_obj is not None:
        face = library.scalar(font_obj, "Name")
        if face is not None:
            kwargs["font_face"] = face
    height = library.scalar(obj, "Height")
    if height is not None:
        kwargs["height"] = float(height)
    slanted = library.scalar(obj, "Slanted")
    if slanted is not None:
        kwargs["italic"] = slanted == "true"
    return kwargs


def surface_sign_object_to_fill(library: SignLibrary, obj: XtfObject) -> Fill:
    """Build a `Fill` from a real `SurfaceSign` data object's `FillColor` - `HatchSymb`/`Clip`/`HatchOrg` not resolved.

    Same `Color` wire mechanism as `symbol_sign_object_to_marker`
    (verified there against real data) applied to a different attribute
    name (`FillColor` vs `Color`) - not itself corpus-verified (no real
    `SurfaceSign` data object found in the fixtures fetched so far).
    """
    color, opacity = library.resolve_color(obj, "FillColor")
    return surface_sign_to_fill(fill_color=color, opacity=opacity)


def surface_sign_object_border_to_stroke(library: SignLibrary, obj: XtfObject) -> Stroke | None:
    """Build a `Stroke` from a `SurfaceSign` data object's `Border` (`SurfaceSignBorderAssoc`, a `PolylineSign` REF).

    Reuses `polyline_sign_object_to_stroke` on the referenced object - the
    border IS a real `PolylineSign` object, same wire shape as one
    referenced directly by a `PolylineSign` `Sign := {name}`.
    """
    border_obj = library.resolve_ref(obj, "Border")
    return polyline_sign_object_to_stroke(library, border_obj) if border_obj is not None else None


def polyline_sign_object_to_stroke(library: SignLibrary, obj: XtfObject) -> Stroke:
    """Build a `Stroke` from a real `PolylineSign` data object's `Color` and `Style` (width/join/cap/dash pattern).

    `Style` (`PolylineSignLineStyleAssoc`) resolves to a `LineStyle_Solid`/
    `_Dashed` object; its own `LineAttrs` association
    (`LineStyle_SolidPolylineAttrsAssoc`/`_DashedLineAttrsAssoc`) gives
    `Width`/`Join`/`Caps`, and a `LineStyle_Dashed` additionally gives its
    own `Dashes` (`LIST OF DashRec`) as the dash pattern. Same `Color`
    wire mechanism as `symbol_sign_object_to_marker` (verified there)
    applied to `PolylineSign.Color`.
    """
    color, opacity = library.resolve_color(obj, "Color")
    width = join = cap = dashes = None
    style_obj = library.resolve_ref(obj, "Style")
    if style_obj is not None:
        attrs_obj = library.resolve_ref(style_obj, "LineAttrs")
        if attrs_obj is not None:
            width_text = library.scalar(attrs_obj, "Width")
            width = float(width_text) if width_text is not None else None
            join = library.scalar(attrs_obj, "Join")
            cap = library.scalar(attrs_obj, "Caps")
        if style_obj.qualified_class.endswith("LineStyle_Dashed"):
            dashes = library.dash_pattern(style_obj)
    return polyline_sign_to_stroke(color=color, opacity=opacity, width=width, join=join, cap=cap, dash_pattern=dashes)


def _symbol_sign_kwargs(library: SignLibrary, obj: XtfObject) -> dict[str, Any]:
    return {"marker": symbol_sign_object_to_marker(library, obj)}


def _polyline_sign_kwargs(library: SignLibrary, obj: XtfObject) -> dict[str, Any]:
    return {"stroke": polyline_sign_object_to_stroke(library, obj)}


def _surface_sign_kwargs(library: SignLibrary, obj: XtfObject) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"fill": surface_sign_object_to_fill(library, obj)}
    stroke = surface_sign_object_border_to_stroke(library, obj)
    if stroke is not None:
        kwargs["stroke"] = stroke
    return kwargs


_SIGN_OBJECT_BUILDERS = {
    _MARKER_SIGN_CLASS: _symbol_sign_kwargs,
    _FILL_SIGN_CLASS: _surface_sign_kwargs,
    _STROKE_SIGN_CLASS: _polyline_sign_kwargs,
}


def _with_feature_type(selector: dict[str, Any] | None, feature_type: str | None) -> dict[str, Any] | None:
    """Prepend a `dataLayer.id` conjunct - pycartosym's own writer pulls this back out as `se:FeatureTypeName`."""
    if feature_type is None:
        return selector
    conjunct = {"op": "=", "args": [{"sysId": "dataLayer.id"}, feature_type]}
    return conjunct if selector is None else {"op": "and", "args": [conjunct, selector]}


def styling_rule_from_drawing_rule(
    drawing_rule: MetaInstance, sign_library: SignLibrary | None = None, feature_type: str | None = None
) -> StylingRule:
    """Build one pycartosym `StylingRule` from a built INTERLIS `DrawingRule`.

    The `WHERE` selector compiles via `cql2.to_cql2` (the same CQL2-JSON
    shape `StylingRule.selector` expects natively). `feature_type`
    (typically the enclosing `Graphic.Base.Name` - `DrawingRule` has no
    back-reference to it, so the caller must pass it) is prepended as a
    `dataLayer.id` conjunct, pycartosym's own convention for `se:
    FeatureTypeName` (confirmed against its writer source). `Priority` ->
    `z_order`. A `Sign := {name}` reference resolves (via `sign_library`,
    a parsed SIGN BASKET data section - `None` skips it entirely, same as
    before this was wired) to its own library-object data, dispatched by
    `drawing_rule.Class` to `_symbol_sign_kwargs`/`_surface_sign_kwargs`/
    `_polyline_sign_kwargs` - those hold color/width/symbol/border; a
    `TextSign`'s `Txt`/`Rotation`/`HAli`/`VAli` are genuine `PARAMETER`s
    (`AbstractSymbology.Signs.TextSign`) set directly on the
    `DrawingRule`, unlike `Height`/`Font` which are OWN attributes only
    ever set via the referenced Sign object - resolved via
    `text_sign_object_to_font_kwargs` when `sign_object` is present (not
    itself corpus-verified, no real `TextSign` data object found).

    A `DrawingRule` with more than one `CondSignParamAssignment` (several
    independent `WHERE (...)` blocks under the same rule name, which would
    need `StylingRule.nested_rules`) has no real corpus example - only the
    first is used here (RULE #7).
    """
    conditions = drawing_rule.Rule if isinstance(drawing_rule.Rule, list) else [drawing_rule.Rule]
    cond = conditions[0]
    where = getattr(cond, "Where", None)
    selector = _with_feature_type(to_cql2(where) if where is not None else None, feature_type)

    raw_assignments = cond.Assignments if isinstance(cond.Assignments, list) else [cond.Assignments]
    params: dict[str, Any] = {}
    sign_object: XtfObject | None = None
    for assignment in raw_assignments:
        if assignment.Param == "Geometry":
            continue  # which attribute carries the geometry, not a Symbolizer field
        if assignment.Param == "Sign":
            target = assignment.Assignment
            name = getattr(target, "Name", None) if isinstance(target, MetaInstance) else None
            if sign_library is not None and name is not None:
                sign_object = sign_library.by_name.get(name)
            continue
        params[assignment.Param] = to_cql2(assignment.Assignment)

    symbolizer_kwargs: dict[str, Any] = {}
    if "Priority" in params:
        symbolizer_kwargs["z_order"] = symbolizer_z_order(params["Priority"])
    sign_class = getattr(getattr(drawing_rule, "Class", None), "Name", None)
    if sign_object is not None and sign_library is not None and sign_class in _SIGN_OBJECT_BUILDERS:
        symbolizer_kwargs.update(_SIGN_OBJECT_BUILDERS[sign_class](sign_library, sign_object))
    if sign_class == _TEXT_SIGN_CLASS and "Txt" in params:
        font_kwargs = (
            text_sign_object_to_font_kwargs(sign_library, sign_object)
            if sign_object is not None and sign_library is not None
            else {}
        )
        symbolizer_kwargs["label"] = text_sign_to_label(
            text=params.get("Txt"),
            rotation=params.get("Rotation"),
            h_alignment=params.get("HAli"),
            v_alignment=params.get("VAli"),
            **font_kwargs,
        )

    return StylingRule(
        name=getattr(drawing_rule, "Name", None),
        selector=selector,
        symbolizer=Symbolizer(**symbolizer_kwargs),
    )


def write_sld(style: Style) -> str:
    """Write `style` to SLD, working around pycartosym's SLD writer not yet supporting attribute-driven text rotation.

    `Transform2D.orientation` accepts a `PropertyRef` at the pycartosym
    model level, but the SLD writer's angle formatter raises
    `NotImplementedError` for anything but a literal number (confirmed
    empirically - real corpus data, e.g. `RoadsExgm2ien.ili`'s `Rotation
    := NamOri`, needs exactly the dynamic form). Reported to the
    pycartosym maintainer - remove this workaround once fixed there.
    Strips any `PropertyRef`-driven text rotation before handing `style`
    to the real writer, then patches the resulting XML to add it back as
    `se:Rotation><ogc:PropertyName>`, the same shape pycartosym already
    writes for other dynamic fields (`stroke-width`, `fill`, ...).
    """
    pending: dict[str, str] = {}
    patched_rules = []
    for rule in style.styling_rules:
        label = rule.symbolizer.label if rule.symbolizer else None
        orientation = None
        if label is not None and label.elements:
            transform = label.elements[0].transform
            orientation = transform.orientation if transform is not None else None
        if isinstance(orientation, PropertyRef) and rule.name and label is not None:
            pending[rule.name] = orientation.property
            new_graphic = label.elements[0].model_copy(update={"transform": None})
            new_label = label.model_copy(update={"elements": [new_graphic, *label.elements[1:]]})
            new_symbolizer = rule.symbolizer.model_copy(update={"label": new_label})
            rule = rule.model_copy(update={"symbolizer": new_symbolizer})
        patched_rules.append(rule)

    raw = get_codec("sld").write(style.model_copy(update={"styling_rules": patched_rules}))
    xml = raw if isinstance(raw, str) else raw.decode()
    return _inject_dynamic_text_rotations(xml, pending) if pending else xml


def _inject_dynamic_text_rotations(sld_xml: str, rotations: dict[str, str]) -> str:
    root = etree.fromstring(sld_xml.encode("utf-8"))
    for rule_el in root.iter(f"{{{_SE_NS}}}Rule"):
        title_el = rule_el.find(f"{{{_SE_NS}}}Description/{{{_SE_NS}}}Title")
        attr_name = rotations.get(title_el.text) if title_el is not None else None
        if attr_name is None:
            continue
        text_symbolizer = rule_el.find(f"{{{_SE_NS}}}TextSymbolizer")
        if text_symbolizer is None:
            continue
        placement = text_symbolizer.find(f"{{{_SE_NS}}}LabelPlacement")
        if placement is None:
            placement = etree.SubElement(text_symbolizer, f"{{{_SE_NS}}}LabelPlacement")
        point_placement = placement.find(f"{{{_SE_NS}}}PointPlacement")
        if point_placement is None:
            point_placement = etree.SubElement(placement, f"{{{_SE_NS}}}PointPlacement")
        # SE 1.1.0 PointPlacementType sequence: AnchorPoint?, Displacement?,
        # Rotation? - always appended last, after any AnchorPoint pycartosym
        # itself already wrote for HAli/VAli.
        rotation_el = etree.SubElement(point_placement, f"{{{_SE_NS}}}Rotation")
        etree.SubElement(rotation_el, f"{{{_OGC_NS}}}PropertyName").text = attr_name
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8").decode()
