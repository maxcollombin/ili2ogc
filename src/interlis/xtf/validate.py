"""XTF validator: cross-references a parsed transfer with the resolved schema.

Cross-references an already-parsed XtfTransfer (parse.py, structural
layer) with the schema resolved by InterlisModelBuilder (schema.py) to
produce a list of issues (structure/MANDATORY/base type/reference).

Coverage: structural correctness (unknown class/attribute), MANDATORY,
base types (TEXT/NUMERIC/ENUM), TID/REF reference resolution (including
the legitimate-external-reference vs broken-reference distinction),
embedded association roles as pseudo-attributes, class compatibility of a
resolved reference, the 3rd XTF encoding form
(`CLASS RESTRICTION(A; B; C)`), geometry/coordinates
(COORD/POLYLINE/SURFACE/AREA/MULTI*), association roles defined in an
imported model, numeric Min/Max rounding tolerance, and recursive
validation of STRUCTURE/BAG/LIST content. Not yet covered: attributes
inherited via EXTENDS from an unloaded imported model, the internal
structure of a custom LINE FORM segment (its presence is surfaced as an
`info` issue rather than silently dropped - no confirmed real-world tag
encoding exists to validate its content against), and a few geometry
variants absent from the current XTF inventory. Full detail, severity
rationale, and real-corpus evidence for each item:
docs/dev-notes/xtf-validator-scope.md.
"""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfObject, XtfTransfer
from interlis.xtf.schema import (
    ResolvedAttribute, coord_axes, enum_values, home_symbol_table, is_class_compatible, line_coord_type,
    reference_external_status, reference_target_class, resolve_attribute, resolve_class, restriction_candidates,
    schema_members_of, single_own_attribute,
)

# Concrete Type classes recognized as "reference to an object" (structural
# value expected: REF to a TID/OID, not a literal value) - ReferenceType
# (REFERENCE TO ...) and Class (restrictedStructureRef/
# restrictedClassOrAssRef resolving DIRECTLY to a Class[Kind=Class|
# Structure], see spec/grammar/mapping/04_attributes.yml).
_REFERENCE_TYPE_KINDS = {"ReferenceType", "Class"}


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning" | "info"
    basket_bid: str
    object_tid: str | None
    qualified_class: str
    attribute: str | None
    message: str


def _extract_reference(node: RawNode) -> str | None:
    """Find a reference attribute's target TID/OID, in one of 3 known forms.

    See docs/xtf-transfer-encoding-notes.md:
    - spec form (canonical INTERLIS 2.4): an `ili:ref` XML attribute
      directly on the node - the namespace isn't stripped by the
      structural parser (RawNode keeps `elem.attrib` as-is), so this
      searches by key SUFFIX.
    - real form, plain reference attribute (ili2fme, INTERLIS 2.3): a
      single descendant carrying a `REF` attribute (uppercase, no
      namespace prefix), nested in an intermediate element named after the
      qualified role.
    - real form, embedded association role (confirmed on
      `rMeasurementLocation`): a `REF` attribute (uppercase) directly on
      the role's own node, with no nested element - already covered by
      the same `"REF" in node.attrib` fallback as the previous form, the
      recursion over empty children (`node.children == []`) simply doing
      nothing before reaching that fallback.
    """
    for key, val in node.attrib.items():
        if key.rsplit("}", 1)[-1] == "ref":
            return val
    for child in node.children:
        found = _extract_reference(child)
        if found is not None:
            return found
    if "REF" in node.attrib:
        return node.attrib["REF"]
    return None


def _decimal_places(raw: str) -> int:
    """Return the number of digits after the decimal point in `raw` (0 if none).

    Used to derive the domain's PRECISION directly from Min/Max's textual
    form (e.g. '850000.000' -> 3), preserved as-is from the source .ili
    file (confirmed in `tests/test_numeric_domain_min_max.py`: `0.000 ..
    850000.000` -> `Min='0.000'`/`Max='850000.000'`, no normalization) -
    the IlisMeta16 metamodel has NO dedicated Precision/decimals attribute
    on NumType (own: {Min, Max, Circular, Clockwise} only, confirmed in
    `mappings/ilismeta16-classes.yml`), so this is the ONLY signal
    available.
    """
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def _numeric_range_tolerance(min_raw, max_raw) -> float:
    """Return the rounding tolerance for a numeric range, per eCH-0031 V2.1.0.

    §2.8 "Umgang mit Rundung von numerischen Werten und Koordinaten":
    "Numerische Werte werden [...] im INTERLIS2-Transfer gemaess der
    Wertebereichsdefinition [...] dargestellt. Konsistenzbedingungen
    muessen mit den gerundeten Werten (auf- oder abgerundet) eingehalten
    sein. [...] Pruefprogramme muessen also bei ihrer Pruefung auf- und
    abgerundete Werte als in Ordnung taxieren." / §4.3.11.4 "Codierung von
    numerischen Datentypen": "Sie [numerische Werte] koennen mit hoeherer
    Genauigkeit transferiert werden als durch den Wertebereich verlangt.
    [...] Damit kann z.B. 100 (bei einem angenommenen Wertebereich von
    0..999) als 100, 100.0000001, 10.0e1 oder 1.0e2 uebertragen werden."

    A sender may therefore transfer MORE decimals than the domain defines
    - all that matters is whether the value, once rounded to the domain's
    precision (deduced from Min/Max via `_decimal_places`), stays in
    range. Tolerance = half a unit of the smallest representable decimal
    (0.5 * 10^-decimals): a value sitting EXACTLY that far from a bound
    still rounds inside it (standard rounding, symmetric on both bounds -
    "auf- oder abgerundet" favors neither direction).
    """
    decimals = 0
    for raw in (min_raw, max_raw):
        if raw is not None:
            decimals = max(decimals, _decimal_places(str(raw)))
    return 0.5 * (10 ** -decimals)


def _validate_scalar(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> list[str]:
    """Run base-type checks on ONE present attribute node.

    Returns a list of error messages (empty if compliant, or if the Type
    doesn't lend itself to any check known to this batch).
    """
    problems: list[str] = []
    kind = resolved.type_kind
    if kind == "NumType":
        text = node.text
        if text is None:
            return problems
        try:
            numeric_value = float(text)
        except ValueError:
            return [f"{ctx}: valeur {text!r} non numerique (Type=NumType)"]
        min_raw = getattr(resolved.type_instance, "Min", None)
        max_raw = getattr(resolved.type_instance, "Max", None)
        try:
            tolerance = _numeric_range_tolerance(min_raw, max_raw)
            if min_raw is not None and numeric_value < float(min_raw) - tolerance:
                problems.append(f"{ctx}: valeur {text!r} < Min {min_raw!r}")
            if max_raw is not None and numeric_value > float(max_raw) + tolerance:
                problems.append(f"{ctx}: valeur {text!r} > Max {max_raw!r}")
        except ValueError:
            pass  # Non-numeric Min/Max (e.g. a predefined domain) - out of scope here
    elif kind == "EnumType" and resolved.type_instance is not None:
        text = node.text
        if text is None:
            return problems
        allowed = enum_values(resolved.type_instance)
        # "OTHERS" is always valid (eCH-0031 V2.1.0 §4.3.11.3:
        # "EnumValue = (EnumElement-Name {'.' EnumElement-Name}) | 'OTHERS'.")
        if allowed and text != "OTHERS" and text not in allowed:
            problems.append(f"{ctx}: value {text!r} not in the enumeration ({sorted(allowed)!r})")
    elif kind == "TextType":
        if node.text is None and not node.children:
            problems.append(f"{ctx}: TEXT attribute present but empty")
    return problems


# --- Geometry/coordinates ----------------------------------------------------
#
# Direct citation, eCH-0031 V2.1.0:
# §4.3.11.13 "Codierung von Koordinaten": "CoordValue = <geom:coord>
#   <geom:c1>NumericConst</geom:c1> <geom:c2>NumericConst</geom:c2>
#   [<geom:c3>NumericConst</geom:c3>] </geom:coord>." / "MultiCoordValue =
#   <geom:multicoord> (* CoordValue *) </geom:multicoord>."
# §4.3.11.14 "Codierung von Linienzuegen": "PolylineValue = <geom:polyline>
#   SegmentSequence </geom:polyline>." with SegmentSequence = StartSegment
#   (CoordValue) (* StraightSegment (CoordValue) | ArcSegment | LineFormSegment *).
#   ArcSegment = <geom:arc> <geom:c1>..<geom:c2>..[<geom:c3>..] <geom:a1>..
#   <geom:a2>.. [<geom:r>..] </geom:arc>. "MultiPolylineValue =
#   <geom:multipolyline> (* PolylineValue *) </geom:multipolyline>."
# §4.3.11.15 "Codierung von Einzelflaechen...": "SurfaceValue = <geom:surface>
#   Boundaries </geom:surface>." Boundaries = OuterBoundary {InnerBoundary}.
#   "MultiSurfaceValue = <geom:multisurface> (* SurfaceValue *) </geom:multisurface>."
#   Same structure for AREA (the manual states explicitly "SURFACE und AREA
#   werden wie folgt codiert" - one single rule set for both Kinds).
#
# Real corpus evidence (xtf_corpus/*.xtf): no "geom:"
# namespace (like ili:ref/REF, already documented for references in
# docs/xtf-transfer-encoding-notes.md), and UPPERCASE, an exact mirror of
# the grammar keyword (COORD/POLYLINE/SURFACE, same convention already
# confirmed for REFERENCE/REF): `<AttrName><COORD><C1>x</C1><C2>y</C2>[<C3>z</C3>]</COORD>
# </AttrName>` (RoadTrafficAccidentLocations.xtf, 3D); `<AttrName><SURFACE>
# <BOUNDARY><POLYLINE><COORD>...</COORD>...</POLYLINE></BOUNDARY>[<BOUNDARY>
# ...]</SURFACE></AttrName>` (alpenkonvention_2056.xtf, outer boundary THEN
# inner boundary/ies, with no tag distinguishing them - "OuterBoundary"/
# "InnerBoundary" from the abstract grammar share the SAME concrete tag
# BOUNDARY, only the ORDER - first = outer - carries the information, per
# the manual: "Der erste Rand einer Flaeche (OuterBoundary) ist der aeussere
# Rand"). No real example of MULTICOORD/MULTIPOLYLINE/MULTISURFACE/
# MULTIAREA/AREA/ARC/a custom LINE FORM exists in the 12-file inventory -
# these forms are implemented by direct extrapolation from the manual plus
# the UPPERCASE=keyword convention already confirmed twice (COORD/POLYLINE),
# documented as extrapolated rather than corpus-confirmed. A custom LINE
# FORM segment (an arbitrary structure, neither COORD nor ARC) is not
# structurally interpreted (no real candidate in the inventory to confirm
# its tag encoding against) - but unlike before, its presence is no longer
# silently dropped: `_validate_line_attribute` surfaces it as an `info`
# issue (see `_custom_line_form_tags`), so a transfer using one is never
# reported as "0 problems" while part of its geometry went unchecked.


def _find_child(node: RawNode, tag: str) -> RawNode | None:
    return next((c for c in node.children if c.tag == tag), None)


def _numeric_problems(text: str | None, min_raw, max_raw, ctx: str) -> list[str]:
    """Run the same checks as `_validate_scalar`'s NumType branch.

    Textual Min/Max, rounding tolerance via `_numeric_range_tolerance` -
    factored out here for reuse on coordinate components (C1/C2/C3/A1/A2/R)
    - `min_raw`/`max_raw` are `None` for a component whose axis/bound
    isn't known (PARSEABILITY check only, never a range check) - see
    schema.coord_axes.
    """
    if text is None:
        return [f"{ctx}: composante absente"]
    try:
        value = float(text)
    except ValueError:
        return [f"{ctx}: valeur {text!r} non numerique"]
    problems: list[str] = []
    try:
        tolerance = _numeric_range_tolerance(min_raw, max_raw)
        if min_raw is not None and value < float(min_raw) - tolerance:
            problems.append(f"{ctx}: valeur {text!r} < Min {min_raw!r}")
        if max_raw is not None and value > float(max_raw) + tolerance:
            problems.append(f"{ctx}: valeur {text!r} > Max {max_raw!r}")
    except ValueError:
        pass
    return problems


def _axis_components(node: RawNode, prefix: str) -> list[RawNode]:
    """Return `{prefix}1`, `{prefix}2`, ... components present on `node`.

    In order, up to the first gap (e.g. prefix="C" -> C1/C2/[C3] of a
    COORD/ARC; prefix="A" -> A1/A2 of an ARC's intermediate point).
    """
    out: list[RawNode] = []
    i = 1
    while True:
        child = _find_child(node, f"{prefix}{i}")
        if child is None:
            break
        out.append(child)
        i += 1
    return out


def _validate_axis_values(components: list[RawNode], axes: list[MetaInstance], ctx: str, *, label: str) -> list[str]:
    """Check each component against its CORRESPONDING axis (by position).

    `AxisSpec.Axis` is ORDERED (confirmed in
    ilismeta16-associations.yml) - Min/Max range check if `axes` is known
    (schema.coord_axes non-empty), numeric PARSEABILITY only otherwise.
    Reports a CARDINALITY mismatch (component count differs from declared
    axis count) ONLY when `axes` is known - a count mismatch is only a
    reliable signal if the expected count is too.
    """
    problems: list[str] = []
    if axes and len(components) != len(axes):
        problems.append(f"{ctx}: {len(components)} composante(s) {label}, {len(axes)} attendue(s) (CoordType.Axis)")
    for i, comp in enumerate(components):
        axis = axes[i] if i < len(axes) else None
        problems.extend(_numeric_problems(
            comp.text, getattr(axis, "Min", None), getattr(axis, "Max", None), f"{ctx}.{label}{i + 1}",
        ))
    return problems


def _validate_coord_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "COORD":
        return [f"{ctx}: geometrie COORD attendue, balise {node.tag!r} trouvee"]
    return _validate_axis_values(_axis_components(node, "C"), axes, ctx, label="C")


def _validate_arc_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    """Validate an ArcSegment (eCH-0031 V2.1.0 §4.3.11.14).

    The intermediate point A1/A2 is checked against the 2 FIRST axes (same
    X/Y components as a COORD, never a 3rd intermediate component for an
    arc - confirmed by the grammar: geom:a1/geom:a2 only, no geom:a3). R
    (radius, optional): numeric PARSEABILITY only, never a range - no axis
    covers it (it's a derived length, not a coordinate).
    """
    if node.tag != "ARC":
        return [f"{ctx}: geometrie ARC attendue, balise {node.tag!r} trouvee"]
    problems = _validate_axis_values(_axis_components(node, "C"), axes, ctx, label="C")
    mid = _axis_components(node, "A")
    if len(mid) < 2:
        problems.append(f"{ctx}: point intermediaire A1/A2 absent (ARC)")
    else:
        problems.extend(_validate_axis_values(mid, axes[:2], ctx, label="A"))
    r_node = _find_child(node, "R")
    if r_node is not None:
        problems.extend(_numeric_problems(r_node.text, None, None, f"{ctx}.R"))
    return problems


def _validate_polyline_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "POLYLINE":
        return [f"{ctx}: expected POLYLINE geometry, found tag {node.tag!r}"]
    if not node.children:
        return [f"{ctx}: empty POLYLINE (no segment)"]
    problems: list[str] = []
    for i, seg in enumerate(node.children):
        seg_ctx = f"{ctx}[{i}]"
        if seg.tag == "COORD":
            problems.extend(_validate_coord_node(seg, axes, seg_ctx))
        elif seg.tag == "ARC":
            problems.extend(_validate_arc_node(seg, axes, seg_ctx))
        # else: a custom LINE FORM segment (arbitrary structure, other than
        # STRAIGHTS/ARCS) - not structurally interpreted (no real candidate
        # in the XTF inventory, see module docstring), not flagged as an
        # error here (would be a false structural alarm); its presence is
        # surfaced separately as an `info` issue by `_line_form_infos`,
        # called once from `_validate_line_attribute`.
    return problems


def _validate_boundary_node(node: RawNode, axes: list[MetaInstance], ctx: str) -> list[str]:
    if node.tag != "BOUNDARY":
        return [f"{ctx}: expected BOUNDARY geometry, found tag {node.tag!r}"]
    polyline = _find_child(node, "POLYLINE")
    if polyline is None:
        return [f"{ctx}: BOUNDARY without POLYLINE"]
    return _validate_polyline_node(polyline, axes, f"{ctx}/POLYLINE")


def _validate_surface_node(node: RawNode, expected_tag: str, axes: list[MetaInstance], ctx: str) -> list[str]:
    """Validate a SURFACE or AREA node (same Boundaries structure for both).

    `expected_tag` = "SURFACE" or "AREA".
    """
    if node.tag != expected_tag:
        return [f"{ctx}: expected {expected_tag} geometry, found tag {node.tag!r}"]
    boundaries = [c for c in node.children if c.tag == "BOUNDARY"]
    if not boundaries:
        return [f"{ctx}: {expected_tag} without any BOUNDARY"]
    problems: list[str] = []
    for i, boundary in enumerate(boundaries):
        problems.extend(_validate_boundary_node(boundary, axes, f"{ctx}[{i}]"))
    return problems


def _validate_coord_attribute(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> list[str]:
    """Validate an attribute whose Type resolves to CoordType.

    COORD/MULTICOORD, eCH-0031 V2.1.0 §4.3.11.13 - `node` is the
    ATTRIBUTE's own node (e.g. `<AccidentLocation>`), its 1st child must
    be COORD (or MULTICOORD if `CoordType.Multi`).
    """
    coord_type = resolved.type_instance
    multi = bool(getattr(coord_type, "Multi", False))
    axes = coord_axes(coord_type)
    expected_tag = "MULTICOORD" if multi else "COORD"
    child = node.children[0] if node.children else None
    if child is None or child.tag != expected_tag:
        found = child.tag if child is not None else "(empty)"
        return [f"{ctx}: expected {expected_tag} geometry (Type=CoordType, Multi={multi}), found {found!r}"]
    if not multi:
        return _validate_coord_node(child, axes, ctx)
    coords = [c for c in child.children if c.tag == "COORD"]
    if not coords:
        return [f"{ctx}: MULTICOORD without any inner COORD"]
    problems: list[str] = []
    for i, c in enumerate(coords):
        problems.extend(_validate_coord_node(c, axes, f"{ctx}[{i}]"))
    return problems


_LINE_KIND_TAGS = {"Polyline": "POLYLINE", "DirectedPolyline": "POLYLINE", "Surface": "SURFACE", "Area": "AREA"}
_LINE_KIND_MULTI_TAGS = {
    "Polyline": "MULTIPOLYLINE", "DirectedPolyline": "MULTIPOLYLINE", "Surface": "MULTISURFACE", "Area": "MULTIAREA",
}


def _custom_line_form_tags(node: RawNode) -> set[str]:
    """Recursively collect tags of POLYLINE segments that are neither COORD nor ARC.

    eCH-0031 V2.1.0 §4.3.11.14's SegmentSequence has a 3rd alternative
    (LineFormSegment, a custom form declared via `lineFormTypeDef`)
    besides StraightSegment/ArcSegment - walks through any nesting
    (BOUNDARY/SURFACE/AREA/MULTI*) to find every POLYLINE and report the
    tags of its non-COORD/ARC segments, regardless of depth.
    """
    tags: set[str] = set()
    if node.tag == "POLYLINE":
        tags.update(seg.tag for seg in node.children if seg.tag not in ("COORD", "ARC"))
    for child in node.children:
        tags |= _custom_line_form_tags(child)
    return tags


def _line_form_infos(node: RawNode, ctx: str) -> list[str]:
    """Surface custom LINE FORM segments as `info` rather than silently dropping them.

    Their internal structure isn't validated (see module docstring - no
    confirmed real-world tag encoding in the corpus to check content
    against), but their presence must still show up in the report so a
    transfer using one is never mistaken for fully-validated geometry.
    """
    tags = _custom_line_form_tags(node)
    if not tags:
        return []
    return [f"{ctx}: custom LINE FORM segment(s) present ({', '.join(sorted(tags))}) - structure not validated by this tool"]


def _validate_line_attribute(resolved: ResolvedAttribute, node: RawNode, ctx: str) -> tuple[list[str], list[str]]:
    """Validate an attribute whose Type resolves to LineType.

    POLYLINE/SURFACE/AREA/MULTI*, eCH-0031 V2.1.0 §4.3.11.14/.15. `axes`
    comes from `schema.line_coord_type` (LineCoord association - EMPTY,
    numeric check only with no range, if the VERTEX clause is
    absent/unresolved, e.g. `DirectedLine EXTENDS Line = DIRECTED
    POLYLINE;` with no VERTEX of its own - a documented limitation).

    Returns `(errors, infos)` - `infos` flags custom LINE FORM segments
    (see `_line_form_infos`) found anywhere in the geometry.
    """
    line_type = resolved.type_instance
    kind = getattr(line_type, "Kind", None)
    multi = bool(getattr(line_type, "Multi", False))
    axes = coord_axes(line_coord_type(line_type))
    single_tag = _LINE_KIND_TAGS.get(kind)
    if single_tag is None:
        return [], []  # Unresolved/unexpected Kind - nothing reliable to check
    expected_tag = _LINE_KIND_MULTI_TAGS[kind] if multi else single_tag
    child = node.children[0] if node.children else None
    if child is None or child.tag != expected_tag:
        found = child.tag if child is not None else "(empty)"
        return [f"{ctx}: expected {expected_tag} geometry (LineType Kind={kind!r}, Multi={multi}), found {found!r}"], []
    infos = _line_form_infos(child, ctx)
    validator = _validate_polyline_node if single_tag == "POLYLINE" else (
        lambda n, ax, c: _validate_surface_node(n, single_tag, ax, c)
    )
    if not multi:
        return validator(child, axes, ctx), infos
    parts = [c for c in child.children if c.tag == single_tag]
    if not parts:
        return [f"{ctx}: {expected_tag} without any inner {single_tag}"], infos
    errors: list[str] = []
    for i, part in enumerate(parts):
        errors.extend(validator(part, axes, f"{ctx}[{i}]"))
    return errors, infos


_GENERIC_RESTRICTION_INFO = (
    "attribut de type reference/structure sans REF reconnu (valeur texte nue, "
    "probable structure a 1 attribut - voir docs/xtf-transfer-encoding-notes.md)"
)


def _validate_restriction_text(
    resolved: ResolvedAttribute, node: RawNode | None, *, basket_bid: str, tid: str | None, qualified_class: str,
    path: str, ctx: str,
) -> "ValidationIssue | None":
    """Interpret the 3rd XTF encoding form: `CLASS RESTRICTION(A; B; C)`.

    See docs/xtf-transfer-encoding-notes.md "Third form found". An
    attribute whose Type resolves to `ReferenceType` with SEVERAL
    `BaseClass` candidates (`CLASS RESTRICTION(A; B; C)`,
    `restriction_candidates`), each candidate itself a 1-own-attribute
    STRUCTURE (`single_own_attribute`) - ili2fme appears to transfer this
    as that single attribute's bare TEXT value, with no `<Reference>`
    wrapper, not even a structure wrapper. Validates `node.text` against
    EVERY candidate whose inner type is verifiable (`_validate_scalar` on
    TextType/NumType/EnumType):
    - at least one candidate accepts the value with no problem -> no issue
      (like a resolved REF).
    - no verifiable candidate (BaseClass absent, no 1-attribute pattern,
      or the pattern is present but the inner type(s) don't resolve -
      e.g. an external domain not loaded via `--repo`, CHAdminCodes_V1 on
      this corpus) -> `info`, a genuinely undetermined status - IDENTICAL
      to the message used when no structural candidate was even found.
    - at least one verifiable candidate exists but NONE accepts the value
      -> `warning` (not `error`: choosing the "right" candidate among
      several stays a structural heuristic, unlike exact TID/REF
      resolution - never upgrade a heuristic interpretation to certainty).
    """
    if node is None:
        return ValidationIssue("info", basket_bid, tid, qualified_class, path, f"{ctx}: {_GENERIC_RESTRICTION_INFO}")
    single_attr_candidates = [
        (candidate, resolve_attribute(attr))
        for candidate in restriction_candidates(resolved)
        if (attr := single_own_attribute(candidate)) is not None
    ]
    if not single_attr_candidates:
        return ValidationIssue("info", basket_bid, tid, qualified_class, path, f"{ctx}: {_GENERIC_RESTRICTION_INFO}")
    checkable = [(c, ir) for c, ir in single_attr_candidates if ir.type_kind in ("TextType", "NumType", "EnumType")]
    if any(not _validate_scalar(inner, node, ctx) for _, inner in checkable):
        return None
    if len(checkable) < len(single_attr_candidates):
        return ValidationIssue(
            "info", basket_bid, tid, qualified_class, path,
            f"{ctx}: bare text value {node.text!r} (CLASS RESTRICTION, 3rd encoding form) matches "
            f"none of the {len(checkable)}/{len(single_attr_candidates)} verifiable candidate(s) - the "
            "rest are unresolved (external model not loaded via --repo)",
        )
    names = [getattr(c, "Name", None) for c, _ in checkable]
    return ValidationIssue(
        "warning", basket_bid, tid, qualified_class, path,
        f"{ctx}: bare text value {node.text!r} (CLASS RESTRICTION, 3rd encoding form) matches "
        f"none of the {len(checkable)} declared candidate(s) ({names!r})",
    )


def _build_tid_index(transfer: XtfTransfer, catalogs: list[XtfTransfer] | None = None) -> dict[str, XtfObject]:
    """Map TID -> XtfObject, across every basket of the transfer.

    A reference can target an object in a different basket of the same
    file (confirmed real on
    wohnungsinventar-zweitwohnungsanteil_2019-10_2056.xtf, 2 baskets),
    THEN across every basket of each given catalogue transfer (`--catalog`,
    see cli.py): an EXTERNAL catalogue object (e.g. MLocStatus, eCH-0031
    V2.1.0 §3.6.3) typically lives in a basket/file SEPARATE from the main
    data transfer - this file isn't auto-discovered (no public source
    identified for this corpus, see docs/model-resolution-strategy.md),
    but if the operator provides one, its objects become resolvable just
    like the main transfer's. A TID duplicated across baskets (main
    transfer OR catalogue) would already be an invalid object (each TID
    must be unique within a transfer) - first one found wins, main
    transfer takes priority over catalogues.
    """
    index: dict[str, XtfObject] = {}
    for basket in transfer.baskets:
        for obj in basket.objects:
            if obj.tid is not None:
                index.setdefault(obj.tid, obj)
    for catalog in catalogs or []:
        for basket in catalog.baskets:
            for obj in basket.objects:
                if obj.tid is not None:
                    index.setdefault(obj.tid, obj)
    return index


def _resolved_schema_of(
    cls: MetaInstance, symbol_table: SymbolTable, cache: dict[int, dict[str, ResolvedAttribute]],
) -> dict[str, ResolvedAttribute]:
    """Return `schema_members_of`+`resolve_attribute`, memoized per class.

    Cached by `id(cls)` for the duration of a `validate_transfer`
    (optimization, no behavior change). `embedded_roles_of` (called by
    `schema_members_of`) rescans the WHOLE symbol_table on every call -
    profiled (cProfile, ch.astra.nationalstrassenachsen.xtf, 34533 objects
    but only 4 distinct classes): ~82% of `validate_transfer`'s time was
    spent recomputing the SAME result for each object of an
    already-seen class, even though a class's schema never changes during
    a validation run. Keyed by Python object identity (`id`), not
    qualified name: two different `MetaInstance`s must never share an
    entry, even same-named ones (already handled elsewhere via kind_hint -
    don't reintroduce that ambiguity through a cache shortcut).
    """
    key = id(cls)
    cached = cache.get(key)
    if cached is None:
        cached = {name: resolve_attribute(attr) for name, attr in schema_members_of(cls, symbol_table).items()}
        cache[key] = cached
    return cached


def _group_by_tag(nodes: list[RawNode]) -> dict[str, list[RawNode]]:
    """Group a FLAT list of `RawNode` by tag name.

    E.g. the children of ONE structure occurrence - same convention as
    `XtfObject.attributes` (`xtf/parse.py`), so the per-attribute
    validation logic can be reused AS-IS on nested content.
    """
    grouped: dict[str, list[RawNode]] = {}
    for n in nodes:
        grouped.setdefault(n.tag, []).append(n)
    return grouped


def _validate_object(
    obj: XtfObject, basket: XtfBasket, *, symbol_table: SymbolTable, repository: ModelRepository | None,
    tid_index: dict[str, XtfObject], schema_cache: dict[int, dict[str, ResolvedAttribute]],
) -> list[ValidationIssue]:
    cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
    if cls is None:
        return [ValidationIssue(
            "error", basket.bid, obj.tid, obj.qualified_class, None,
            "class absent from the resolved schema (model not loaded/findable via --repo, or wrong qualified name)",
        )]

    # The table that ACTUALLY declares `cls` can differ from the root
    # table (e.g. a class resolved via ModelRepository, TOPIC EXTENDS of a
    # base model) - needed so embedded_roles_of finds the embedding
    # associations where they're really declared.
    cls_table = home_symbol_table(obj.qualified_class, symbol_table=symbol_table, repository=repository)
    return _validate_attrs(
        cls, obj.attributes, path_prefix="", basket_bid=basket.bid, tid=obj.tid, qualified_class=obj.qualified_class,
        home_table=cls_table, symbol_table=symbol_table, repository=repository, tid_index=tid_index,
        schema_cache=schema_cache,
    )


def _validate_attrs(
    cls: MetaInstance, attrs: dict[str, list[RawNode]], *, path_prefix: str, basket_bid: str, tid: str | None,
    qualified_class: str, home_table: SymbolTable, symbol_table: SymbolTable, repository: ModelRepository | None,
    tid_index: dict[str, XtfObject], schema_cache: dict[int, dict[str, ResolvedAttribute]],
) -> list[ValidationIssue]:
    """Validate every attribute in `attrs` against `cls`'s schema.

    Checks MANDATORY, base type, reference, geometry, etc. - factored out
    so it applies equally to a root `XtfObject` (`path_prefix=""`,
    `cls`=its Class) and to the content of a nested STRUCTURE occurrence
    (`path_prefix`=the parent attribute's path, `cls`=the Structure itself
    - same AttrOrParam/embedded roles via `schema_members_of`, the
    Class/Structure distinction only affects the `Kind` discriminant,
    never the schema's shape). `home_table` stays the one computed for the
    root object, never recomputed per recursion level (the real corpus's
    nested structures are all declared in the SAME model as their
    enclosing class - a deliberate scope limit, to revisit if a real
    cross-model case appears).
    """
    issues: list[ValidationIssue] = []
    schema_attrs = _resolved_schema_of(cls, home_table, schema_cache)
    for attr_name, raw_nodes in attrs.items():
        path = f"{path_prefix}.{attr_name}" if path_prefix else attr_name
        if attr_name not in schema_attrs:
            issues.append(ValidationIssue(
                "warning", basket_bid, tid, qualified_class, path,
                "attribut absent du schema (classe connue) - inconnu, ou herite via EXTENDS depuis un "
                "modele importe non charge (non couvert actuellement)",
            ))
            continue
        resolved = schema_attrs[attr_name]
        ctx = f"{qualified_class}[{tid}].{path}"
        issues.extend(_validate_resolved_attr(
            resolved, raw_nodes, path=path, ctx=ctx, basket_bid=basket_bid, tid=tid, qualified_class=qualified_class,
            home_table=home_table, symbol_table=symbol_table, repository=repository, tid_index=tid_index,
            schema_cache=schema_cache,
        ))

    for attr_name, resolved in schema_attrs.items():
        if resolved.mandatory and attr_name not in attrs:
            path = f"{path_prefix}.{attr_name}" if path_prefix else attr_name
            issues.append(ValidationIssue(
                "error", basket_bid, tid, qualified_class, path,
                f"{qualified_class}[{tid}].{path}: attribut MANDATORY {attr_name!r} absent",
            ))
    return issues


def _validate_resolved_attr(
    resolved: ResolvedAttribute, raw_nodes: list[RawNode], *, path: str, ctx: str, basket_bid: str, tid: str | None,
    qualified_class: str, home_table: SymbolTable, symbol_table: SymbolTable, repository: ModelRepository | None,
    tid_index: dict[str, XtfObject], schema_cache: dict[int, dict[str, ResolvedAttribute]],
    already_unwrapped: bool = False,
) -> list[ValidationIssue]:
    """Dispatch by `type_kind` for ONE already-resolved attribute.

    The core of `_validate_attrs`, extracted so it can be called
    RECURSIVELY: once for a root object's attribute, once per occurrence
    of a `MultiValue` collection (BAG/LIST), and indirectly via
    `_validate_attrs` for a nested STRUCTURE's content.

    `already_unwrapped` distinguishes two forms of `raw_nodes[0]` that
    carry different nesting levels (a bug was found on the real corpus,
    `Facility.Name.LocalisedText[i].Text` falsely flagged "MANDATORY
    absent" 100% of the time): `False` (default, root/"normal" nested
    structure case) - `raw_nodes[0]` is the node NAMED AFTER THE
    ATTRIBUTE (e.g. `<Name>`), whose ONLY child is the structure wrapper
    (`<...MultilingualText>`) - one level of unwrapping is needed. `True`
    (an individual `MultiValue` occurrence, below) - `raw_nodes[0]` IS
    ALREADY that wrapper (e.g. each `<...LocalisedText>` found as a direct
    child of the `<LocalisedText>` container) - unwrapping it a SECOND
    time would group the grandchildren (`<Language>`/`<Text>`... which
    have no children of their own) instead of the real attributes,
    producing FALSE MANDATORY-missing on 100% of occurrences.
    """
    issues: list[ValidationIssue] = []
    kind = resolved.type_kind

    if kind == "MultiValue":
        # `(BAG|LIST) cardinality? OF
        # restrictedStructureRef` (spec/grammar/mapping/04_attributes.yml,
        # attributeDef._collection) builds a `MultiValue` (TypeRelatedType,
        # NOT ClassRelatedType - `BaseType`, not `BaseClass`) - not covered
        # before this addition (used to fall into the generic "unchecked
        # type" fallback below), whether on a ROOT attribute (real example:
        # `KGS_PBC_V2_2.ili`: `Objektart`/`EGID`/`Adressen`) or a nested
        # STRUCTURE's own attribute (`LocalisationCH_V1.
        # MultilingualText.LocalisedText`). Confirmed real XTF encoding
        # (`ID65.1_KGS_PBC_V2_2__20250506.xtf`): a SINGLE element
        # named after the attribute, containing each occurrence as DIRECT
        # CHILDREN (`<Objektart><...Objektarten_CatRef>...</...><...
        # Objektarten_CatRef>...</...></Objektart>`) - NOT repeated
        # occurrences of the attribute tag itself (unlike the
        # `XtfObject.attributes` convention, which captures tag REPETITION
        # at the OBJECT level - two distinct encodings for the same
        # multiplicity concept, each confirmed independently). Each
        # occurrence is re-dispatched as a SIMPLE value of the wrapped type
        # (`BaseType` - Structure, Reference, scalar... same generic
        # mechanism, unlimited recursion) via a synthetic `ResolvedAttribute`.
        base_type = getattr(resolved.type_instance, "BaseType", None)
        if not isinstance(base_type, MetaInstance):
            issues.append(ValidationIssue(
                "info", basket_bid, tid, qualified_class, path,
                f"{ctx}: unchecked 'MultiValue' type (BaseType unresolved - external model not loaded via --repo)",
            ))
            return issues
        base_kind = base_type._qualified_class.rsplit(".", 1)[-1]
        for node in raw_nodes:
            for i, occurrence in enumerate(node.children):
                occ_path = f"{path}[{i}]"
                occ_resolved = ResolvedAttribute(attr=resolved.attr, type_instance=base_type, type_kind=base_kind, mandatory=False)
                issues.extend(_validate_resolved_attr(
                    occ_resolved, [occurrence], path=occ_path, ctx=f"{qualified_class}[{tid}].{occ_path}",
                    basket_bid=basket_bid, tid=tid, qualified_class=qualified_class, home_table=home_table,
                    symbol_table=symbol_table, repository=repository, tid_index=tid_index, schema_cache=schema_cache,
                    already_unwrapped=True,
                ))
        return issues

    if kind in _REFERENCE_TYPE_KINDS:
        # Note: a Type resolved to ReferenceType/Class doesn't
        # always mean a REF/ili:ref in the real XML -
        # `CLASS RESTRICTION(...)` on STRUCTUREs with a SINGLE
        # attribute (a pattern found on RoadTrafficCensus_V1_1:
        # `Owner`/`Canton`, restricted to sCHOwnerCode/sCHCantonCode/...)
        # appears to be transferred by ili2fme as the bare TEXT value of
        # that single attribute, NOT as a reference - a 3rd encoding
        # form, INTERPRETED (see `_validate_restriction_text` below) when
        # possible, otherwise falling back to the same "info" message as
        # before.
        ref = _extract_reference(raw_nodes[0]) if raw_nodes else None
        if ref is None:
            if kind == "Class" and getattr(resolved.type_instance, "Kind", None) == "Structure":
                # `restriction_candidates()` NEVER returns anything for
                # `type_kind == "Class"` (internal guard `!= "ReferenceType"`)
                # - `_validate_restriction_text` could therefore never have
                # done anything here besides the generic fallback. A real
                # STRUCTURE (Kind=Structure, no REF extractable anywhere in
                # its subtree - unlike the MandatoryCatalogueReference
                # pattern already handled above, which DOES carry a REF
                # findable via `_extract_reference`): GENERIC recursion into
                # its own content, EXACTLY the same mechanism as a root
                # object - confirmed responsible for nearly all the
                # remaining unchecked "info" issues before this addition
                # (Name/ModInfo/Point/Surface/Line...).
                for node in raw_nodes:
                    wrapper = node if already_unwrapped else (node.children[0] if node.children else None)
                    child_attrs = _group_by_tag(wrapper.children) if wrapper is not None else {}
                    issues.extend(_validate_attrs(
                        resolved.type_instance, child_attrs, path_prefix=path, basket_bid=basket_bid, tid=tid,
                        qualified_class=qualified_class, home_table=home_table, symbol_table=symbol_table,
                        repository=repository, tid_index=tid_index, schema_cache=schema_cache,
                    ))
                return issues
            restriction_issue = _validate_restriction_text(
                resolved, raw_nodes[0] if raw_nodes else None, basket_bid=basket_bid, tid=tid,
                qualified_class=qualified_class, path=path, ctx=ctx,
            )
            if restriction_issue is not None:
                issues.append(restriction_issue)
        elif ref not in tid_index:
            # REF extracted successfully but NO object of THIS transfer
            # (across all baskets) carries that TID - always `warning`,
            # never `error`: without an external catalogue loaded, a
            # legitimate external reference is indistinguishable from a
            # broken one (see module docstring). The message distinguishes
            # a declared-EXTERNAL reference (eCH-0031 V2.1.0 §3.6.3: without
            # the EXTERNAL clause, the target normally MUST resolve in the
            # SAME basket) from a non-EXTERNAL one, which is a more likely
            # sign of genuinely bad data - severity stays `warning` either
            # way, this only makes the distinction in the message text.
            # `reference_external_status` covers the direct ReferenceType
            # case, the CatalogueObjects_V1 structure-wrapper pattern, and
            # the embedded association role's own EXTERNAL clause - see
            # docs/dev-notes/reference-external-status-investigation.md for
            # the full breakdown. Tri-state: `None` means genuinely
            # undetermined (e.g. a structure wrapping non-reference content
            # like geometry), never asserted as "not declared" by default.
            status = reference_external_status(resolved)
            if status is True:
                detail = (
                    "declared reference (EXTERNAL): target object expected in an external "
                    "basket/catalogue (DEPENDS ON), unresolved for lack of a loaded catalogue - normal"
                )
            elif status is False:
                detail = (
                    "NOT declared as EXTERNAL: should normally resolve in this same "
                    "basket (eCH-0031 V2.1.0 §3.6.3) - a more likely sign of bad data"
                )
            else:
                detail = (
                    "likely external/catalogue reference, or a broken one - EXTERNAL status "
                    "undetermined by this validator (unrecognized structure)"
                )
            issues.append(ValidationIssue(
                "warning", basket_bid, tid, qualified_class, path,
                f"{ctx}: REF {ref!r} not found in this transfer ({detail})",
            ))
        else:
            # REF resolved successfully - now check the actual target
            # object's class compatibility against the class DECLARED by
            # the reference/role. Silent (no issue) if either class can't
            # be established with certainty, or if compatible - never a
            # false positive on cross-model uncertainty.
            declared = reference_target_class(resolved)
            if declared is not None:
                target_obj = tid_index[ref]
                actual_cls = resolve_class(target_obj.qualified_class, symbol_table=symbol_table, repository=repository)
                if actual_cls is not None and not is_class_compatible(actual_cls, declared):
                    issues.append(ValidationIssue(
                        "error", basket_bid, tid, qualified_class, path,
                        f"{ctx}: REF {ref!r} resolved to {target_obj.qualified_class!r}, incompatible "
                        f"with declared class {getattr(declared, 'Name', '?')!r} "
                        "(neither identical nor a subclass via EXTENDS)",
                    ))
        return issues

    if kind == "CoordType":
        # Geometry/coordinates - see the module docstring for the
        # real forms (COORD/MULTICOORD) and the eCH-0031 V2.1.0
        # §4.3.11.13 citation. `error`: COMPLETE information as soon as the
        # Type resolves to CoordType (Multi/Axis always known directly on
        # the instance itself, never a cross-model uncertainty) - same
        # policy as MANDATORY/NUMERIC/ENUM.
        for node in raw_nodes:
            for problem in _validate_coord_attribute(resolved, node, ctx):
                issues.append(ValidationIssue("error", basket_bid, tid, qualified_class, path, problem))
        return issues

    if kind == "LineType":
        # Geometry/coordinates - POLYLINE/SURFACE/AREA/MULTI*,
        # eCH-0031 V2.1.0 §4.3.11.14/.15. `error` for the same reason as
        # CoordType above (structure/Kind/Multi always known) - the only
        # possible uncertainty (per-axis Min/Max range if VERTEX is
        # unresolved, schema.line_coord_type) already degrades gracefully
        # to a parseability-only check, never a full structural skip. A
        # custom LINE FORM segment is reported separately as `info` (its
        # content isn't checked, but its presence must not go unreported).
        for node in raw_nodes:
            errors, infos = _validate_line_attribute(resolved, node, ctx)
            for problem in errors:
                issues.append(ValidationIssue("error", basket_bid, tid, qualified_class, path, problem))
            for note in infos:
                issues.append(ValidationIssue("info", basket_bid, tid, qualified_class, path, note))
        return issues

    if kind not in ("TextType", "NumType", "EnumType"):
        # Type resolved to something other than the kinds handled here
        # (e.g. "FormattedType"/"BooleanType"/"BlackboxType"/"AnyOIDType"
        # - never interpreted, or None - Type never resolved, cf.
        # Municipality/AttrOrParam): made explicitly VISIBLE rather than
        # silently ignored by `_validate_scalar` (which would return an
        # empty list for an unknown `type_kind`) - avoid a "0 problems"
        # total giving a false impression of complete conformance.
        issues.append(ValidationIssue(
            "info", basket_bid, tid, qualified_class, path,
            f"{ctx}: unchecked type {kind!r} (type not covered/unresolved)",
        ))
        return issues

    for node in raw_nodes:
        for problem in _validate_scalar(resolved, node, ctx):
            issues.append(ValidationIssue("error", basket_bid, tid, qualified_class, path, problem))
    return issues


def validate_transfer(
    transfer: XtfTransfer, *, symbol_table: SymbolTable, repository: ModelRepository | None = None,
    catalogs: list[XtfTransfer] | None = None,
) -> list[ValidationIssue]:
    """Validate an XtfTransfer, returning the list of issues found.

    `catalogs` (optional): extra XTF transfers already parsed
    (`parse_xtf`) whose objects should also count as resolvable for
    TID/REF resolution - typically a catalogue-data basket
    (RoadTrafficCensusCatalogues and family) distributed separately from
    the main business data transfer (see `_build_tid_index`).
    """
    tid_index = _build_tid_index(transfer, catalogs)
    schema_cache: dict[int, dict[str, ResolvedAttribute]] = {}
    issues: list[ValidationIssue] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            issues.extend(_validate_object(
                obj, basket, symbol_table=symbol_table, repository=repository, tid_index=tid_index,
                schema_cache=schema_cache,
            ))
    return issues
