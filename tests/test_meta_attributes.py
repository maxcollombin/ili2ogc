"""eCH-0117 `!!@Name=Value` meta-attribute capture.

`!!` comments sit on ANTLR's HIDDEN channel (vendor/interlis-antlr4/
InterlisLexer.g4: `channel(HIDDEN)`, never `-> skip`) - never discarded,
just invisible to the 121 mapped parser rules. `runtime.parse.
meta_attribute_comments` recovers them via a second lex-only pass;
`InterlisModelBuilder.build(tree, meta_attributes=...)` attaches each to
the "first following language construct" (eCH-0117 SS3) as a real
`IlisMeta16.ModelData.MetaAttribute` instance, via the metamodel's own
pre-existing `MetaAttributes` association - no grammar change, no new
metamodel class, purely additive (opt-in via the `meta_attributes` kwarg).
"""
import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import meta_attribute_comments, parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def _build(src: str, *, capture: bool = True):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if capture:
            builder.build(tree, meta_attributes=meta_attribute_comments(src))
        else:
            builder.build(tree)
    return builder


def _names_values(instance) -> list[tuple[str, str]]:
    return [(m.Name, m.Value) for m in (getattr(instance, "MetaAttribute", None) or [])]


def test_meta_attribute_comments_lexes_pairs_and_quoted_strings():
    src = (
        'INTERLIS 2.4;\n'
        '!!@ili.charset = ISO-8859-1\n'
        '!!@a=1;b=2\n'
        '!!@limitedTo = "ch.admin.bafu.kbs_codetexte_V1_5"\n'
        'MODEL Foo AT "http://x" VERSION "1" =\n'
        'END Foo.\n'
    )
    assert meta_attribute_comments(src) == [
        (2, "ili.charset", "ISO-8859-1"),
        (3, "a", "1"),
        (3, "b", "2"),
        (4, "limitedTo", "ch.admin.bafu.kbs_codetexte_V1_5"),
    ]


def test_ordinary_comment_without_at_sign_is_not_a_meta_attribute():
    src = 'INTERLIS 2.4;\n!! just a regular comment, no @\nMODEL Foo AT "http://x" VERSION "1" =\nEND Foo.\n'
    assert meta_attribute_comments(src) == []


def test_model_level_meta_attributes_attach_to_model_instance():
    builder = _build(
        """INTERLIS 2.4;
!!@technicalContact=mailto:models@geo.admin.ch
!!@furtherInformation=https://example.org
MODEL Foo AT "http://x" VERSION "1" =
END Foo.
"""
    )
    model = builder.symbol_table.resolve("Foo")
    assert _names_values(model) == [
        ("technicalContact", "mailto:models@geo.admin.ch"),
        ("furtherInformation", "https://example.org"),
    ]


def test_topic_meta_attribute_attaches_to_submodel_not_datunit():
    # eCH-0117 SS5: explicit rule - a TopicDef's meta-attributes belong to
    # the schema side (SubModel), never DataUnit, even though both twins
    # share the same source position.
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  !!@shortName=Bar
  TOPIC T =
  END T;
END Foo.
"""
    )
    submodel = builder.symbol_table.resolve("T")
    assert _names_values(submodel) == [("shortName", "Bar")]
    assert submodel._twin is not None
    assert _names_values(submodel._twin) == []


def test_multi_declaration_domain_disambiguates_by_line():
    # The real, hardest case: 2 domains sharing one DOMAIN keyword block -
    # a meta-attribute must attach to the domain it immediately precedes,
    # never to a sibling in the same block (confirmed for real on
    # CHBase_Part1_GEOMETRY_V1.ili, ili_corpus/ - see PROGRESS.md).
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    !!@CRS=EPSG:2056
    Coord2 = COORD 0..100, 0..200;
    Coord3 = COORD 0..100, 0..200, 0..50;
END Foo.
"""
    )
    coord2 = builder.symbol_table.resolve("Coord2")
    coord3 = builder.symbol_table.resolve("Coord3")
    assert _names_values(coord2) == [("CRS", "EPSG:2056")]
    assert _names_values(coord3) == []


def test_class_and_attribute_level_meta_attributes():
    builder = _build(
        """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    !!@cid=42
    CLASS A =
      !!@name=AgeInYears
      Age : 0 .. 130;
    END A;
  END T;
END Foo.
"""
    )
    cls = builder.symbol_table.resolve("A")
    assert _names_values(cls) == [("cid", "42")]
    attr = cls.ClassAttribute[0]
    assert _names_values(attr) == [("name", "AgeInYears")]


def test_no_meta_attributes_kwarg_is_fully_backward_compatible():
    # build(tree) with no meta_attributes= at all - the pre-Lot-3 call
    # shape used by every other test/caller - must behave identically
    # (no MetaAttribute anywhere, no crash).
    builder = _build(
        """INTERLIS 2.4;
!!@technicalContact=mailto:x@example.org
MODEL Foo AT "http://x" VERSION "1" =
END Foo.
""",
        capture=False,
    )
    model = builder.symbol_table.resolve("Foo")
    assert _names_values(model) == []
