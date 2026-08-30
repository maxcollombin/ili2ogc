"""Backlog item 8 (VIEW support), Lot B - `.ili` VIEW -> JSON Schema.

See mappings/ilismeta16-to-jsonschema-rules.yml (View concept) and
spec/conversion/jsonschema-mapping.yml for the contract. `View` extends
`Class` in the metamodel and shares its `ClassAttribute` mechanism (backlog
item 8's Lot A2) - `class_to_json_schema`/`model_to_json_schema`
(`src/interlis/convert/jsonschema.py`) need no View-specific code at all,
confirmed here: a View is simply one more root, and its already-flattened
attribute set converts through the exact same path as a plain Class.
"""

import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.cli import _SUPPORTED_VIEW_FORMATION_KINDS
from interlis.convert.jsonschema import class_to_json_schema, model_to_json_schema
from interlis.runtime.parse import parse_text

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


def _view(builder, name: str):
    for inst in builder.symbol_table.all_registered():
        if getattr(inst, "_qualified_class", None) == "IlisMeta16.ModelData.View" and inst.Name == name:
            return inst
    raise AssertionError(f"no View named {name!r} built")


PROJECTION_SRC = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
      Attr2 : 0 .. 130;
    END B;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VP
      PROJECTION OF Test.Base.B;
      =
      ATTRIBUTE
        ALL OF B;
    END VP;
  END Views;
END Test.
"""

JOIN_SRC = """INTERLIS 2.3;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*20;
    END B;
    CLASS C =
      Attr3: TEXT*30;
    END C;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VJ
      JOIN OF B ~ Test.Base.B, C ~ Test.Base.C;
      WHERE
        B->Attr1 == C->Attr3;
      =
      ATTRIBUTE
        ALL OF B;
        ALL OF C;
    END VJ;
  END Views;
END Test.
"""

UNION_SRC = """INTERLIS 2.4;
MODEL Test AT "http://x" VERSION "1" =
  TOPIC Base =
    CLASS B =
      Attr1: TEXT*10;
    END B;
    CLASS C =
      Attr2: TEXT*10;
    END C;
  END Base;
  TOPIC Views =
    DEPENDS ON Test.Base;
    VIEW VU
      UNION OF B ~ Test.Base.B, C ~ Test.Base.C;
      =
      ATTRIBUTE
        Attr: TEXT*10 := B->Attr1,C->Attr2;
    END VU;
  END Views;
END Test.
"""


def test_projection_of_view_converts_like_a_class():
    view = _view(_build(PROJECTION_SRC), "VP")
    schema = class_to_json_schema(view)
    assert schema["title"] == "VP"
    assert schema["properties"]["Attr1"] == {"type": "string", "maxLength": 20}
    assert schema["properties"]["Attr2"] == {"type": "integer", "minimum": 0, "maximum": 130}


def test_join_of_where_view_flattens_both_bases_into_one_schema():
    view = _view(_build(JOIN_SRC), "VJ")
    schema = class_to_json_schema(view)
    assert set(schema["properties"]) == {"Attr1", "Attr3"}
    assert schema["properties"]["Attr1"]["type"] == "string"
    assert schema["properties"]["Attr3"]["type"] == "string"


def test_model_to_json_schema_accepts_views_alongside_classes():
    builder = _build(JOIN_SRC)
    classes = [
        i
        for i in builder.symbol_table.all_registered()
        if getattr(i, "_qualified_class", None) == "IlisMeta16.ModelData.Class"
    ]
    view = _view(builder, "VJ")
    schema = model_to_json_schema(classes + [view], symbol_table=builder.symbol_table)
    assert set(schema["$defs"]) == {"B", "C", "VJ"}
    assert set(schema["$defs"]["VJ"]["properties"]) == {"Attr1", "Attr3"}


def test_cli_formation_kind_filter_includes_every_kind():
    """cli.cmd_convert selects a VIEW root for every FormationKind - see
    docs/view-formation-support.md."""
    assert set(_SUPPORTED_VIEW_FORMATION_KINDS) == {
        "Projection",
        "Join",
        "Union",
        "Aggregation",
        "Inspection",
    }

    vp = _view(_build(PROJECTION_SRC), "VP")
    vj = _view(_build(JOIN_SRC), "VJ")
    vu = _view(_build(UNION_SRC), "VU")

    assert vp.FormationKind in _SUPPORTED_VIEW_FORMATION_KINDS
    assert vj.FormationKind in _SUPPORTED_VIEW_FORMATION_KINDS
    assert vu.FormationKind in _SUPPORTED_VIEW_FORMATION_KINDS


def test_union_view_converts_to_one_schema_with_the_union_attribute():
    builder = _build(UNION_SRC)
    view = _view(builder, "VU")
    schema = class_to_json_schema(view)
    assert schema["title"] == "VU"
    assert set(schema["properties"]) == {"Attr"}
    # a merge view is not 1:1 with a single base - GET-only
    assert schema["x-crud"] == ["GET"]
