"""`MANDATORY <named domain>` (e.g. `Geom : MANDATORY Coord2D;`) - InterlisModelBuilder._apply_pending_mandatory_overrides.

See docs/sql-conversion-strategy.md ("One more real gap found") for the
full investigation: found while building backlog item 14 (SQL DDL). Every
attribute referencing a named domain resolves to the SAME registered
DomainType instance (confirmed empirically) - `DomainType.Mandatory` is
the only place `MANDATORY` can attach in this metamodel (`AttrOrParam`
itself has none), so naively setting it there would incorrectly mark
every OTHER attribute using the same domain as mandatory too. The fix
gives a `MANDATORY`-qualified attribute its own private clone of the
resolved domain instead, leaving the shared one untouched.
"""

import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_text
from interlis.xtf.schema import attributes_of, resolve_attribute

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"

_MODEL = """INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    MyText = TEXT*20;
  TOPIC T =
    CLASS A =
      Required : MANDATORY MyText;
      Optional : MyText;
    END A;
  END T;
END Foo.
"""


def _build(src: str):
    tree, errors = parse_text(src)
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def test_mandatory_named_domain_reference_gets_its_own_clone():
    builder = _build(_MODEL)
    a = builder.symbol_table.resolve("Foo.T.A")
    attrs = attributes_of(a)
    required = resolve_attribute(attrs["Required"])
    optional = resolve_attribute(attrs["Optional"])
    assert required.mandatory is True
    assert optional.mandatory is False


def test_shared_domain_instance_is_never_mutated():
    builder = _build(_MODEL)
    a = builder.symbol_table.resolve("Foo.T.A")
    domain = builder.symbol_table.resolve("Foo.MyText")
    attrs = attributes_of(a)
    required = resolve_attribute(attrs["Required"])
    optional = resolve_attribute(attrs["Optional"])
    assert domain.Mandatory is None  # the DOMAIN declaration itself never carried MANDATORY
    assert optional.type_instance is domain  # unaffected use still shares the canonical instance
    assert required.type_instance is not domain  # the mandatory use got its own private clone
    assert required.type_instance.MaxLength == domain.MaxLength  # clone carries the domain's own fields


def test_inline_mandatory_type_unaffected_by_this_fix():
    """A fresh, non-shared instance (inline type, not a named domain) already worked before this fix - must keep working."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  TOPIC T =
    CLASS A =
      Inline : MANDATORY TEXT*20;
    END A;
  END T;
END Foo.
""")
    a = builder.symbol_table.resolve("Foo.T.A")
    resolved = resolve_attribute(attributes_of(a)["Inline"])
    assert resolved.mandatory is True


def test_mandatory_on_domain_already_declared_mandatory_needs_no_clone():
    """The DOMAIN itself is `MANDATORY <type>` - already correct, no override/clone needed."""
    builder = _build("""INTERLIS 2.4;
MODEL Foo AT "http://x" VERSION "1" =
  DOMAIN
    AlwaysSet = MANDATORY TEXT*20;
  TOPIC T =
    CLASS A =
      Attr1 : MANDATORY AlwaysSet;
    END A;
  END T;
END Foo.
""")
    a = builder.symbol_table.resolve("Foo.T.A")
    domain = builder.symbol_table.resolve("Foo.AlwaysSet")
    resolved = resolve_attribute(attributes_of(a)["Attr1"])
    assert resolved.mandatory is True
    assert resolved.type_instance is domain
