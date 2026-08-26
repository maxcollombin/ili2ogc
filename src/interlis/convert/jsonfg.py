"""XTF data instance -> JSON-FG conversion: Feature/FeatureCollection, scalar properties, OID, featureType, geometry.

See docs/jsonfg-conversion-strategy.md for the design decision and scope.
`object_to_feature` converts one already-parsed XtfObject (xtf/parse.py,
structural layer) into a JSON-FG (OGC 21-045r1) Feature object;
`transfer_to_feature_collection` wraps every resolvable object of an
XtfTransfer into one FeatureCollection. Both conform to the "core" and
"types-schemas" requirements classes only - single-attribute point/line/
polygon geometry -> "place" (never "geometry", which stays `null` - no
WGS84 reprojection, see module docs); a plain REFERENCE TO, an embedded
association role, or a 1-own-attribute STRUCTURE wrapping a REFERENCE TO
(all 3 real wire shapes, unified via the SAME `_extract_reference` search
already proven by xtf/validate.py) -> the referenced object's OID as a
plain string; a genuine STRUCTURE (no findable REF) -> a nested JSON
object, `BAG`/`LIST OF X` -> a JSON array, both recursively (same
"resolve schema, dispatch per kind" logic as the root object, mirroring
xtf/validate.py's own `_validate_attrs` recursion). CoordType/LineType
NESTED inside a structure/list element still stays unsupported (no real
corpus DATA evidence for it - the top-level Feature's own "place" is the
only geometry shape built so far). Multi-geometry classes stay out of
scope too (each a separate future lot). Reuses the schema resolution
already proven by
xtf/validate.py (resolve_attribute/attributes_of/coord_axes/
line_coord_type) AND its wire-tag helpers (_geom_tag/_find_child/
_axis_components/the BOUNDARY/SURFACE/LINE_KIND tag sets) rather than a
second parallel implementation of the same COORD/POLYLINE/SURFACE/AREA/
MULTI* wire conventions - convert() stays a decoupled stage from
validate(), same split already established by convert/jsonschema.py.
"""
from typing import Any

from interlis.builder.forward_refs import SymbolTable
from interlis.builder.repository import ModelRepository
from interlis.convert.jsonschema import _is_integer_range
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfObject, XtfTransfer
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    is_class_compatible,
    line_coord_type,
    resolve_attribute,
    resolve_class,
    schema_members_of,
)
from interlis.xtf.validate import (
    _BOUNDARY_TAGS,
    _LINE_KIND_MULTI_TAGS,
    _LINE_KIND_TAGS,
    _REFERENCE_TYPE_KINDS,
    _SURFACE_TAGS,
    _axis_components,
    _extract_reference,
    _find_child,
    _geom_tag,
    _group_by_tag,
)

JSON_FG_VERSION = "1.0"
CONF_CORE = f"http://www.opengis.net/spec/json-fg-1/{JSON_FG_VERSION}/conf/core"
CONF_TYPES_SCHEMAS = f"http://www.opengis.net/spec/json-fg-1/{JSON_FG_VERSION}/conf/types-schemas"
CONF_CIRCULAR_ARCS = f"http://www.opengis.net/spec/json-fg-1/{JSON_FG_VERSION}/conf/circular-arcs"
CRS_URI_PREFIX = "http://www.opengis.net/def/crs/EPSG/0/"

_SCALAR_KINDS = {"NumType", "TextType", "EnumType", "BooleanType"}
_GEOMETRY_KINDS = {"CoordType", "LineType"}
# JSON-FG Part 1 Core §7.5 (conformance class "circular-arcs") - geometry
# "type" values that require CONF_CIRCULAR_ARCS to be declared in
# "conformsTo" (docs/interlis-geometry-sfa-mapping.md).
_CIRCULAR_ARC_TYPES = frozenset({"CircularString", "CompoundCurve", "CurvePolygon", "MultiCurve", "MultiSurface"})


def _scalar_value(resolved: ResolvedAttribute, node: RawNode) -> Any:
    """Read ONE scalar attribute's wire value off its RawNode.

    Same wire conventions already relied on by xtf/validate.py's
    `_validate_scalar` (plain element text, "true"/"false" for BOOLEAN -
    eCH-0031 V2.1.0 SS4.3.11), just producing a value instead of a
    validation verdict. Never raises: a value that doesn't parse as
    expected (e.g. non-numeric text under a NumType) is passed through
    as-is rather than dropped, consistent with this converter's RULE #5
    stance of surfacing rather than hiding.
    """
    text = node.text
    if text is None:
        return None
    kind = resolved.type_kind
    if kind == "NumType":
        # Same integer-vs-number heuristic already used for this SAME
        # attribute's JSON Schema (convert/jsonschema.py) - keeps a
        # Feature's property values consistent with the type its own
        # $defs entry declares, rather than a second independent guess.
        as_int = _is_integer_range(getattr(resolved.type_instance, "Min", None), getattr(resolved.type_instance, "Max", None))
        try:
            return int(text) if as_int else float(text)
        except ValueError:
            return text
    if kind == "BooleanType":
        return text == "true"
    return text  # TextType/EnumType: the wire text itself (EnumType: a dotted path)


def _attribute_value(
    resolved: ResolvedAttribute, raw_nodes: list[RawNode], *,
    symbol_table: SymbolTable | None = None, already_unwrapped: bool = False,
) -> Any:
    kind = resolved.type_kind
    if kind in _SCALAR_KINDS:
        return _scalar_value(resolved, raw_nodes[0])
    if kind == "MultiValue":
        return _multi_value(resolved, raw_nodes, symbol_table=symbol_table)
    if kind in _REFERENCE_TYPE_KINDS:
        # Same dispatch as xtf/validate.py's _validate_resolved_attr: a
        # "ReferenceType"/"Class" kind doesn't always mean a plain
        # REFERENCE TO or an embedded association role - a 1-own-attribute
        # STRUCTURE wrapping a REFERENCE TO (the "MandatoryCatalogueReference"
        # pattern, real corpus example: RoadTrafficAccidentLocation_V2's
        # AccidentType/AccidentSeverityCategory/RoadType/AccidentWeekDay)
        # is ALSO transferred with a findable REF, several levels deep -
        # _extract_reference searches the whole subtree regardless of which
        # of these 3 real wire shapes produced it, so all 3 are handled by
        # this ONE lookup, never 3 separate cases.
        ref = _extract_reference(raw_nodes[0]) if raw_nodes else None
        if ref is not None:
            return ref
        # A GENUINE STRUCTURE occurrence (Kind=Structure, no REF anywhere -
        # real nested content, e.g. MultilingualText) recurses into its own
        # content instead of falling through to the marker below.
        if kind == "Class" and getattr(resolved.type_instance, "Kind", None) == "Structure":
            return _structure_value(resolved, raw_nodes, symbol_table=symbol_table, already_unwrapped=already_unwrapped)
    # Same "unknown" fallback as convert/jsonschema.py's _attribute_schema,
    # for an unresolved Type (type_kind is None - e.g. an external/
    # unqualified reference not loaded via --repo). Also covers CoordType/
    # LineType NESTED inside a structure/list element (as opposed to the
    # top-level Feature's own geometry attribute, handled separately via
    # "place") - no real corpus DATA shows this occurring, so it stays
    # unsupported rather than reusing the "place" geometry shape without
    # evidence (RULE #7).
    return {"x-unsupported": kind or "unknown"}


def _structure_value(
    resolved: ResolvedAttribute, raw_nodes: list[RawNode], *, symbol_table: SymbolTable | None, already_unwrapped: bool,
) -> dict[str, Any]:
    """Recurse into a genuine STRUCTURE occurrence's own attributes.

    Value-producing counterpart of xtf/validate.py's `_validate_attrs`
    recursion (same real wire convention, same `already_unwrapped`
    distinction - a bug there, fixed once, is worth respecting exactly
    rather than re-deriving): `already_unwrapped=False` (a root/plain
    structure-typed attribute) - `raw_nodes[0]` is the node NAMED AFTER
    THE ATTRIBUTE (e.g. `<Name>`), whose ONLY child is the actual
    structure-content wrapper (`<...MultilingualText>`) - one level of
    unwrapping needed. `already_unwrapped=True` (a `MultiValue`
    occurrence, see `_multi_value`) - `raw_nodes[0]` IS ALREADY that
    wrapper - unwrapping it again would misgroup the grandchildren
    instead of the real attributes.
    """
    node = raw_nodes[0] if raw_nodes else None
    if node is None:
        return {}
    wrapper = node if already_unwrapped else (node.children[0] if node.children else None)
    if wrapper is None or not isinstance(resolved.type_instance, MetaInstance):
        return {}
    return _members_value(resolved.type_instance, _group_by_tag(wrapper.children), symbol_table=symbol_table)


def _multi_value(resolved: ResolvedAttribute, raw_nodes: list[RawNode], *, symbol_table: SymbolTable | None) -> Any:
    """`BAG {m..n} OF X` / `LIST {m..n} OF X` -> a JSON array of converted occurrences.

    Real wire convention (confirmed on `KGS_PBC_V2_2.ili`'s
    `Objektart`/`EGID`/`Adressen`, same as xtf/validate.py's own
    MultiValue handling): a SINGLE element named after the attribute,
    containing each occurrence as a DIRECT CHILD - never repeated
    attribute-name tags at the object level (that convention is what
    `XtfObject.attributes` itself already captures, a DIFFERENT kind of
    repetition). Each occurrence is re-dispatched through
    `_attribute_value` via a synthetic `ResolvedAttribute` for
    `MultiValue.BaseType` (Structure, Reference, scalar, ... - the exact
    same generic mechanism, unlimited recursion), `already_unwrapped=True`
    since an occurrence IS the content wrapper already (see
    `_structure_value`).
    """
    base_type = getattr(resolved.type_instance, "BaseType", None)
    if not isinstance(base_type, MetaInstance):
        return {"x-unsupported": "MultiValue"}
    base_kind = base_type._qualified_class.rsplit(".", 1)[-1]
    values: list[Any] = []
    for node in raw_nodes:
        for occurrence in node.children:
            occ_resolved = ResolvedAttribute(attr=resolved.attr, type_instance=base_type, type_kind=base_kind, mandatory=False)
            values.append(_attribute_value(occ_resolved, [occurrence], symbol_table=symbol_table, already_unwrapped=True))
    return values


def _members_value(cls: MetaInstance, attrs: dict[str, list[RawNode]], *, symbol_table: SymbolTable | None) -> dict[str, Any]:
    """Convert every attribute present in `attrs` against `cls`'s own schema into a plain dict.

    Shared by `object_to_feature` (the root object) and `_structure_value`
    (nested STRUCTURE content) - same "resolve schema, dispatch per kind"
    logic applied to any Class/Structure's own attribute set, mirroring
    xtf/validate.py's `_validate_attrs` (there: valid/invalid issues;
    here: a value). An attribute present on the wire but not found in the
    schema is silently skipped - not this converter's job to flag,
    `validate()` does.
    """
    schema_attrs = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    resolved_attrs = {name: resolve_attribute(attr) for name, attr in schema_attrs.items()}
    result: dict[str, Any] = {}
    for name, raw_nodes in attrs.items():
        if name not in resolved_attrs:
            continue
        result[name] = _attribute_value(resolved_attrs[name], raw_nodes, symbol_table=symbol_table)
    return result


# --- Geometry (`"place"`) ----------------------------------------------------
#
# Value-extraction counterparts of xtf/validate.py's _validate_coord_node/
# _validate_polyline_node/_validate_boundary_node/_validate_surface_node -
# same wire structure (reuses the SAME tag helpers/sets, imported directly
# rather than duplicated), but building a JSON-FG geometry value instead of
# a list of validation issues. Returns `None` (never raises) on anything
# this Lot doesn't represent - a custom LINE FORM segment, a missing/
# non-numeric component - so the caller falls back to leaving the attribute
# in "properties" (RULE #5: never silently misrepresent geometry that
# couldn't be read). An ARC segment IS representable (see `_read_polyline`
# below, docs/interlis-geometry-sfa-mapping.md, decision 2026-08-26): a
# straight-only POLYLINE/BOUNDARY still returns a plain position list (as
# before, wrapped into LineString/Polygon by the caller), but one
# containing at least one ARC returns an already-typed JSON-FG geometry
# object (CircularString/CompoundCurve/CurvePolygon) instead - the caller
# (`_line_geometry`) tells the two apart via `isinstance(value, dict)`.

def _positions_from(node: RawNode, prefix: str) -> list[float] | None:
    """Read `{prefix}1`, `{prefix}2`, ... as one position - shared by COORD (`C`) and an ARC's intermediate point (`A`)."""
    components = _axis_components(node, prefix)
    if not components:
        return None
    try:
        return [float(c.text) for c in components]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_coord(node: RawNode) -> list[float] | None:
    if _geom_tag(node) != "COORD":
        return None
    return _positions_from(node, "C")


def _read_arc(node: RawNode) -> tuple[list[float], list[float]] | None:
    """Read an ArcSegment (eCH-0031 V2.1.0 SS4.3.11.14): `(intermediate point, end point)`.

    The intermediate point (A1/A2) is always 2D - INTERLIS never carries a
    3rd component for it (confirmed in xtf/validate.py's `_validate_arc_node`)
    even when the surrounding COORD/end point is 3D; the optional radius
    (R) is redundant with the 3 points a `CircularString` arc needs and is
    not read here.
    """
    if _geom_tag(node) != "ARC":
        return None
    end = _positions_from(node, "C")
    mid = _positions_from(node, "A")
    return None if end is None or mid is None else (mid, end)


def _read_polyline(node: RawNode) -> list[list[float]] | dict[str, Any] | None:
    """Read a POLYLINE's SegmentSequence into a plain position list (straight-only) or a curved geometry object.

    A SegmentSequence always starts with a COORD (the path's start point),
    then zero or more StraightSegment (COORD)/ArcSegment (ARC) - never
    ArcSegment first (eCH-0031 V2.1.0 SS4.3.11.14). Consecutive segments of
    the same kind are grouped into one run (a straight run -> `LineString`,
    an arc run -> `CircularString`, chained arcs sharing their shared
    endpoint per JSON-FG's own encoding rather than repeating it - see
    core/examples/multi-curve.json, opengeospatial/ogc-feat-geo-json); a
    run switch keeps the boundary point as the start of the next run. A
    straight-only polyline collapses back to the original plain position
    list (unchanged return shape, still what `_line_geometry` wraps into a
    "LineString"/"Polygon"); one made of a single arc run returns a bare
    `CircularString`; anything mixing runs returns a `CompoundCurve`
    (JSON-FG Part 1 Core SS7.5.2 - each item's first position equals the
    previous item's last, exactly how runs are chained here).
    """
    if _geom_tag(node) != "POLYLINE" or not node.children:
        return None
    segments = node.children
    if _geom_tag(segments[0]) != "COORD":
        return None
    start = _read_coord(segments[0])
    if start is None:
        return None
    parts: list[dict[str, Any]] = []
    kind = "line"
    points: list[list[float]] = [start]
    for seg in segments[1:]:
        tag = _geom_tag(seg)
        if tag == "COORD":
            pos = _read_coord(seg)
            if pos is None:
                return None
            if kind == "arc":
                parts.append({"type": "CircularString", "coordinates": points})
                kind = "line"
                points = [points[-1]]
            points.append(pos)
        elif tag == "ARC":
            arc = _read_arc(seg)
            if arc is None:
                return None
            mid, end = arc
            if kind == "line":
                if len(points) > 1:
                    parts.append({"type": "LineString", "coordinates": points})
                    points = [points[-1]]
                kind = "arc"
            points.extend((mid, end))
        else:
            return None  # custom LINE FORM segment - not representable here
    parts.append({"type": "CircularString" if kind == "arc" else "LineString", "coordinates": points})
    if len(parts) == 1:
        sole = parts[0]
        return sole["coordinates"] if sole["type"] == "LineString" else sole
    return {"type": "CompoundCurve", "geometries": parts}


def _read_boundary(node: RawNode) -> list[list[float]] | dict[str, Any] | None:
    if _geom_tag(node) not in _BOUNDARY_TAGS:
        return None
    polyline = _find_child(node, "POLYLINE")
    return None if polyline is None else _read_polyline(polyline)


def _read_surface(node: RawNode) -> list[list[list[float]]] | dict[str, Any] | None:
    """Read a SURFACE/AREA into plain rings (straight-only) or a `CurvePolygon` (any ring with an arc).

    A `CurvePolygon`'s "geometries" member is a list of closed curve
    geometries (JSON-FG Part 1 Core SS7.5.3) - every ring, straight or
    curved, is wrapped as a full geometry object there (a plain ring
    becomes `{"type": "LineString", ...}`), unlike the plain-`Polygon` case
    where rings stay bare position lists (`_read_boundary`'s straight-only
    return shape, unchanged).
    """
    if _geom_tag(node) not in _SURFACE_TAGS:
        return None
    boundaries = [c for c in node.children if _geom_tag(c) in _BOUNDARY_TAGS]
    if not boundaries:
        return None
    rings: list[list[list[float]] | dict[str, Any]] = []
    any_curved = False
    for boundary in boundaries:
        ring = _read_boundary(boundary)
        if ring is None:
            return None
        if isinstance(ring, dict):
            any_curved = True
        rings.append(ring)  # first = outer boundary, eCH-0031 SS4.3.11.15 (order-only, no tag distinction)
    if any_curved:
        geometries = [r if isinstance(r, dict) else {"type": "LineString", "coordinates": r} for r in rings]
        return {"type": "CurvePolygon", "geometries": geometries}
    return rings


def _coord_geometry(resolved: ResolvedAttribute, node: RawNode) -> dict[str, Any] | None:
    """`node` is the attribute's own node (e.g. `<AccidentLocation>`) - its 1st child is COORD/MULTICOORD."""
    coord_type = resolved.type_instance
    multi = bool(getattr(coord_type, "Multi", False))
    expected = "MULTICOORD" if multi else "COORD"
    child = node.children[0] if node.children else None
    if child is None or _geom_tag(child) != expected:
        return None
    if not multi:
        pos = _read_coord(child)
        return None if pos is None else {"type": "Point", "coordinates": pos}
    positions: list[list[float]] = []
    for c in (c for c in child.children if _geom_tag(c) == "COORD"):
        pos = _read_coord(c)
        if pos is None:
            return None
        positions.append(pos)
    return None if not positions else {"type": "MultiPoint", "coordinates": positions}


def _line_geometry(resolved: ResolvedAttribute, node: RawNode) -> dict[str, Any] | None:
    """Build the JSON-FG geometry value for a LineType attribute occurrence.

    `reader` (`_read_polyline`/`_read_surface`) returns either a plain
    position list (straight-only - wrapped here into "LineString"/"Polygon"/
    "MultiLineString"/"MultiPolygon", unchanged from before circular-arcs
    support) or an already-typed geometry object (`CircularString`/
    `CompoundCurve`/`CurvePolygon` - any run/ring containing an ARC segment,
    see `_read_polyline`/`_read_surface`), told apart via `isinstance(...,
    dict)`. In the `multi` case, ANY curved part switches the WHOLE
    attribute to JSON-FG's `MultiCurve`/`MultiSurface` (SS7.5.4/.5) instead
    of `MultiLineString`/`MultiPolygon` - both require every member to be a
    full geometry object, so a straight part is wrapped into a
    `LineString`/`Polygon` there too rather than left as a bare coordinate
    array (docs/interlis-geometry-sfa-mapping.md).
    """
    line_type = resolved.type_instance
    kind = getattr(line_type, "Kind", None)
    multi = bool(getattr(line_type, "Multi", False))
    single_tags = _LINE_KIND_TAGS.get(kind)
    if single_tags is None:
        return None
    expected_tags = _LINE_KIND_MULTI_TAGS[kind] if multi else single_tags
    child = node.children[0] if node.children else None
    if child is None or _geom_tag(child) not in expected_tags:
        return None
    is_polyline = single_tags == frozenset({"POLYLINE"})
    reader = _read_polyline if is_polyline else _read_surface
    single_type = "LineString" if is_polyline else "Polygon"
    multi_type = "MultiLineString" if is_polyline else "MultiPolygon"
    curved_multi_type = "MultiCurve" if is_polyline else "MultiSurface"
    if not multi:
        value = reader(child)
        if value is None:
            return None
        return value if isinstance(value, dict) else {"type": single_type, "coordinates": value}
    values = []
    any_curved = False
    for part in (c for c in child.children if _geom_tag(c) in single_tags):
        value = reader(part)
        if value is None:
            return None
        if isinstance(value, dict):
            any_curved = True
        values.append(value)
    if not values:
        return None
    if any_curved:
        geometries = [v if isinstance(v, dict) else {"type": single_type, "coordinates": v} for v in values]
        return {"type": curved_multi_type, "geometries": geometries}
    return {"type": multi_type, "coordinates": values}


def _meta_value(instance: MetaInstance | None, name: str) -> str | None:
    if instance is None:
        return None
    for meta in getattr(instance, "MetaAttribute", None) or []:
        if getattr(meta, "Name", None) == name:
            return meta.Value
    return None


def _crs_uri(coord_type: MetaInstance | None) -> str | None:
    """Resolve a CoordType's `!!@CRS=EPSG:<code>` meta-attribute (eCH-0117) into a JSON-FG `coordRefSys` URI.

    `None` (never a guessed default) when the meta-attribute is absent or
    not an `EPSG:<digits>` value - Swiss data is never WGS84, so omitting
    `coordRefSys` would make a JSON-FG reader assume CRS84/CRS84h by the
    spec's own default-CRS rule (a real misrepresentation, not just a gap)
    - see docs/jsonfg-conversion-strategy.md. Requires
    `ModelRepository._get_table` to propagate `meta_attributes` into
    imported models (builder/repository.py) - without that fix this
    resolves to `None` for virtually every real Swiss geometry attribute,
    since they import their CoordType rather than declaring it locally.
    """
    raw = _meta_value(coord_type, "CRS")
    if raw is None:
        return None
    scheme, _, code = raw.partition(":")
    if scheme.strip().upper() != "EPSG" or not code.strip().isdigit():
        return None
    return f"{CRS_URI_PREFIX}{code.strip()}"


def _place_and_crs(resolved: ResolvedAttribute, node: RawNode) -> tuple[dict[str, Any], str] | None:
    """Return `(geometry, coordRefSys)` for one geometry-typed attribute occurrence, or `None`."""
    if resolved.type_kind == "CoordType":
        geometry = _coord_geometry(resolved, node)
        coord_type = resolved.type_instance
    elif resolved.type_kind == "LineType":
        geometry = _line_geometry(resolved, node)
        coord_type = line_coord_type(resolved.type_instance)
    else:
        return None
    if geometry is None:
        return None
    crs = _crs_uri(coord_type)
    return None if crs is None else (geometry, crs)


def object_to_feature(
    obj: XtfObject, cls: MetaInstance, *, standalone: bool = True, symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """Convert one XtfObject into a JSON-FG Feature object.

    `cls` is the already-resolved Class/Structure instance for
    `obj.qualified_class` (xtf.schema.resolve_class) - resolution stays
    the caller's job, same split as xtf/validate.py's `_validate_object`,
    so this function is a pure value transform.

    `symbol_table`, when given, additionally includes EMBEDDED
    ASSOCIATION ROLES (`xtf.schema.schema_members_of` instead of plain
    `attributes_of`) as pseudo-attributes - same opt-in precondition as
    `convert/jsonschema.py`'s `class_to_json_schema`. `None` (the
    default) means own+inherited `ClassAttribute`s only.

    `standalone=True` (default): produced as a JSON-FG "root object" in
    its own right (OGC 21-045r1 clause 8: "not contained in another
    JSON-FG object") - carries its own "conformsTo" (core, and
    types-schemas since "featureType" is always included, per core
    requirement /req/core/metadata.H; plus circular-arcs, SS7.5, whenever
    "place" ends up one of CircularString/CompoundCurve/CurvePolygon/
    MultiCurve/MultiSurface - see `_read_polyline`/`_read_surface`).
    `standalone=False` (used by
    `transfer_to_feature_collection` for a Feature nested inside a
    FeatureCollection, which becomes the root object instead) OMITS
    "conformsTo" - required, not a style choice: /req/core/metadata.C
    states "Every other JSON-FG object SHALL NOT include a 'conformsTo'
    member." "id" is included only when `obj.tid` is set (GeoJSON RFC
    7946 SS3.2: OPTIONAL, string or number) - never a literal `null`,
    unlike "geometry" which RFC 7946 requires as a member even when
    unlocated (`null`). "featureType" reuses the class's short `Name`
    (same identifier convert/jsonschema.py uses as its $defs key), so a
    Feature and its schema entry can be linked by name once a future lot
    adds "featureSchema".

    Geometry ("place"/"coordRefSys"): only when `cls` has EXACTLY ONE
    own+inherited attribute whose type resolves directly to CoordType/
    LineType (never via a BAG/LIST wrapper - out of scope, no real corpus
    evidence, see docs/jsonfg-conversion-strategy.md) AND that attribute's
    actual wire value converts cleanly (see `_place_and_crs` - a `None`
    result, e.g. a custom LINE FORM segment or an unresolved CRS, leaves
    the attribute in "properties" instead, marked `x-unsupported`
    like any other out-of-scope attribute - never a silent loss). A class
    with zero or multiple geometry-typed attributes gets no "place"
    either (multi-geometry real cases exist - e.g. a point + an area on
    the same class - but picking one over the other needs a policy this
    Lot deliberately doesn't invent). "geometry" (the WGS84 GeoJSON
    fallback) always stays `null` here - reprojecting LV95/LV03 to WGS84
    would need a real coordinate-transform dependency, out of scope for
    this pure-Python runtime; JSON-FG core explicitly allows this
    ("geometry" is `null` when no valid WGS84 representation exists).
    """
    schema_attrs = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    resolved_attrs = {name: resolve_attribute(attr) for name, attr in schema_attrs.items()}
    properties = _members_value(cls, obj.attributes, symbol_table=symbol_table)

    place: dict[str, Any] | None = None
    crs_uri: str | None = None
    geometry_names = [name for name, r in resolved_attrs.items() if r.type_kind in _GEOMETRY_KINDS]
    if len(geometry_names) == 1:
        geom_name = geometry_names[0]
        raw_nodes = obj.attributes.get(geom_name)
        if raw_nodes:
            result = _place_and_crs(resolved_attrs[geom_name], raw_nodes[0])
            if result is not None:
                place, crs_uri = result
                properties.pop(geom_name, None)

    feature: dict[str, Any] = {"type": "Feature"}
    if standalone:
        conforms_to = [CONF_CORE, CONF_TYPES_SCHEMAS]
        if place is not None and place.get("type") in _CIRCULAR_ARC_TYPES:
            conforms_to.append(CONF_CIRCULAR_ARCS)
        feature["conformsTo"] = conforms_to
    if obj.tid is not None:
        feature["id"] = obj.tid
    feature["featureType"] = getattr(cls, "Name", None) or obj.qualified_class
    feature["geometry"] = None
    if place is not None:
        feature["place"] = place
        feature["coordRefSys"] = crs_uri
    feature["properties"] = properties
    return feature


# --- VIEW evaluation (backlog item 8, Lot C) --------------------------------
#
# PROJECTION OF/JOIN OF Views evaluated in memory against an already-parsed
# XtfTransfer, producing JSON-FG Features shaped like the VIEW rather than
# its raw base class(es) - the FGDM4GS report's central equivalence (a VIEW
# = the FeatureType projection instruction, docs/... item 8/.claude/PROGRESS.md).
# WHERE-filtered Views are explicitly OUT of scope here (see
# `unsupported_view_reason`) rather than evaluated against the Expression
# tree: investigation (2026-08-24) found 4 real bugs in Expression
# construction (PathOrInspFactor.PathEls empty, CompoundExpr.Operation lost,
# Constant.Value shape mismatch, DEFINED() losing its target) with zero real
# corpus WHERE-on-VIEW evidence to justify fixing them blind - a VIEW with a
# WHERE is skipped with a clear diagnostic (RULE #5: never a silently wrong
# result) rather than silently dropped or wrongly evaluated.

_UNSET = object()


def unsupported_view_reason(view: MetaInstance) -> str | None:
    """Return why `evaluate_view` can't evaluate `view`, or `None` if it can.

    Reused by `cli.cmd_convert_jsonfg` to print a clear diagnostic for
    every VIEW it skips instead of a silent omission.
    """
    kind = getattr(view, "FormationKind", None)
    if kind not in ("Projection", "Join"):
        return f"FormationKind {kind!r} not evaluated (only Projection/Join)"
    if getattr(view, "Where", None) is not None:
        return "WHERE clause present - Expression evaluation isn't supported yet (see .claude/HANDOFF.md)"
    bases = [b for b in getattr(view, "RenamedBaseView", None) or [] if isinstance(b, MetaInstance) and isinstance(b.BaseView, MetaInstance)]
    if not bases:
        return "no resolvable base (RenamedBaseView.BaseView unresolved)"
    return None


def _join_combinations(bases: list[MetaInstance], objects_by_base: list[list[XtfObject]]) -> list[list[XtfObject | None]]:
    """Cartesian product of `objects_by_base`, one list per base, with `RenamedBaseView.OrNull` outer-join handling.

    Reference Manual eCH-0031 V2.1.0 SS3.16 (JOIN OF): "kartesisches Produkt
    der Basis-Klassen" - a literal cartesian product, no join KEY/condition
    of its own (a WHERE clause narrows the result on top, out of this
    Lot's scope, see `unsupported_view_reason`). "(OR NULL)": "wenn zu
    einer bestimmten Kombination der vorangegangenen Objekte kein Objekt
    der gewuenschten weiteren Klasse gefunden wird" - since there is no
    WHERE here to narrow a combination-by-combination match, a base
    contributes "no object found" only when it has ZERO objects in the
    whole transfer; `OrNull` then keeps every partial combination alive
    with a `None` placeholder for that base (contributing no attributes)
    instead of collapsing the whole JOIN to the empty set.
    """
    combos: list[list[XtfObject | None]] = [[]]
    for base, objs in zip(bases, objects_by_base):
        if objs:
            combos = [combo + [obj] for combo in combos for obj in objs]
        elif bool(getattr(base, "OrNull", False)):
            combos = [combo + [None] for combo in combos]
        else:
            return []
    return combos


def _merge_join_combo(combo: list[XtfObject | None], view_name: str) -> XtfObject:
    """Merge one JOIN OF combination into a single synthetic `XtfObject`, re-fed through `object_to_feature`.

    `attributes` are pooled from every non-`None` participant (a real
    key collision between 2 bases' own attribute names would let the
    later participant win silently - no real corpus evidence of this, the
    2 known real JOIN OF examples are attribute-disjoint). `tid` joins
    every participant's own tid with `_` (`None` if none has one) - a
    simple, stable synthetic id; JSON-FG's "id" is OPTIONAL (RFC 7946
    SS3.2), so this is a convenience, not a spec requirement.
    `qualified_class` is set to the VIEW's own short name - never read by
    `object_to_feature` (it derives "featureType" from `cls.Name`, the
    VIEW itself, passed separately), kept only for readability/debugging.
    """
    attributes: dict[str, list[RawNode]] = {}
    tid_parts: list[str] = []
    for obj in combo:
        if obj is None:
            continue
        attributes.update(obj.attributes)
        if obj.tid is not None:
            tid_parts.append(obj.tid)
    return XtfObject(tid="_".join(tid_parts) or None, qualified_class=view_name, attributes=attributes)


def evaluate_view(
    view: MetaInstance, transfer: XtfTransfer, *,
    symbol_table: SymbolTable, repository: ModelRepository | None = None, standalone: bool = False,
) -> list[dict[str, Any]]:
    """Evaluate one PROJECTION OF/JOIN OF `view` against `transfer` into JSON-FG Features.

    Raises `ValueError` if `unsupported_view_reason(view)` isn't `None` -
    callers (e.g. `cmd_convert_jsonfg`) are expected to check that first
    and skip with a diagnostic, never call this blind.

    `PROJECTION OF` (single base): each matching base object is re-tagged
    with `view` as its `cls` directly, no data transformation at all - the
    View's `ClassAttribute` list (backlog item 8 Lot A2's `ALL OF`
    expansion) already carries the SAME attribute names as the base
    Class's own wire encoding, so `object_to_feature` naturally emits only
    the View's declared properties, exactly like Lot B's `View -> JSON
    Schema` needed no View-specific code either.

    `JOIN OF` (N bases): the full cartesian product of each base's
    matching objects (`_join_combinations`), each combination merged into
    one synthetic `XtfObject` (`_merge_join_combo`) then fed through the
    same `object_to_feature(obj, view, ...)` path.

    Base matching (`is_class_compatible`, same mechanism already used by
    `_extract_reference`'s embedded-role/reference resolution elsewhere in
    this module): an XtfObject's resolved Class must BE, or be a SUBCLASS
    of, a `RenamedBaseView.BaseView` - real corpus data can transfer a
    concrete subclass where the VIEW's base names an abstract superclass.
    """
    reason = unsupported_view_reason(view)
    if reason is not None:
        raise ValueError(f"cannot evaluate view {getattr(view, 'Name', None)!r}: {reason}")

    bases = [b for b in view.RenamedBaseView if isinstance(b, MetaInstance) and isinstance(b.BaseView, MetaInstance)]

    resolved_by_qualified_class: dict[str, MetaInstance | None] = {}
    resolved_objects: list[tuple[XtfObject, MetaInstance]] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            cls = resolved_by_qualified_class.get(obj.qualified_class, _UNSET)
            if cls is _UNSET:
                cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
                resolved_by_qualified_class[obj.qualified_class] = cls
            if cls is not None:
                resolved_objects.append((obj, cls))

    objects_by_base = [[obj for obj, cls in resolved_objects if is_class_compatible(cls, base.BaseView)] for base in bases]

    if view.FormationKind == "Projection":
        return [object_to_feature(obj, view, standalone=standalone, symbol_table=symbol_table) for obj in objects_by_base[0]]

    view_name = getattr(view, "Name", None) or "View"
    combos = _join_combinations(bases, objects_by_base)
    features = []
    for combo in combos:
        feature = object_to_feature(_merge_join_combo(combo, view_name), view, standalone=standalone, symbol_table=symbol_table)
        members = _join_members(bases, combo)
        if members:
            feature["x-join-members"] = members
        features.append(feature)
    return features


def _join_members(bases: list[MetaInstance], combo: list[XtfObject | None]) -> list[dict[str, Any]]:
    """Return `{"featureType": ..., "id": ...}` for every real (non-`None`) participant of one JOIN combination.

    Backlog item 8 Lot D: a `JOIN OF` collection is GET-only
    (`.claude/PROGRESS.md`'s CRUD decision - no natural single writable
    target for a multi-base combination, by analogy with non-updatable
    SQL views). This is what makes that limitation actionable rather than
    a dead end: each base object this Lot's runtime ALREADY publishes as
    its own, independently fully-writable collection (a plain `Class` -
    `transfer_to_feature_collection` emits it via `resolve_class`
    regardless of any `views=` given), so a client that needs to edit a
    property coming from one specific base can resolve WHICH collection/
    id to `PUT`/`PATCH` instead, straight off the JOIN Feature itself -
    no need to reverse-engineer `_merge_join_combo`'s `tid` join
    convention. `featureType` mirrors `object_to_feature`'s own
    convention (`cls.Name`, falling back to the wire tag).
    """
    return [
        {"featureType": getattr(base.BaseView, "Name", None) or obj.qualified_class, "id": obj.tid}
        for base, obj in zip(bases, combo)
        if obj is not None
    ]


def transfer_to_feature_collection(
    transfer: XtfTransfer, *, symbol_table: SymbolTable, repository: ModelRepository | None = None,
    views: list[MetaInstance] | None = None,
) -> dict[str, Any]:
    """Convert every resolvable object of `transfer` into one JSON-FG FeatureCollection.

    Walks all of `transfer`'s baskets (real corpus evidence: 11/12
    xtf_corpus/geoadmin files hold exactly 1 basket, the one exception
    holds 2 - not worth a separate per-basket entry point) and resolves
    each object's Class itself (xtf.schema.resolve_class), same
    architecture as xtf/validate.py's `validate_transfer` (which likewise
    resolves internally, unlike the single-object `object_to_feature`/
    `_validate_object` pair - a whole-transfer entry point naturally has
    `symbol_table`/`repository` on hand already, e.g. from
    `cli.cmd_convert_jsonfg`). An object whose class doesn't resolve is
    skipped - not this converter's job to flag (validate() does), same
    stance `object_to_feature` already takes for an unknown attribute
    NAME; there is no schema to draw even a featureType/properties from
    without a resolved class, so there is nothing to emit for it.

    Each Feature is produced with `standalone=False` (no per-feature
    "conformsTo" - the FeatureCollection is the JSON-FG root object here,
    RULE /req/core/metadata.C). When every produced Feature shares the
    same "featureType", it is ALSO set once on the collection itself
    (clause 13 Recommendation A, "homogeneous feature collections") -
    purely additive, never replaces the per-feature member (clause 13
    Requirement B already allows either placement, so both stay valid).

    "coordRefSys" hoisting (uniform case only) is NOT an optional
    optimization - it is what `/req/core/same-crs` actually requires
    ("A 'coordRefSys' member SHALL only be included in the JSON-FG root
    object and not in any other JSON-FG objects") and what the
    Standard's OWN official example demonstrates verbatim
    (`core/examples/airports.json`, opengeospatial/ogc-feat-geo-json):
    `"coordRefSys"` declared once on the `FeatureCollection`, entirely
    ABSENT from each nested Feature. When every Feature that has a
    "place" shares the same "coordRefSys", it is moved to the collection
    and removed from every Feature. When they differ (heterogeneous CRS
    within one collection - no real corpus evidence found: every basket
    seen so far uses one CRS throughout), per-feature "coordRefSys" is
    left as a conservative fallback rather than inventing an unverified
    geometry-level placement with no official example to check it
    against (see docs/jsonfg-conversion-strategy.md).

    `views` (backlog item 8 Lot C, optional): PROJECTION OF/JOIN OF Views
    already filtered by the caller to ones `evaluate_view` can actually
    handle (same `unsupported_view_reason` check `cmd_convert_jsonfg`
    applies before calling this - this function assumes every given view
    IS evaluable, raising via `evaluate_view` rather than silently
    skipping one that isn't). Evaluated against the SAME `transfer` and
    folded into the SAME feature list BEFORE the featureType/coordRefSys
    uniformity hoisting below runs, so a transfer producing only
    view-shaped Features (or a homogeneous mix of both) still benefits
    from collection-level hoisting exactly like class-shaped Features do.
    """
    features: list[dict[str, Any]] = []
    for basket in transfer.baskets:
        for obj in basket.objects:
            cls = resolve_class(obj.qualified_class, symbol_table=symbol_table, repository=repository)
            if cls is None:
                continue
            features.append(object_to_feature(obj, cls, standalone=False, symbol_table=symbol_table))

    for view in views or []:
        features.extend(evaluate_view(view, transfer, symbol_table=symbol_table, repository=repository, standalone=False))

    conforms_to = [CONF_CORE, CONF_TYPES_SCHEMAS]
    if any(f.get("place", {}).get("type") in _CIRCULAR_ARC_TYPES for f in features):
        conforms_to.append(CONF_CIRCULAR_ARCS)
    collection: dict[str, Any] = {
        "type": "FeatureCollection",
        "conformsTo": conforms_to,
        "features": features,
    }
    feature_types = {f["featureType"] for f in features}
    if len(feature_types) == 1:
        collection["featureType"] = next(iter(feature_types))

    crs_values = {f["coordRefSys"] for f in features if "coordRefSys" in f}
    if len(crs_values) == 1:
        collection["coordRefSys"] = next(iter(crs_values))
        for f in features:
            f.pop("coordRefSys", None)

    return collection
