""".ili -> JSON Schema conversion (Lot 1: scalar types, Lot 2: STRUCTURE/BAG/LIST nesting, Lot 4: BooleanType, Lot 5: FormattedType/BlackboxType, Lot 6: plain REFERENCE TO).

See docs/jsonschema-conversion-strategy.md for the design decision and
mappings/ilismeta16-to-jsonschema-rules.yml /
spec/conversion/jsonschema-mapping.yml for the concept/field contract this
module implements. Walks IlisMeta16 instances already built by
InterlisModelBuilder, reusing xtf.schema's type-resolution helpers
(resolve_attribute/attributes_of/enum_values) rather than duplicating any
resolution logic - convert() is a decoupled stage from validate().
"""
from typing import Any

from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    enum_values,
    reference_external_status,
    reference_target_class,
    resolve_attribute,
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
    return None


def _reference_type_schema(resolved: ResolvedAttribute) -> dict[str, Any]:
    """Plain `REFERENCE TO X` -> the OID value as `type: string`.

    Matches the XTF wire format, which transfers a reference as a
    `REF="<oid>"` XML attribute - never the referenced object inline
    (unlike a Class/STRUCTURE attribute, Lot 2's `$ref`). The declared
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


def _class_ref_or_marker(class_instance: MetaInstance | None, ref_keys: dict[int, str]) -> dict[str, Any]:
    """Return a `$ref` to `class_instance`'s own $defs entry, or the unsupported marker.

    A Class not present in `ref_keys` means it wasn't reachable from the
    roots given to `model_to_json_schema` (e.g. an unresolved cross-model
    reference, no `--repo` given) - RULE #5, marked explicitly rather than
    emitting a dangling `$ref`.
    """
    if class_instance is not None:
        ref = ref_keys.get(id(class_instance))
        if ref is not None:
            return {"$ref": f"#/$defs/{ref}"}
    return {"x-interlis-unsupported": "Class"}


def _element_schema(kind: str | None, type_instance: MetaInstance | None, ref_keys: dict[int, str]) -> dict[str, Any]:
    """Return the schema for one "leaf" type - a plain attribute's type, or a MultiValue's BaseType.

    `ReferenceType` here (a `BAG`/`LIST OF REFERENCE TO X` element, not
    seen in the real corpus so far but grammatically legal) falls back to
    a bare `{"type": "string"}` - the richer `x-interlis-reference-*`
    markers need a full `ResolvedAttribute` (see `_reference_type_schema`,
    used instead for the direct-attribute case by `_attribute_schema`).
    """
    scalar = _scalar_type_schema(kind, type_instance)
    if scalar is not None:
        return scalar
    if kind == "Class":
        return _class_ref_or_marker(type_instance, ref_keys)
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
    TextType/EnumType/BooleanType/FormattedType/BlackboxType/
    ReferenceType/Class/MultiValue) is never silently dropped - it gets
    an explicit `x-interlis-unsupported` marker instead (RULE #5, see
    docs/jsonschema-conversion-strategy.md).
    """
    if resolved.type_kind == "MultiValue" and resolved.type_instance is not None:
        return _multi_value_schema(resolved.type_instance, ref_keys)
    if resolved.type_kind == "Class":
        return _class_ref_or_marker(resolved.type_instance, ref_keys)
    if resolved.type_kind == "ReferenceType" and resolved.type_instance is not None:
        return _reference_type_schema(resolved)
    return _element_schema(resolved.type_kind, resolved.type_instance, ref_keys)


def _nested_class(resolved: ResolvedAttribute) -> MetaInstance | None:
    """Return the Class a structure/BAG/LIST-of-structure attribute points to, else None."""
    if resolved.type_kind == "Class" and resolved.type_instance is not None:
        return resolved.type_instance
    if resolved.type_kind == "MultiValue" and resolved.type_instance is not None:
        base = getattr(resolved.type_instance, "BaseType", None)
        if isinstance(base, MetaInstance) and base._qualified_class.rsplit(".", 1)[-1] == "Class":
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


def class_to_json_schema(class_instance: MetaInstance, ref_keys: dict[int, str] | None = None) -> dict[str, Any]:
    """Convert one IlisMeta16 Class (or Structure - same metaclass) instance.

    Own+inherited attributes: NumType/TextType/EnumType map per Lot 1,
    BooleanType per Lot 4, FormattedType/BlackboxType per Lot 5, plain
    `REFERENCE TO X` per Lot 6 (embedded association roles remain
    backlog, see mappings/ilismeta16-to-jsonschema-rules.yml); a
    Class-typed (nested structure) or MultiValue-typed (BAG/LIST OF)
    attribute maps per Lot 2, via `ref_keys` (instance id -> its own
    `$defs` key - normally supplied by `model_to_json_schema`, which
    discovers and assigns keys for every reachable class first). Called
    standalone with `ref_keys=None` (e.g. in a unit test), a nested
    Class-typed attribute falls back to the `x-interlis-unsupported`
    marker rather than crashing - embedded association roles, geometry,
    inheritance-as-oneOf, OID and formal constraints remain backlog
    regardless (see mappings/ilismeta16-to-jsonschema-rules.yml).
    """
    ref_keys = ref_keys or {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, attr in attributes_of(class_instance).items():
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


def model_to_json_schema(classes: list[MetaInstance]) -> dict[str, Any]:
    """Convert every Class/Structure reachable from `classes` into one JSON Schema document.

    `classes` is typically every `Class`-kind instance from a built
    model's SymbolTable (`builder.symbol_table.all_registered()`,
    filtered by `_qualified_class` - see `cli.cmd_convert`) - the roots
    for `_discover_classes`, which also pulls in any nested structure
    reachable only via an attribute (e.g. imported from another model).
    Each discovered class becomes one `$defs` entry, keyed by its own
    `Name` (not a fully model-qualified name - a short-name collision
    across topics, while possible, is only disambiguated by an
    incrementing suffix, not a qualified key; revisit if a later lot
    needs qualified `$defs` keys).
    """
    reachable = _discover_classes(classes)
    ref_keys = _assign_keys(reachable)
    defs = {ref_keys[instance_id]: class_to_json_schema(cls, ref_keys) for instance_id, cls in reachable.items()}
    return {"$schema": JSON_SCHEMA_DRAFT, "$defs": defs}
