"""XTF data instance -> JSON-FG Feature conversion: scalar properties, OID, featureType, geometry.

See docs/jsonfg-conversion-strategy.md for the design decision and scope.
Converts one already-parsed XtfObject (xtf/parse.py, structural layer)
into a JSON-FG (OGC 21-045r1) Feature object conforming to the "core" and
"types-schemas" requirements classes only - single-attribute point/line/
polygon geometry -> "place" (never "geometry", which stays `null` - no
WGS84 reprojection, see module docs), no FeatureCollection wrapping, no
STRUCTURE/BAG/LIST/REFERENCE/embedded-role attributes, no multi-geometry
classes yet (each a separate future lot). Reuses the schema resolution
already proven by xtf/validate.py (resolve_attribute/attributes_of/
coord_axes/line_coord_type) AND its wire-tag helpers (_geom_tag/
_find_child/_axis_components/the BOUNDARY/SURFACE/LINE_KIND tag sets)
rather than a second parallel implementation of the same COORD/POLYLINE/
SURFACE/AREA/MULTI* wire conventions - convert() stays a decoupled stage
from validate(), same split already established by convert/jsonschema.py.
"""
from typing import Any

from interlis.convert.jsonschema import _is_integer_range
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfObject
from interlis.xtf.schema import ResolvedAttribute, attributes_of, line_coord_type, resolve_attribute
from interlis.xtf.validate import (
    _BOUNDARY_TAGS,
    _LINE_KIND_MULTI_TAGS,
    _LINE_KIND_TAGS,
    _SURFACE_TAGS,
    _axis_components,
    _find_child,
    _geom_tag,
)

JSON_FG_VERSION = "1.0"
CONF_CORE = f"http://www.opengis.net/spec/json-fg-1/{JSON_FG_VERSION}/conf/core"
CONF_TYPES_SCHEMAS = f"http://www.opengis.net/spec/json-fg-1/{JSON_FG_VERSION}/conf/types-schemas"
CRS_URI_PREFIX = "http://www.opengis.net/def/crs/EPSG/0/"

_SCALAR_KINDS = {"NumType", "TextType", "EnumType", "BooleanType"}
_GEOMETRY_KINDS = {"CoordType", "LineType"}


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


def _attribute_value(resolved: ResolvedAttribute, raw_nodes: list[RawNode]) -> Any:
    if resolved.type_kind not in _SCALAR_KINDS:
        # Same "unknown" fallback as convert/jsonschema.py's
        # _attribute_schema, for an unresolved Type (type_kind is None -
        # e.g. an external/unqualified reference not loaded via --repo).
        return {"x-interlis-unsupported": resolved.type_kind or "unknown"}
    return _scalar_value(resolved, raw_nodes[0])


# --- Geometry (`"place"`) ----------------------------------------------------
#
# Value-extraction counterparts of xtf/validate.py's _validate_coord_node/
# _validate_polyline_node/_validate_boundary_node/_validate_surface_node -
# same wire structure (reuses the SAME tag helpers/sets, imported directly
# rather than duplicated), but building a JSON-FG geometry value instead of
# a list of validation issues. Returns `None` (never raises) on anything
# this Lot doesn't represent - an ARC segment, a custom LINE FORM segment,
# a missing/non-numeric component - so the caller falls back to leaving the
# attribute in "properties" (RULE #5: never silently misrepresent a curved
# edge as a straight one).

def _read_coord(node: RawNode) -> list[float] | None:
    if _geom_tag(node) != "COORD":
        return None
    components = _axis_components(node, "C")
    if not components:
        return None
    try:
        return [float(c.text) for c in components]  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_polyline(node: RawNode) -> list[list[float]] | None:
    if _geom_tag(node) != "POLYLINE" or not node.children:
        return None
    positions: list[list[float]] = []
    for seg in node.children:
        if _geom_tag(seg) != "COORD":
            return None  # ARC / custom LINE FORM segment - not representable here
        pos = _read_coord(seg)
        if pos is None:
            return None
        positions.append(pos)
    return positions


def _read_boundary(node: RawNode) -> list[list[float]] | None:
    if _geom_tag(node) not in _BOUNDARY_TAGS:
        return None
    polyline = _find_child(node, "POLYLINE")
    return None if polyline is None else _read_polyline(polyline)


def _read_surface(node: RawNode) -> list[list[list[float]]] | None:
    if _geom_tag(node) not in _SURFACE_TAGS:
        return None
    boundaries = [c for c in node.children if _geom_tag(c) in _BOUNDARY_TAGS]
    if not boundaries:
        return None
    rings: list[list[list[float]]] = []
    for boundary in boundaries:
        ring = _read_boundary(boundary)
        if ring is None:
            return None
        rings.append(ring)  # first = outer boundary, eCH-0031 SS4.3.11.15 (order-only, no tag distinction)
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
    if not multi:
        value = reader(child)
        return None if value is None else {"type": single_type, "coordinates": value}
    values = []
    for part in (c for c in child.children if _geom_tag(c) in single_tags):
        value = reader(part)
        if value is None:
            return None
        values.append(value)
    return None if not values else {"type": multi_type, "coordinates": values}


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


def object_to_feature(obj: XtfObject, cls: MetaInstance) -> dict[str, Any]:
    """Convert one XtfObject into a standalone JSON-FG Feature object.

    `cls` is the already-resolved Class/Structure instance for
    `obj.qualified_class` (xtf.schema.resolve_class) - resolution stays
    the caller's job, same split as xtf/validate.py's `_validate_object`,
    so this function is a pure value transform.

    Produced as a JSON-FG "root object" in its own right (OGC 21-045r1
    clause 8: "not contained in another JSON-FG object") - carries its
    own "conformsTo" (core, and types-schemas since "featureType" is
    always included, per core requirement /req/core/metadata.H). "id" is
    included only when `obj.tid` is set (GeoJSON RFC 7946 SS3.2: OPTIONAL,
    string or number) - never a literal `null`, unlike "geometry" which
    RFC 7946 requires as a member even when unlocated (`null`). "featureType"
    reuses the class's short `Name` (same identifier convert/jsonschema.py
    uses as its $defs key), so a Feature and its schema entry can be
    linked by name once a future lot adds "featureSchema".

    Geometry ("place"/"coordRefSys"): only when `cls` has EXACTLY ONE
    own+inherited attribute whose type resolves directly to CoordType/
    LineType (never via a BAG/LIST wrapper - out of scope, no real corpus
    evidence, see docs/jsonfg-conversion-strategy.md) AND that attribute's
    actual wire value converts cleanly (see `_place_and_crs` - a `None`
    result, e.g. an ARC segment or an unresolved CRS, leaves the
    attribute in "properties" instead, marked `x-interlis-unsupported`
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
    schema_attrs = attributes_of(cls)
    resolved_attrs = {name: resolve_attribute(attr) for name, attr in schema_attrs.items()}

    properties: dict[str, Any] = {}
    for name, raw_nodes in obj.attributes.items():
        if name not in resolved_attrs:
            continue  # unknown to the schema - not this converter's job to flag, validate() does
        properties[name] = _attribute_value(resolved_attrs[name], raw_nodes)

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

    feature: dict[str, Any] = {
        "type": "Feature",
        "conformsTo": [CONF_CORE, CONF_TYPES_SCHEMAS],
    }
    if obj.tid is not None:
        feature["id"] = obj.tid
    feature["featureType"] = getattr(cls, "Name", None) or obj.qualified_class
    feature["geometry"] = None
    if place is not None:
        feature["place"] = place
        feature["coordRefSys"] = crs_uri
    feature["properties"] = properties
    return feature
