""".ili -> JSON Schema conversion (Lot 1: scalar types only).

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
from interlis.xtf.schema import ResolvedAttribute, attributes_of, enum_values, resolve_attribute

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


def _enum_type_schema(type_instance: MetaInstance) -> dict[str, Any]:
    # "OTHERS" is a transfer-time escape valid for any INTERLIS enum
    # (eCH-0031 SS4.3.11.3), not a declared domain value - excluded here,
    # see docs/jsonschema-conversion-strategy.md.
    values = enum_values(type_instance) - {"OTHERS"}
    return {"type": "string", "enum": sorted(values)}


def _attribute_schema(resolved: ResolvedAttribute) -> dict[str, Any]:
    """Return the JSON Schema for one resolved attribute.

    An attribute whose type falls outside Lot 1's mapped set (NumType/
    TextType/EnumType) is never silently dropped - it gets an explicit
    `x-interlis-unsupported` marker instead (RULE #5, see
    docs/jsonschema-conversion-strategy.md).
    """
    if resolved.type_kind == "NumType" and resolved.type_instance is not None:
        return _num_type_schema(resolved.type_instance)
    if resolved.type_kind == "TextType" and resolved.type_instance is not None:
        return _text_type_schema(resolved.type_instance)
    if resolved.type_kind == "EnumType" and resolved.type_instance is not None:
        return _enum_type_schema(resolved.type_instance)
    return {"x-interlis-unsupported": resolved.type_kind or "unknown"}


def class_to_json_schema(class_instance: MetaInstance) -> dict[str, Any]:
    """Convert one IlisMeta16 Class (or Structure - same metaclass) instance.

    Own+inherited SCALAR attributes only (Lot 1 scope) - STRUCTURE/BAG
    nesting, associations/REF, geometry, and inheritance-as-oneOf are
    backlog (see mappings/ilismeta16-to-jsonschema-rules.yml).
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, attr in attributes_of(class_instance).items():
        resolved = resolve_attribute(attr)
        properties[name] = _attribute_schema(resolved)
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
    """Convert every given Class/Structure instance into one JSON Schema document.

    `classes` is typically every `Class`-kind instance from a built model's
    SymbolTable (`builder.symbol_table.all_registered()`, filtered by
    `_qualified_class` - see `cli.cmd_convert`). Each class becomes one
    `$defs` entry, keyed by its own `Name` (not a fully model-qualified
    name - no cross-model $ref resolution exists yet in Lot 1, so a
    short-name collision across topics, while possible, isn't disambiguated
    here; revisit if/when a later lot needs qualified $defs keys).
    """
    defs: dict[str, Any] = {}
    for class_instance in classes:
        class_name = getattr(class_instance, "Name", None)
        if not class_name:
            continue
        key = class_name
        suffix = 2
        while key in defs:
            key = f"{class_name}_{suffix}"
            suffix += 1
        defs[key] = class_to_json_schema(class_instance)
    return {"$schema": JSON_SCHEMA_DRAFT, "$defs": defs}
