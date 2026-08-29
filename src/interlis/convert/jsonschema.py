""".ili -> JSON Schema conversion: scalar types, STRUCTURE/BAG/LIST nesting, plain REFERENCE TO, embedded association roles, and ABSTRACT structure polymorphism.

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

import jsonschema

from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    coord_axes,
    enum_values,
    is_class_compatible,
    line_coord_type,
    reference_external_status,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

JSON_SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"


def _meta_marker(instance: MetaInstance | None) -> dict[str, str]:
    """Return `instance`'s eCH-0117 `!!@Name=Value` meta-attributes as a plain `Name -> Value` dict, or `{}`.

    See docs/ech-0117-meta-attributes.md - a `MetaAttribute` is only ever
    present when the caller built the model with `meta_attributes=...`
    (`InterlisModelBuilder.build`); otherwise `instance.MetaAttribute` is
    simply absent/empty, so this degrades to `{}` with no special-casing
    needed. A duplicate `Name` on the SAME construct (not seen in the real
    corpus so far) would have the later one win - consistent with this
    codebase's existing stance on unproven collisions elsewhere (e.g.
    `convert/jsonfg.py`'s JOIN attribute pooling).
    """
    return {
        m.Name: m.Value
        for m in (getattr(instance, "MetaAttribute", None) or [])
        if isinstance(m, MetaInstance) and getattr(m, "Name", None) is not None
    }


_FULL_CRUD = ("GET", "POST", "PUT", "PATCH", "DELETE")
_READ_ONLY = ("GET",)


def _crud_operations(class_instance: MetaInstance) -> tuple[str, ...] | None:
    """Return the HTTP operations a future OGC API Features publication of `class_instance` could support, or `None`.

    `None` for `Kind in ("Structure", "Association")` - nested/embedded
    content is never its own collection, CRUD semantics don't apply.
    `Kind == "View"` with `FormationKind == "Join"`: GET-only - see
    .claude/PROGRESS.md item 8 Lot D's decision, by direct analogy with
    SQL view updatability (the report's own pipeline, chapter 6 step 2a,
    materializes `JOIN OF` as a real multi-table database VIEW via
    `ili2db` - PostgreSQL/PostGIS, GeoPackage and ESRI FileGDB all require
    a SINGLE source relation, or `INSTEAD OF` triggers INTERLIS provides
    no metadata to generate, for a view to be automatically updatable).
    Every other case - a plain `Class`, or a `PROJECTION OF` View (1:1
    with its single base, the direct equivalent of a single-table SQL
    view) - gets full CRUD: OGC API Features - Part 4 doesn't require
    every collection to be writable, a provider declares per-collection
    which HTTP methods it supports (unsupported ones simply answer 405).
    """
    kind = getattr(class_instance, "Kind", None)
    if kind not in ("Class", "View"):
        return None
    if kind == "View" and getattr(class_instance, "FormationKind", None) == "Join":
        return _READ_ONLY
    return _FULL_CRUD


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


# eCH-0031 SS3.2.2 "Namen": Name = Letter {Letter|Digit|'_'} (255 chars max) -
# and SS3.8.1: `NAME (FINAL) = TEXT*255` / `URI (FINAL) = TEXT*1023` (RFC 2396).
# Both bounds are exact per the grammar's own alternative (`TextType = ...
# | 'NAME' | 'URI'` - neither carries a `MaxLength-PosNumber`, so
# `TextType.MaxLength` is never populated for them and is supplied here
# instead), so `pattern`/`format`/`maxLength` are faithful, not approximated.
_INTERLIS_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,254}$"


def _text_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    kind = getattr(type_instance, "Kind", None)
    if kind == "Name":
        schema["pattern"] = _INTERLIS_NAME_PATTERN
        schema["maxLength"] = 255
        return schema
    if kind == "Uri":
        schema["format"] = "uri"
        schema["maxLength"] = 1023
        return schema
    max_length = getattr(type_instance, "MaxLength", None)
    if max_length is not None:
        try:
            schema["maxLength"] = int(max_length)
        except (TypeError, ValueError):
            pass
    return schema


# eCH-0118 ("Regles de codification GML pour INTERLIS") SS6.15.5 singles
# out exactly these 3 well-known format names for a specific target type
# (xsd:date/time/dateTime) - every OTHER (custom/STRUCT-based) format
# stays a plain string, same as before. Values are stored bare (e.g.
# "XMLDate", confirmed empirically - the "INTERLIS." qualifier is not
# part of FormattedType.Format's own text). JSON Schema 2020-12's
# "format" keyword (SS7.3.1) has the exact same 3-way vocabulary.
_KNOWN_FORMAT_TO_JSON_FORMAT = {"XMLDate": "date", "XMLTime": "time", "XMLDateTime": "date-time"}


def _formatted_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """FormattedType always describes a formatted TEXT value - `type: string`.

    `Format` names a predefined/custom format (e.g. `INTERLIS.XMLDate`) or
    a STRUCT-based template - no regex `pattern` is derived from it (see
    docs/jsonschema-conversion-strategy.md), but the 3 recognized
    date/time format names get the matching JSON Schema `format` hint
    (see docs/ech-0118-gml-mapping-analysis.md, finding 1) - additive,
    `type: string` alone still covers every legal value regardless.
    """
    schema: dict[str, Any] = {"type": "string"}
    json_format = _KNOWN_FORMAT_TO_JSON_FORMAT.get(getattr(type_instance, "Format", None))
    if json_format is not None:
        schema["format"] = json_format
    return schema


def _blackbox_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    """BlackboxType (XML or BINARY payload) -> `type: string`.

    `Kind` ("Xml"/"Binary") is surfaced as an informational
    `x-blackbox-kind` marker instead of being silently dropped
    (RULE #5, same pattern as MultiValue's `x-ordered`). The
    BINARY variant additionally gets `contentEncoding: "base64"` (JSON
    Schema 2020-12 SS8.3) - matching eCH-0118 SS6.15.6, which maps BINARY
    specifically to `xsd:base64Binary` (see
    docs/ech-0118-gml-mapping-analysis.md, finding 1). XML has no
    equivalent native JSON Schema content-shape keyword, stays plain
    `string`.
    """
    schema: dict[str, Any] = {"type": "string"}
    kind = getattr(type_instance, "Kind", None)
    if kind is not None:
        schema["x-blackbox-kind"] = kind
    if kind == "Binary":
        schema["contentEncoding"] = "base64"
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
    informational `x-boundary-order` marker (RULE #5). ARC
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
            "x-boundary-order": "outer-first",
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
    `x-ordered`). Reuses `reference_target_class`/
    `reference_external_status` directly (xtf.schema), no duplicated
    resolution logic.
    """
    schema: dict[str, Any] = {"type": "string"}
    target = reference_target_class(resolved)
    target_name = getattr(target, "Name", None) if target is not None else None
    if target_name:
        schema["x-reference-target"] = target_name
    if reference_external_status(resolved):
        schema["x-reference-external"] = True
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


def _concrete_subclasses(abstract_class: MetaInstance, symbol_table: SymbolTable) -> list[MetaInstance]:
    """Every concrete (non-abstract) STRUCTURE subclass of an ABSTRACT one.

    `Inheritance`/`Super` is a forward-only pointer (child -> parent, see
    `xtf.schema.attributes_of`/`is_class_compatible`) - there is no reverse
    "subclasses of" link, so finding them means scanning every registered
    Class and keeping the ones for which `is_class_compatible` holds.
    Abstract intermediates are excluded: eCH-0031 SS3.6.4
    ("Strukturattribute") - only concrete structures (or their own further
    concrete extensions) are valid transferred structure elements, an
    abstract one never is.
    """
    seen: set[int] = set()
    result: list[MetaInstance] = []
    for candidate in symbol_table.all_registered():
        if not isinstance(candidate, MetaInstance) or candidate._qualified_class.rsplit(".", 1)[-1] != "Class":
            continue
        if candidate is abstract_class or id(candidate) in seen:
            continue
        if getattr(candidate, "Kind", None) != "Structure" or bool(getattr(candidate, "Abstract", False)):
            continue
        if is_class_compatible(candidate, abstract_class):
            seen.add(id(candidate))
            result.append(candidate)
    return result


def _class_ref_or_marker(
    class_instance: MetaInstance | None, ref_keys: dict[int, str], symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """Return a `$ref` to `class_instance`'s own $defs entry, or the unsupported marker.

    Only called for genuine STRUCTURE nesting (`_is_structure` already
    checked by the caller) - a Class not present in `ref_keys` means it
    wasn't reachable from the roots given to `model_to_json_schema` (e.g.
    an unresolved cross-model reference, no `--repo` given) - RULE #5,
    marked explicitly rather than emitting a dangling `$ref`.

    An ABSTRACT structure (e.g. real `CHBase_Part8_GEOMETRY3D_V2.ili`:
    `Curve3D (ABSTRACT)`/`Surface3D (ABSTRACT)`, used via `BAG`/`LIST OF`)
    is transferred on the wire as one of its concrete subclasses - the XML
    encoding's own element tag IS the concrete structure's name (eCH-0031
    SS4.3.11.12), so a single `$ref` to the abstract class's own (often
    empty) schema would misrepresent the actual possible shapes. Resolved
    instead as `anyOf` over every concrete subclass found in
    `symbol_table` (needs the symbol table to enumerate them - without one,
    or when none is found, this falls back to the plain `$ref`). `anyOf`
    rather than `oneOf`: a concrete subclass can itself be extended by
    ANOTHER concrete subclass (real case: `Surface3D` -> `PlanarSurface3D`
    -> `Triangle3D`, both concrete) - a `Triangle3D` value would then
    validate against both branches (its extra own attributes are not
    forbidden by `PlanarSurface3D`'s schema, which has no
    `additionalProperties: false`), which `oneOf`'s "exactly one match"
    requirement cannot tolerate. The plain-`$ref` fallback (no
    `symbol_table`, or no concrete subclass found in it) still carries an
    informational `x-abstract` marker (RULE #5) whenever
    `class_instance` is ABSTRACT - never silently indistinguishable from a
    concrete structure's `$ref`.
    """
    if class_instance is not None:
        if symbol_table is not None and bool(getattr(class_instance, "Abstract", False)):
            concrete = _concrete_subclasses(class_instance, symbol_table)
            refs = sorted(ref_keys[id(c)] for c in concrete if id(c) in ref_keys)
            if refs:
                return {"anyOf": [{"$ref": f"#/$defs/{r}"} for r in refs]}
        ref = ref_keys.get(id(class_instance))
        if ref is not None:
            schema: dict[str, Any] = {"$ref": f"#/$defs/{ref}"}
            if bool(getattr(class_instance, "Abstract", False)):
                schema["x-abstract"] = True
            return schema
    return {"x-unsupported": "Class"}


def _element_schema(
    kind: str | None, type_instance: MetaInstance | None, ref_keys: dict[int, str], symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """Return the schema for one "leaf" type - a plain attribute's type, or a MultiValue's BaseType.

    `ReferenceType`/non-structure `Class` here (a `BAG`/`LIST OF
    REFERENCE TO X` element or an embedded role reached via `MultiValue`
    - neither seen in the real corpus so far, and the latter never
    happens by construction of `embedded_roles_of`, but both
    grammatically/structurally possible) fall back to a bare
    `{"type": "string"}` - the richer `x-reference-*` markers
    need a full `ResolvedAttribute` (see `_reference_type_schema`, used
    instead for the direct-attribute case by `_attribute_schema`).
    """
    scalar = _scalar_type_schema(kind, type_instance)
    if scalar is not None:
        return scalar
    if kind == "Class" and _is_structure(type_instance):
        return _class_ref_or_marker(type_instance, ref_keys, symbol_table)
    if kind == "Class" and type_instance is not None:
        return {"type": "string"}
    if kind == "ReferenceType" and type_instance is not None:
        return {"type": "string"}
    return {"x-unsupported": kind or "unknown"}


def _multi_value_schema(
    multi_value: MetaInstance, ref_keys: dict[int, str], symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """`BAG {m..n} OF X` / `LIST {m..n} OF X` -> `type: array`.

    `items` reuses the same scalar/$ref dispatch as a plain attribute,
    applied to `MultiValue.BaseType` (the element type). `Ordered`
    (True=LIST, False=BAG) has no native JSON Schema equivalent - it is
    NOT the same as `uniqueItems` (a BAG still allows duplicates, it just
    doesn't order them) - surfaced as an informational
    `x-ordered` marker instead of being silently lost.
    """
    base = getattr(multi_value, "BaseType", None)
    base = base if isinstance(base, MetaInstance) else None
    base_kind = base._qualified_class.rsplit(".", 1)[-1] if base is not None else None
    schema: dict[str, Any] = {"type": "array", "items": _element_schema(base_kind, base, ref_keys, symbol_table)}

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
        schema["x-ordered"] = ordered
    return schema


def _attribute_schema(
    resolved: ResolvedAttribute, ref_keys: dict[int, str], symbol_table: SymbolTable | None = None,
) -> dict[str, Any]:
    """Return the JSON Schema for one resolved attribute.

    An attribute whose type falls outside the mapped set (NumType/
    TextType/EnumType/BooleanType/FormattedType/BlackboxType/CoordType/
    LineType/ReferenceType/Class/MultiValue) is never silently dropped -
    it gets an explicit `x-unsupported` marker instead (RULE #5,
    see docs/jsonschema-conversion-strategy.md).

    `type_kind == "Class"` covers two DIFFERENT things (see `_is_structure`):
    genuine STRUCTURE nesting (`$ref`/`anyOf` if ABSTRACT, see
    `_class_ref_or_marker`) vs. an embedded association
    role - which, like a plain `REFERENCE TO`, is
    transferred as a `REF`/OID, not inlined, so it reuses
    `_reference_type_schema` (`reference_target_class`/
    `reference_external_status` already handle a `Role`-typed `resolved`
    correctly, no separate code path needed).

    eCH-0117 meta-attributes (`_meta_marker`) on the attribute's OWN
    `AttrOrParam`/`Role` are surfaced as `x-meta` regardless of
    which branch below produced the schema - real corpus evidence:
    `!!@basketRef=...` lands on the FIRST attribute right after `CLASS
    Datenbestand =` (this project's "first following construct"
    attachment, not the CLASS itself - see docs/ech-0117-meta-attributes.md),
    confirmed empirically on `ili_corpus/Zones_reservees_V1_1_o1.ili`.
    """
    if resolved.type_kind == "MultiValue" and resolved.type_instance is not None:
        schema = _multi_value_schema(resolved.type_instance, ref_keys, symbol_table)
    elif resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
        schema = _class_ref_or_marker(resolved.type_instance, ref_keys, symbol_table)
    elif resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
        schema = _reference_type_schema(resolved)
    else:
        schema = _element_schema(resolved.type_kind, resolved.type_instance, ref_keys, symbol_table)
    meta = _meta_marker(resolved.attr)
    if meta:
        schema["x-meta"] = meta
    return schema


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


def _discover_classes(roots: list[MetaInstance], symbol_table: SymbolTable | None = None) -> dict[int, MetaInstance]:
    """BFS over every Class reachable from `roots` via structure/BAG/LIST attributes.

    Reachable classes not among `roots` (e.g. a STRUCTURE declared in an
    imported model, resolved via `--repo` but never registered in the
    local model's own SymbolTable) are discovered here rather than left
    unconverted. Dedups by Python identity, so a class is visited/
    converted at most once - self-referencing or mutually recursive
    structures terminate naturally (the second encounter is already in
    `found`, no re-enqueue), which is what makes emitting a plain `$ref`
    (rather than inlining) recursion-safe.

    An ABSTRACT structure encountered along the way (`symbol_table`
    given) also enqueues its concrete subclasses (`_concrete_subclasses`)
    - needed so `_class_ref_or_marker`'s `anyOf` branches always resolve
    to a real `$defs` entry, even for a concrete subclass never directly
    named by any attribute (only reachable as "one of the possible
    shapes" of the abstract type).
    """
    found: dict[int, MetaInstance] = {}
    queue: list[MetaInstance] = list(roots)
    while queue:
        cls = queue.pop(0)
        if id(cls) in found:
            continue
        found[id(cls)] = cls
        if symbol_table is not None and getattr(cls, "Kind", None) == "Structure" and bool(getattr(cls, "Abstract", False)):
            for concrete in _concrete_subclasses(cls, symbol_table):
                if id(concrete) not in found:
                    queue.append(concrete)
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
    `x-unsupported` marker rather than crashing.

    `symbol_table`, when given, additionally includes EMBEDDED
    ASSOCIATION ROLES (`xtf.schema.schema_members_of` instead of plain
    `attributes_of`) as pseudo-attributes - mapped the SAME way as a
    plain `REFERENCE TO` (a `REF`/OID, never inlined - see
    `_is_structure`), since that's how the XTF wire format actually
    transfers them. `None` (the default) means own+inherited
    `ClassAttribute`s only, no embedded roles.

    ABSTRACT structure polymorphism (`anyOf` over concrete subclasses, see
    `_class_ref_or_marker`) is included when `symbol_table` is given.
    RESTRICTION-narrowed structure attributes, OID and formal constraints
    remain backlog regardless (see mappings/ilismeta16-to-jsonschema-rules.yml).

    eCH-0117 meta-attributes (`_meta_marker`) attached directly to
    `class_instance` itself (as opposed to one of its attributes, see
    `_attribute_schema`) are surfaced as a top-level `x-meta` -
    no real corpus evidence of a CLASS-level meta-attribute has been found
    so far (MODEL/DOMAIN/CONSTRAINT/ATTRIBUTE are the confirmed real
    attachment points, see docs/ech-0117-meta-attributes.md), but the
    mechanism is generic (any `MetaElement`, `Class` included) and this
    costs nothing extra to support - verified by a synthetic fixture
    rather than real-corpus proof for this specific branch.

    `x-crud` (`_crud_operations`, backlog item 8 Lot D) declares
    which HTTP operations a future OGC API Features publication of this
    `$defs` entry could support - `["GET"]` for a `JOIN OF` View,
    otherwise the full CRUD set. Omitted entirely for `Kind in
    ("Structure", "Association")` (nested content, never its own
    collection).
    """
    ref_keys = ref_keys or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    members = schema_members_of(class_instance, symbol_table) if symbol_table is not None else attributes_of(class_instance)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        properties[name] = _attribute_schema(resolved, ref_keys, symbol_table)
        if resolved.mandatory:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    class_name = getattr(class_instance, "Name", None)
    if class_name:
        schema["title"] = class_name
    if required:
        schema["required"] = sorted(required)
    class_meta = _meta_marker(class_instance)
    if class_meta:
        schema["x-meta"] = class_meta
    crud = _crud_operations(class_instance)
    if crud is not None:
        schema["x-crud"] = list(crud)
    return schema


def validate_feature_properties(properties: dict[str, Any], schema: dict[str, Any], key: str) -> list[str]:
    """Validate a candidate PUT/PATCH `properties` payload against one `$defs` entry of a `model_to_json_schema` document.

    Backlog item 8 Lot D ("CRUD PUT-PATCH-DELETE, validation via le même
    JSON Schema") - the building block a future write-capable OGC API
    Features layer (pygeoapi or otherwise, not implemented in this
    project - see .claude/PROGRESS.md item 8) would call before accepting
    a write. Uses the standard `jsonschema` package rather than
    reimplementing JSON Schema semantics (type/format/pattern/anyOf/
    required/`$ref` resolution...) by hand - this runtime's own output
    already conforms to draft 2020-12, so there is a real validator to
    reuse rather than a problem to re-solve.

    `schema` is a FULL `model_to_json_schema` document - its `$defs` is
    needed to resolve the `$ref`s a single `class_to_json_schema` entry
    contains (e.g. a nested STRUCTURE attribute), which can't resolve on
    their own without the sibling `$defs` they were generated alongside.
    `key` is the `$defs` key to validate against (the same key
    `model_to_json_schema`/`_assign_keys` assigned - typically the class/
    View's own `Name`, see `class_to_json_schema`'s `title`).

    Returns every violation's human-readable message (empty list = valid)
    rather than raising on the first one - a caller building an HTTP 400
    response benefits from the full list, not just one error at a time.
    Does NOT check `x-crud`/whether this write is even allowed
    for this collection (a JOIN OF View) - that's `_crud_operations`'s
    job, a separate concern from payload SHAPE validation.
    """
    root = {"$schema": schema.get("$schema", JSON_SCHEMA_DRAFT), "$defs": schema.get("$defs", {}), "$ref": f"#/$defs/{key}"}
    validator = jsonschema.Draft202012Validator(root)
    return [error.message for error in validator.iter_errors(properties)]


def model_to_json_schema(
    classes: list[MetaInstance], symbol_table: SymbolTable | None = None, model: MetaInstance | None = None,
) -> dict[str, Any]:
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

    `symbol_table`, when given, is forwarded to `class_to_json_schema` so
    each `$defs` entry also includes its embedded association roles, AND
    to `_discover_classes` so an ABSTRACT structure's concrete subclasses
    are pulled in too (needed for `_class_ref_or_marker`'s `anyOf`
    branches to resolve) - an embedded role itself still contributes no
    new reachable class (a reference, never containment, same as a plain
    `REFERENCE TO` target).

    `model`, when given, is the built root `Model` instance (`builder.build(tree,
    meta_attributes=...)`'s own return value for a single-MODEL file, or
    `builder.symbol_table.resolve(<Model-Name>)`) - its eCH-0117
    meta-attributes (`technicalContact`/`furtherInformation`/`IDGeoIV`,
    by far the most common real attachment point, see
    docs/ech-0117-meta-attributes.md) are surfaced as a top-level
    `"x-meta"`, the same mechanism already used at class/attribute level
    (`_meta_marker`). `None` (the default): omitted entirely, unchanged
    from before this parameter existed.
    """
    reachable = _discover_classes(classes, symbol_table)
    ref_keys = _assign_keys(reachable)
    defs = {
        ref_keys[instance_id]: class_to_json_schema(cls, ref_keys, symbol_table)
        for instance_id, cls in reachable.items()
    }
    document: dict[str, Any] = {"$schema": JSON_SCHEMA_DRAFT}
    model_meta = _meta_marker(model) if model is not None else {}
    if model_meta:
        document["x-meta"] = model_meta
    document["$defs"] = defs
    return document


def collect_diagnostics(document: dict, *, file: str | None = None):
    """Walk a built JSON Schema for `x-unsupported` markers and emit a `Diagnostic` per marker.

    The marker stays in the document (a downstream reader still sees the
    gap); this is the parallel machine-readable signal that feeds
    `--output-format sarif` and the exit code. `{"x-unsupported": "Class"}`
    is a missing-input case (a nested structure not reachable from the
    roots - class C); every other value is an unmapped type (class A).
    """
    from interlis.diagnostics import Diagnostic, Location

    out: list[Diagnostic] = []

    def _walk(node, path: str) -> None:
        if isinstance(node, dict):
            marker = node.get("x-unsupported")
            if marker is not None:
                if marker == "Class":
                    rule, sev = "JSONSCHEMA-CLASS-UNRESOLVED", "warning"
                    msg = f"{path or '<root>'}: a nested-structure Class is not reachable from the given roots"
                    hlp = "pass its model's directory to --repo"
                else:
                    rule, sev = "JSONSCHEMA-TYPE-UNSUPPORTED", "note"
                    msg = f"{path or '<root>'}: attribute type {marker!r} is outside the mapped set (x-unsupported)"
                    hlp = None
                out.append(Diagnostic(sev, rule, msg, Location(file=file, element_path=path or None), help=hlp))
            for key, value in node.items():
                if key.startswith("x-"):
                    continue
                _walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                _walk(item, f"{path}[{i}]")

    _walk(document, "")
    return out
