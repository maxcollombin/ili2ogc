"""eCH-0117 meta-attributes surfaced as `x-meta` in JSON Schema output.

See docs/ech-0117-meta-attributes.md ("`.ili -> JSON Schema`" section) -
attribute-level surfacing has real corpus evidence (the `CLASS
Datenbestand`/`!!@basketRef=...` pattern lands on the class's first
attribute, not the class itself); class-level surfacing is the same
generic mechanism, verified here with a synthetic fixture only (no real
corpus example of a class-level meta-attribute found so far).
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonschema import class_to_json_schema
from interlis.runtime.parse import meta_attribute_comments, parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attribute_comments(src))
    return builder


def test_attribute_level_meta_attribute_surfaced_on_its_own_property():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS Datenbestand =
      !!@ basketRef=Foo_V1.Geobasisdaten
      BasketID : TEXT*20;
      Version : TEXT*10;
    END Datenbestand;
  END T;
END Foo.
"""
    )
    cls = builder.symbol_table.resolve("Foo.T.Datenbestand")
    schema = class_to_json_schema(cls)

    assert schema["properties"]["BasketID"]["x-meta"] == {"basketRef": "Foo_V1.Geobasisdaten"}
    assert "x-meta" not in schema["properties"]["Version"]
    assert "x-meta" not in schema


def test_class_level_meta_attribute_surfaced_on_the_defs_entry():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    !!@ IDGeoIV=219.1
    CLASS A =
      Attr1: TEXT*20;
    END A;
  END T;
END Foo.
"""
    )
    cls = builder.symbol_table.resolve("Foo.T.A")
    schema = class_to_json_schema(cls)

    assert schema["x-meta"] == {"IDGeoIV": "219.1"}


def test_no_meta_attributes_leaves_schema_unmarked():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Attr1: TEXT*20;
    END A;
  END T;
END Foo.
"""
    )
    cls = builder.symbol_table.resolve("Foo.T.A")
    schema = class_to_json_schema(cls)

    assert "x-meta" not in schema
    assert "x-meta" not in schema["properties"]["Attr1"]
