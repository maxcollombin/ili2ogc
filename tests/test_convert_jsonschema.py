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
