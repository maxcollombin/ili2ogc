"""Backlog item 8, Lot D - CRUD support marker (`x-crud`) and payload validation.

See .claude/PROGRESS.md item 8's Lot D "point PATCH" decision: a `JOIN OF`
View is GET-only (no natural single writable target, by analogy with
non-updatable multi-table SQL views); a plain `Class` or a `PROJECTION OF`
View gets the full CRUD set. `validate_feature_properties` is the
"validation via le même JSON Schema" building block, using the standard
`jsonschema` package against this runtime's own `model_to_json_schema`
output.
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.convert.jsonschema import class_to_json_schema, model_to_json_schema, validate_feature_properties
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_MODEL = """INTERLIS 2.4;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
    CLASS C =
      Attr2: TEXT*20;
    END C;
    STRUCTURE S =
      Sub: TEXT*10;
    END S;
    CLASS Holder =
      Nested: S;
    END Holder;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
    VIEW VJ
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJ;
  END Views;
END Test.
"""


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def _resolve(builder, name: str) -> MetaInstance:
    return builder.symbol_table.resolve(name)


def test_class_gets_full_crud():
    builder = _build(_MODEL)
    schema = class_to_json_schema(_resolve(builder, "Test.Base.B"))
    assert schema["x-crud"] == ["GET", "POST", "PUT", "PATCH", "DELETE"]


def test_projection_view_gets_full_crud():
    builder = _build(_MODEL)
    view = _resolve(builder, "Test.Views.VP")
    schema = class_to_json_schema(view)
    assert schema["x-crud"] == ["GET", "POST", "PUT", "PATCH", "DELETE"]


def test_join_view_is_get_only():
    builder = _build(_MODEL)
    view = _resolve(builder, "Test.Views.VJ")
    schema = class_to_json_schema(view)
    assert schema["x-crud"] == ["GET"]


def test_structure_gets_no_crud_marker():
    builder = _build(_MODEL)
    schema = class_to_json_schema(_resolve(builder, "Test.Base.S"))
    assert "x-crud" not in schema


def test_validate_feature_properties_accepts_a_valid_payload():
    builder = _build(_MODEL)
    schema = model_to_json_schema([_resolve(builder, "Test.Base.B")])
    assert validate_feature_properties({"Attr1": "hello"}, schema, "B") == []


def test_validate_feature_properties_rejects_wrong_type():
    builder = _build(_MODEL)
    schema = model_to_json_schema([_resolve(builder, "Test.Base.B")])
    errors = validate_feature_properties({"Attr1": 42}, schema, "B")
    assert errors
    assert any("42" in e or "string" in e for e in errors)


def test_validate_feature_properties_resolves_nested_structure_refs():
    builder = _build(_MODEL)
    schema = model_to_json_schema([_resolve(builder, "Test.Base.Holder")])
    assert validate_feature_properties({"Nested": {"Sub": "x"}}, schema, "Holder") == []
    assert validate_feature_properties({"Nested": {"Sub": 1}}, schema, "Holder") != []
