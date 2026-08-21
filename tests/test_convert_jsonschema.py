"""Lot 1 (backlog item 5) - .ili -> JSON Schema, scalar types only.

See docs/jsonschema-conversion-strategy.md for the design decision and
mappings/ilismeta16-to-jsonschema-rules.yml /
spec/conversion/jsonschema-mapping.yml for the field contract this module
implements.
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonschema import class_to_json_schema, model_to_json_schema
from interlis.runtime.parse import parse_text
from interlis.xtf.schema import attributes_of, resolve_attribute

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _resolved_class(builder, name: str):
    return builder.symbol_table.resolve(name)


def test_numtype_integer_range_gets_minimum_maximum():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Age : 0 .. 130;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Age"] == {"type": "integer", "minimum": 0, "maximum": 130}


def test_numtype_decimal_range_gets_number_type():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Height : 0.000 .. 999.999;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Height"] == {"type": "number", "minimum": 0.0, "maximum": 999.999}


def test_texttype_with_and_without_maxlength():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Code : TEXT*20;
      Note : TEXT;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Code"] == {"type": "string", "maxLength": 20}
    assert schema["properties"]["Note"] == {"type": "string"}


def test_enumtype_flat_and_nested_sorted_others_excluded():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Kategorie : (a, b(c, d), e);
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Kategorie"] == {
        "type": "string",
        "enum": ["a", "b", "b.c", "b.d", "e"],
    }


def test_booleantype_attribute():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Active : BOOLEAN;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Active"] == {"type": "boolean"}


def test_formattedtype_attribute_gets_string_type():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    Datum = FORMAT INTERLIS.XMLDate "1900-01-01" .. "2999-12-31";
  TOPIC T =
    CLASS A =
      Erstellt : Datum;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Erstellt"] == {"type": "string"}


def test_blackboxtype_attribute_surfaces_kind_marker():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Bild : BLACKBOX BINARY;
      Meta : BLACKBOX XML;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Bild"] == {"type": "string", "x-interlis-blackbox-kind": "Binary"}
    assert schema["properties"]["Meta"] == {"type": "string", "x-interlis-blackbox-kind": "Xml"}


def test_reference_to_attribute_gets_string_type_and_target_marker():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Item =
      Name : TEXT*20;
    END Item;
    CLASS A =
      Ref : REFERENCE TO Item;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Ref"] == {"type": "string", "x-interlis-reference-target": "Item"}


def test_reference_to_external_attribute_surfaces_external_marker():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Item =
      Name : TEXT*20;
    END Item;
    CLASS A =
      Ref : MANDATORY REFERENCE TO (EXTERNAL) Item;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Ref"] == {
        "type": "string",
        "x-interlis-reference-target": "Item",
        "x-interlis-reference-external": True,
    }


_ASSOCIATION_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Location =
      Name : TEXT*20;
    END Location;
    CLASS Indicator =
      Value : TEXT*20;
    END Indicator;
    ASSOCIATION Location_Indicator =
      rLocation (EXTERNAL) -<#> Location;
      rIndicator -- {0..*} Indicator;
    END Location_Indicator;
  END T;
END Foo.
"""


def test_embedded_role_absent_without_symbol_table():
    builder = _build(_ASSOCIATION_MODEL)
    cls = _resolved_class(builder, "Indicator")
    schema = class_to_json_schema(cls)
    assert set(schema["properties"]) == {"Value"}


def test_embedded_role_gets_reference_schema_with_symbol_table():
    builder = _build(_ASSOCIATION_MODEL)
    cls = _resolved_class(builder, "Indicator")
    schema = class_to_json_schema(cls, symbol_table=builder.symbol_table)
    assert schema["properties"]["rLocation"] == {
        "type": "string",
        "x-interlis-reference-target": "Location",
        "x-interlis-reference-external": True,
    }
    assert schema["properties"]["Value"] == {"type": "string", "maxLength": 20}
    # rIndicator (the {0..*} side) is never embedded on ITS OWN target
    # (Indicator) - eCH-0031 SS4.3.9, confirmed by xtf.schema.embedded_roles_of.
    assert "rIndicator" not in schema["properties"]


def test_embedded_role_target_never_gets_a_defs_entry():
    """An embedded role is a REFERENCE (REF/OID), not containment - its
    target must not be pulled into $defs the way a STRUCTURE would be
    (same as a plain REFERENCE TO target, Lot 6)."""
    builder = _build(_ASSOCIATION_MODEL)
    classes = [
        instance for instance in builder.symbol_table.all_registered()
        if hasattr(instance, "_qualified_class") and instance._qualified_class.rsplit(".", 1)[-1] == "Class"
        and getattr(instance, "Kind", None) == "Class"
    ]
    schema = model_to_json_schema(classes, symbol_table=builder.symbol_table)
    assert schema["$defs"]["Indicator"]["properties"]["rLocation"] == {
        "type": "string",
        "x-interlis-reference-target": "Location",
        "x-interlis-reference-external": True,
    }
    assert "Location_Indicator" not in schema["$defs"]  # the ASSOCIATION class itself, never a root here


def test_mandatory_attribute_is_required():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Id : MANDATORY 0 .. 999;
      Opt : 0 .. 999;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["required"] == ["Id"]
    assert "properties" in schema and set(schema["properties"]) == {"Id", "Opt"}


def test_coordtype_attribute_gets_unsupported_marker_not_dropped():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    Coord2D = COORD 0.000 .. 1000.000, 0.000 .. 1000.000;
  TOPIC T =
    CLASS A =
      Position : Coord2D;
      Label : TEXT*10;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["properties"]["Position"] == {"x-interlis-unsupported": "CoordType"}
    assert schema["properties"]["Label"] == {"type": "string", "maxLength": 10}


def test_class_schema_has_title_and_object_type():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Code : TEXT*20;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    schema = class_to_json_schema(cls)
    assert schema["type"] == "object"
    assert schema["title"] == "A"


def test_model_to_json_schema_collects_defs_by_name():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Code : TEXT*20;
    END A;
    CLASS B =
      Age : 0 .. 130;
    END B;
  END T;
END Foo.
"""
    )
    classes = [_resolved_class(builder, "A"), _resolved_class(builder, "B")]
    doc = model_to_json_schema(classes)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(doc["$defs"]) == {"A", "B"}
    assert doc["$defs"]["A"]["properties"]["Code"]["type"] == "string"


def test_structure_attribute_gets_ref_when_target_reachable():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Sub =
      Value : TEXT*10;
    END Sub;
    CLASS A =
      Position : Sub;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    sub = _resolved_class(builder, "Sub")
    doc = model_to_json_schema([a, sub])
    assert set(doc["$defs"]) == {"A", "Sub"}
    assert doc["$defs"]["A"]["properties"]["Position"] == {"$ref": "#/$defs/Sub"}
    assert doc["$defs"]["Sub"]["properties"]["Value"] == {"type": "string", "maxLength": 10}


def test_structure_attribute_discovered_even_when_not_in_given_roots():
    # model_to_json_schema must pull in a nested Structure even if the
    # caller only passed the top-level Class (mirrors a cross-model
    # reference resolved via --repo but never locally registered).
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Sub =
      Value : TEXT*10;
    END Sub;
    CLASS A =
      Position : Sub;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    doc = model_to_json_schema([a])
    assert set(doc["$defs"]) == {"A", "Sub"}
    assert doc["$defs"]["A"]["properties"]["Position"] == {"$ref": "#/$defs/Sub"}


def test_structure_attribute_standalone_without_ref_keys_gets_marker():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Sub =
      Value : TEXT*10;
    END Sub;
    CLASS A =
      Position : Sub;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    schema = class_to_json_schema(a)
    assert schema["properties"]["Position"] == {"x-interlis-unsupported": "Class"}


def test_bag_of_structure_gets_array_of_ref_and_ordered_marker():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Sub =
      Value : TEXT*10;
    END Sub;
    CLASS A =
      Many : BAG {0..*} OF Sub;
      Ordered : LIST {1..5} OF Sub;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    sub = _resolved_class(builder, "Sub")
    doc = model_to_json_schema([a, sub])
    props = doc["$defs"]["A"]["properties"]
    assert props["Many"] == {
        "type": "array", "items": {"$ref": "#/$defs/Sub"}, "minItems": 0, "x-interlis-ordered": False,
    }
    assert props["Ordered"] == {
        "type": "array", "items": {"$ref": "#/$defs/Sub"},
        "minItems": 1, "maxItems": 5, "x-interlis-ordered": True,
    }


def test_list_of_scalar_type():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Tags : LIST {1..3} OF TEXT*5;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    schema = class_to_json_schema(a)
    assert schema["properties"]["Tags"] == {
        "type": "array",
        "items": {"type": "string", "maxLength": 5},
        "minItems": 1, "maxItems": 3, "x-interlis-ordered": True,
    }


def test_recursive_structure_produces_ref_not_infinite_expansion():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    STRUCTURE Node =
      Label : TEXT*10;
      Children : BAG {0..*} OF Node;
    END Node;
    CLASS A =
      Root : Node;
    END A;
  END T;
END Foo.
"""
    )
    a = _resolved_class(builder, "A")
    doc = model_to_json_schema([a])
    assert set(doc["$defs"]) == {"A", "Node"}
    assert doc["$defs"]["A"]["properties"]["Root"] == {"$ref": "#/$defs/Node"}
    assert doc["$defs"]["Node"]["properties"]["Children"] == {
        "type": "array", "items": {"$ref": "#/$defs/Node"}, "minItems": 0, "x-interlis-ordered": False,
    }


def test_attributes_of_and_resolve_attribute_are_reused_not_duplicated():
    # Sanity check that convert.jsonschema is layered on top of
    # xtf.schema's existing resolution, not a parallel implementation.
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Age : 0 .. 130;
    END A;
  END T;
END Foo.
"""
    )
    cls = _resolved_class(builder, "A")
    attrs = attributes_of(cls)
    resolved = resolve_attribute(attrs["Age"])
    assert resolved.type_kind == "NumType"
