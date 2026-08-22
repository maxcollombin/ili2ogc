""".ili -> JSON Schema conversion: scalar types, STRUCTURE/BAG/LIST nesting, plain REFERENCE TO, and embedded association roles.

See docs/jsonschema-conversion-strategy.md for the design decision and
mappings/ilismeta16-to-jsonschema-rules.yml /
spec/conversion/jsonschema-mapping.yml for the concept/field contract this
module implements. Walks IlisMeta16 instances already built by
InterlisModelBuilder, reusing xtf.schema's type-resolution helpers
(resolve_attribute/attributes_of/schema_members_of/enum_values) rather than
duplicating any resolution logic - convert() is a decoupled stage from
validate().
"""
from typing import Any

from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    coord_axes,
    enum_values,
    line_coord_type,
    reference_external_status,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _is_integer_range(min_raw: str | None, max_raw: str | None) -> bool:
    """True if both bounds are present and neither has a decimal point.

    Same shape heuristic already used (read-only, not duplicated) by
    xtf/validate.py's `_decimal_places` for range-tolerance rounding - see
    docs/jsonschema-conversion-strategy.md.
    """
    return min_raw is not None and max_raw is not None and "." not in min_raw and "." not in max_raw


def _num_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    min_raw = getattr(type_instance, "Min", None)
    max_raw = getattr(type_instance, "Max", None)
    schema: dict[str, Any] = {"type": "integer" if _is_integer_range(min_raw, max_raw) else "number"}
    cast = int if schema["type"] == "integer" else float
    for keyword, raw in (("minimum", min_raw), ("maximum", max_raw)):
        if raw is None:
            continue
        try:
            schema[keyword] = cast(raw)
        except ValueError:
            pass  # non-numeric Min/Max (e.g. a predefined domain) - out of scope, same as validate.py
    return schema


def _text_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    max_length = getattr(type_instance, "MaxLength", None)
    if max_length is not None:
        try:
            schema["maxLength"] = int(max_length)
        except (TypeError, ValueError):
            pass
    return schema


def _formatted_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """FormattedType always describes a formatted TEXT value - `type: string`.

    `Format` names a predefined/custom format (e.g. `INTERLIS.XMLDate`) or
    a STRUCT-based template - no regex `pattern` is derived from it, only
    a conservative `string` type (see docs/jsonschema-conversion-strategy.md).
    """
    return {"type": "string"}


def _blackbox_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """BlackboxType (XML or BINARY payload) -> `type: string`.

    Neither variant has a native JSON Schema equivalent for its content
    shape - `Kind` ("Xml"/"Binary") is surfaced as an informational
    `x-interlis-blackbox-kind` marker instead of being silently dropped
    (RULE #5, same pattern as MultiValue's `x-interlis-ordered`).
    """
    schema: dict[str, Any] = {"type": "string"}
    kind = getattr(type_instance, "Kind", None)
    if kind is not None:
        schema["x-interlis-blackbox-kind"] = kind
    return schema


def _enum_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    # "OTHERS" is a transfer-time escape valid for any INTERLIS enum
    # (eCH-0031 SS4.3.11.3), not a declared domain value - excluded here,
    # see docs/jsonschema-conversion-strategy.md.
    values = enum_values(type_instance) - {"OTHERS"}
    return {"type": "string", "enum": sorted(values)}


def _position_schema(coord_type: MetaInstance | None) -> dict[str, Any]:
    """A single position - `[x, y, (z)]`, one array item per `CoordType.Axis`.

    `xtf.schema.coord_axes` (association `AxisSpec`, ORDERED `{1..3}
    NumType`) gives the exact axis count plus each axis's own Min/Max -
    reused directly via `_num_type_schema` (no duplicated numeric-range
    logic), one `prefixItems` entry per axis (2020-12 tuple validation),
    `items: false` to forbid a 4th coordinate. Falls back to an
    open-ended array of numbers when `Axis` isn't resolved (e.g. an
    external domain not loaded via `--repo`) - same graceful-degradation
    style already used by the XTF validator for the identical case.
    """
    axes = coord_axes(coord_type)
    if not axes:
        return {"type": "array", "items": {"type": "number"}}
    return {
        "type": "array",
        "prefixItems": [_num_type_schema(a) for a in axes],
        "items": False,
        "minItems": len(axes),
        "maxItems": len(axes),
    }


def _wrap_multi(schema: dict[str, Any], multi: object) -> dict[str, Any]:
    """Wrap `schema` in one more array level for MULTICOORD/MULTIPOLYLINE/MULTISURFACE/MULTIAREA.

    `Multi` (own BOOLEAN on `CoordType`/`LineType`, true for the `MULTI*`
    keyword) - the extra array level fully captures "possibly disjoint
    parts" on its own; unlike `MultiValue.Ordered`, no marker is needed
    since no information is lost by the nesting alone.
    """
    return {"type": "array", "items": schema} if bool(multi) else schema


def _coord_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """`COORD`/`MULTICOORD` attribute -> a position, or an array of positions if `Multi`."""
    return _wrap_multi(_position_schema(type_instance), getattr(type_instance, "Multi", None))


_SURFACE_LIKE_KINDS = frozenset({"Surface", "Area"})


def _line_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """`POLYLINE`/`DIRECTED POLYLINE` -> array of positions; `SURFACE`/`AREA` -> array of boundary rings.

    Vertex coordinate shape comes from `xtf.schema.line_coord_type`
    (association `LineCoord`, walks the `EXTENDS` chain like
    `xtf.schema.attributes_of`) fed into `_position_schema` - a vertex is
    always a single position, never itself MULTI, so `_coord_type_schema`
    (which would apply `Multi`-wrapping) is deliberately NOT reused here:
    that flag belongs to the referenced `CoordType` domain in its OWN
    right, unrelated to its use as a polyline/surface vertex type.
    `Kind in {Surface, Area}` (eCH-0031: both encoded identically on the
    wire - `AREA` is a semantic "WITHOUT OVERLAPS" constraint over the
    same `SURFACE` shape, not a distinct geometry) -> one more array
    level (boundary rings) than Polyline/DirectedPolyline; the first ring
    is the outer boundary, the rest are holes - no native JSON Schema
    keyword expresses "first item is special", surfaced as an
    informational `x-interlis-boundary-order` marker (RULE #5). ARC
    segments (a circular arc between two vertices) have no representation
    here - a straight-line-only simplification shared by GeoJSON itself,
    not something this mapping alone introduces.
    """
    coord_schema = _position_schema(line_coord_type(type_instance))
    position_array: dict[str, Any] = {"type": "array", "items": coord_schema}
    if getattr(type_instance, "Kind", None) in _SURFACE_LIKE_KINDS:
        schema: dict[str, Any] = {
            "type": "array",
            "items": position_array,
            "x-interlis-boundary-order": "outer-first",
        }
    else:
        schema = position_array
    return _wrap_multi(schema, getattr(type_instance, "Multi", None))


def _scalar_type_schema(kind: str | None, type_instance: MetaInstance | None) -> dict[str, Any] | None:
    """Return the scalar mapping for one (kind, instance) pair, or None if unmapped."""
    if kind == "NumType" and type_instance is not None:
        return _num_type_schema(type_instance)
    if kind == "TextType" and type_instance is not None:
        return _text_type_schema(type_instance)
    if kind == "EnumType" and type_instance is not None:
        return _enum_type_schema(type_instance)
    if kind == "BooleanType" and type_instance is not None:
        return {"type": "boolean"}
    if kind == "FormattedType" and type_instance is not None:
        return _formatted_type_schema(type_instance)
    if kind == "BlackboxType" and type_instance is not None:
        return _blackbox_type_schema(type_instance)
    if kind == "CoordType" and type_instance is not None:
        return _coord_type_schema(type_instance)
    if kind == "LineType" and type_instance is not None:
        return _line_type_schema(type_instance)
    return None


def _reference_type_schema(resolved: ResolvedAttribute) -> dict[str, Any]:
    """Plain `REFERENCE TO X` -> the OID value as `type: string`.

    Matches the XTF wire format, which transfers a reference as a
    `REF="<oid>"` XML attribute - never the referenced object inline
    (unlike a Class/STRUCTURE attribute's `$ref`). The declared
    target class and the `(EXTERNAL)` flag have no native JSON Schema
    equivalent - surfaced as informational markers instead of being
    silently dropped (RULE #5, same pattern as MultiValue's
    `x-interlis-ordered`). Reuses `reference_target_class`/
    `reference_external_status` directly (xtf.schema), no duplicated
    resolution logic.
    """
    schema: dict[str, Any] = {"type": "string"}
    target = reference_target_class(resolved)
    target_name = getattr(target, "Name", None) if target is not None else None
    if target_name:
        schema["x-interlis-reference-target"] = target_name
    if reference_external_status(resolved):
        schema["x-interlis-reference-external"] = True
    return schema


def _is_structure(type_instance: MetaInstance | None) -> bool:
    """True if a `type_kind == "Class"` resolution is genuine STRUCTURE nesting.

    `resolve_attribute` resolves BOTH a `Class(Kind=Structure)` attribute
    AND an embedded association role
    (`xtf.schema.embedded_roles_of` - always `Kind == "Class"`, a role
    points at a real class instance, never a structure) to the SAME
    `type_kind == "Class"`. `Kind` is the only signal that tells them
    apart - confirmed by `xtf/validate.py`'s own `_validate_resolved_attr`
    dispatch (`Kind == "Structure"` recurses into inline content;
    anything else expects a `REF`/OID, same as a plain `REFERENCE TO`).
    """
    return type_instance is not None and getattr(type_instance, "Kind", None) == "Structure"


def _class_ref_or_marker(class_instance: MetaInstance | None, ref_keys: dict[int, str]) -> dict[str, Any]:
    """Return a `$ref` to `class_instance`'s own $defs entry, or the unsupported marker.

    Only called for genuine STRUCTURE nesting (`_is_structure` already
    checked by the caller) - a Class not present in `ref_keys` means it
    wasn't reachable from the roots given to `model_to_json_schema` (e.g.
    an unresolved cross-model reference, no `--repo` given) - RULE #5,
    marked explicitly rather than emitting a dangling `$ref`.
    """
    if class_instance is not None:
        ref = ref_keys.get(id(class_instance))
        if ref is not None:
            return {"$ref": f"#/$defs/{ref}"}
    return {"x-interlis-unsupported": "Class"}


def _element_schema(kind: str | None, type_instance: MetaInstance | None, ref_keys: dict[int, str]) -> dict[str, Any]:
    """Return the schema for one "leaf" type - a plain attribute's type, or a MultiValue's BaseType.

    `ReferenceType`/non-structure `Class` here (a `BAG`/`LIST OF
    REFERENCE TO X` element or an embedded role reached via `MultiValue`
    - neither seen in the real corpus so far, and the latter never
    happens by construction of `embedded_roles_of`, but both
    grammatically/structurally possible) fall back to a bare
    `{"type": "string"}` - the richer `x-interlis-reference-*` markers
    need a full `ResolvedAttribute` (see `_reference_type_schema`, used
    instead for the direct-attribute case by `_attribute_schema`).
    """
    scalar = _scalar_type_schema(kind, type_instance)
    if scalar is not None:
        return scalar
    if kind == "Class" and _is_structure(type_instance):
        return _class_ref_or_marker(type_instance, ref_keys)
    if kind == "Class" and type_instance is not None:
        return {"type": "string"}
    if kind == "ReferenceType" and type_instance is not None:
        return {"type": "string"}
    return {"x-interlis-unsupported": kind or "unknown"}


def _multi_value_schema(multi_value: MetaInstance, ref_keys: dict[int, str]) -> dict[str, Any]:
    """`BAG {m..n} OF X` / `LIST {m..n} OF X` -> `type: array`.

    `items` reuses the same scalar/$ref dispatch as a plain attribute,
    applied to `MultiValue.BaseType` (the element type). `Ordered`
    (True=LIST, False=BAG) has no native JSON Schema equivalent - it is
    NOT the same as `uniqueItems` (a BAG still allows duplicates, it just
    doesn't order them) - surfaced as an informational
    `x-interlis-ordered` marker instead of being silently lost.
    """
    base = getattr(multi_value, "BaseType", None)
    base = base if isinstance(base, MetaInstance) else None
    base_kind = base._qualified_class.rsplit(".", 1)[-1] if base is not None else None
    schema: dict[str, Any] = {"type": "array", "items": _element_schema(base_kind, base, ref_keys)}

    mult = getattr(multi_value, "Multiplicity", None)
    if isinstance(mult, MetaInstance):
        min_raw = getattr(mult, "Min", None)
        max_raw = getattr(mult, "Max", None)
        if min_raw is not None:
            try:
                schema["minItems"] = int(min_raw)
            except (TypeError, ValueError):
                pass
        if max_raw is not None and max_raw != "*":
            try:
                schema["maxItems"] = int(max_raw)
            except (TypeError, ValueError):
                pass

    ordered = getattr(multi_value, "Ordered", None)
    if isinstance(ordered, bool):
        schema["x-interlis-ordered"] = ordered
    return schema


def _attribute_schema(resolved: ResolvedAttribute, ref_keys: dict[int, str]) -> dict[str, Any]:
    """Return the JSON Schema for one resolved attribute.

    An attribute whose type falls outside the mapped set (NumType/
    TextType/EnumType/BooleanType/FormattedType/BlackboxType/CoordType/
    LineType/ReferenceType/Class/MultiValue) is never silently dropped -
    it gets an explicit `x-interlis-unsupported` marker instead (RULE #5,
    see docs/jsonschema-conversion-strategy.md).

    `type_kind == "Class"` covers two DIFFERENT things (see `_is_structure`):
    genuine STRUCTURE nesting (`$ref`) vs. an embedded association
    role - which, like a plain `REFERENCE TO`, is
    transferred as a `REF`/OID, not inlined, so it reuses
    `_reference_type_schema` (`reference_target_class`/
    `reference_external_status` already handle a `Role`-typed `resolved`
    correctly, no separate code path needed).
    """
    if resolved.type_kind == "MultiValue" and resolved.type_instance is not None:
        return _multi_value_schema(resolved.type_instance, ref_keys)
    if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
        return _class_ref_or_marker(resolved.type_instance, ref_keys)
    if resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
        return _reference_type_schema(resolved)
    return _element_schema(resolved.type_kind, resolved.type_instance, ref_keys)


def _nested_class(resolved: ResolvedAttribute) -> MetaInstance | None:
    """Return the Class a structure/BAG/LIST-of-structure attribute points to, else None.

    Only genuine STRUCTURE nesting counts as "reachable" for `$defs`
    discovery (`_is_structure`) - an embedded association role's target
    is a REFERENCE, not containment, same as a plain `REFERENCE TO`
    target (also never added to `$defs` by this discovery).
    """
    if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
        return resolved.type_instance
    if resolved.type_kind == "MultiValue" and resolved.type_instance is not None:
        base = getattr(resolved.type_instance, "BaseType", None)
        if isinstance(base, MetaInstance) and base._qualified_class.rsplit(".", 1)[-1] == "Class" and _is_structure(base):
            return base
    return None


def _discover_classes(roots: list[MetaInstance]) -> dict[int, MetaInstance]:
    """BFS over every Class reachable from `roots` via structure/BAG/LIST attributes.

    Reachable classes not among `roots` (e.g. a STRUCTURE declared in an
    imported model, resolved via `--repo` but never registered in the
    local model's own SymbolTable) are discovered here rather than left
    unconverted. Dedups by Python identity, so a class is visited/
    converted at most once - self-referencing or mutually recursive
    structures terminate naturally (the second encounter is already in
    `found`, no re-enqueue), which is what makes emitting a plain `$ref`
    (rather than inlining) recursion-safe.
    """
    found: dict[int, MetaInstance] = {}
    queue: list[MetaInstance] = list(roots)
    while queue:
        cls = queue.pop(0)
        if id(cls) in found:
            continue
        found[id(cls)] = cls
        for attr in attributes_of(cls).values():
            nested = _nested_class(resolve_attribute(attr))
            if nested is not None and id(nested) not in found:
                queue.append(nested)
    return found


def _assign_keys(classes_by_id: dict[int, MetaInstance]) -> dict[int, str]:
    """Assign each discovered class a unique `$defs` key from its own `Name`."""
    keys: dict[int, str] = {}
    used: set[str] = set()
    for instance_id, cls in classes_by_id.items():
        base_name = getattr(cls, "Name", None) or "Unnamed"
        key = base_name
        suffix = 2
        while key in used:
            key = f"{base_name}_{suffix}"
            suffix += 1
        used.add(key)
        keys[instance_id] = key
    return keys


def class_to_json_schema(
    class_instance: MetaInstance, ref_keys: dict[int, str] | None = None, symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """Convert one IlisMeta16 Class (or Structure - same metaclass) instance.

    Own+inherited attributes: NumType/TextType/EnumType/BooleanType/
    FormattedType/BlackboxType/CoordType/LineType/plain `REFERENCE TO X`
    map to their respective scalar/string/array schema; a Class-typed
    (nested structure) or
    MultiValue-typed (BAG/LIST OF) attribute maps via `ref_keys` (instance
    id -> its own `$defs` key - normally supplied by
    `model_to_json_schema`, which discovers and assigns keys for every
    reachable class first). Called standalone with `ref_keys=None` (e.g.
    in a unit test), a nested Class-typed attribute falls back to the
    `x-interlis-unsupported` marker rather than crashing.

    `symbol_table`, when given, additionally includes EMBEDDED
    ASSOCIATION ROLES (`xtf.schema.schema_members_of` instead of plain
    `attributes_of`) as pseudo-attributes - mapped the SAME way as a
    plain `REFERENCE TO` (a `REF`/OID, never inlined - see
    `_is_structure`), since that's how the XTF wire format actually
    transfers them. `None` (the default) means own+inherited
    `ClassAttribute`s only, no embedded roles.

    Inheritance-as-oneOf, OID and formal constraints remain backlog
    regardless (see mappings/ilismeta16-to-jsonschema-rules.yml).
    """
    ref_keys = ref_keys or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    members = schema_members_of(class_instance, symbol_table) if symbol_table is not None else attributes_of(class_instance)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        properties[name] = _attribute_schema(resolved, ref_keys)
        if resolved.mandatory:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    class_name = getattr(class_instance, "Name", None)
    if class_name:
        schema["title"] = class_name
    if required:
        schema["required"] = sorted(required)
    return schema


def model_to_json_schema(classes: list[MetaInstance], symbol_table: SymbolTable | None = None) -> dict[str, Any]:
    """Convert every Class/Structure reachable from `classes` into one JSON Schema document.

    `classes` is typically every `Class`-kind instance from a built
    model's SymbolTable (`builder.symbol_table.all_registered()`,
    filtered by `_qualified_class` - see `cli.cmd_convert`) - the roots
    for `_discover_classes`, which also pulls in any nested structure
    reachable only via an attribute (e.g. imported from another model).
    Each discovered class becomes one `$defs` entry, keyed by its own
    `Name` (not a fully model-qualified name - a short-name collision
    across topics, while possible, is only disambiguated by an
    incrementing suffix, not a qualified key; revisit if qualified
    `$defs` keys turn out to be needed).

    `symbol_table`, when given, is forwarded to `class_to_json_schema`
    so each `$defs` entry also includes its embedded association roles -
    NOT used by `_discover_classes` itself (an embedded role is a
    reference, never containment, so it never contributes a new reachable
    class, same as a plain `REFERENCE TO` target).
    """
    reachable = _discover_classes(classes)
    ref_keys = _assign_keys(reachable)
    defs = {
        ref_keys[instance_id]: class_to_json_schema(cls, ref_keys, symbol_table)
        for instance_id, cls in reachable.items()
    }
    return {"$schema": JSON_SCHEMA_DRAFT, "$defs": defs}
