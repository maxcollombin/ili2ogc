"""`CLASS X (EXTENDED)` reopening a same-named class from a `TOPIC ... EXTENDS ...` base.

Real corpus gap found via item 13's VIEW-corpus pipeline
(`.claude/PROGRESS.md`, `docs/fgdm4gs-view-strategy.md`): `ISOS_V2.ili`
uses `TOPIC ISOS EXTENDS ISOS_V2.ISOSBase = CLASS Ortsbild (EXTENDED) =
<additional attrs> ... END Ortsbild; ... END ISOS;` - eCH-0031 V2.1.0
§3.5.2, exact citation: "Erweitert z.B. ein Thema T2 das Thema T1, das
die Klasse C enthaelt, gibt es mit C (EXTENDED) innerhalb von T2 nur eine
Klasse, naemlich C" (there is only ONE class C). `classDef.Super`
(`spec/grammar/mapping/03_classes_and_structures.yml`) only ever fired
for an EXPLICIT `EXTENDS classOrStructureRef` clause - `(EXTENDED)` has
no such clause (its target is implicit: the same-named class in the
topic named by the enclosing topic's own `EXTENDS`), so the reopening
class was left with no `Super` at all, and any VIEW/converter reading an
attribute inherited from the base topic's class (`name`/`id`/`kantone` in
the real ISOS case) found nothing.

`InterlisModelBuilder._fix_class_extended_super` approximates "one class
C" as ordinary single inheritance (`Super` -> the base topic's class),
reusing the existing `Super`-chain walk (`xtf.schema.attributes_of`) for
free - confirmed on both real corpus patterns found (a sondage of the 71
`ili_corpus/` files, 2026-08-31): a cross-MODEL, same-FILE `TOPIC EXTENDS`
(`CHBase_Part4_ADMINISTRATIVEUNITS_V1.ili`'s `AdministrativeUnion`/
`Agency`, 2 hits) and a cross-file one via `IMPORTS` (`ISOS_V2.ili`,
`Naturereigniskataster_umfassend_V1.ili`, 19 hits) - the fixtures here
mirror the cross-file shape (the more general one, `IMPORTS` +
`ModelRepository`).
"""

import warnings
from pathlib import Path

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance
from interlis.runtime.parse import parse_file
from interlis.xtf.schema import attributes_of

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"
FIXTURES_DIR = Path(__file__).parent / "fixtures/class_extended"


def _build(repository):
    tree, errors = parse_file(FIXTURES_DIR / "extension.ili")
    assert not errors, f"erreurs de syntaxe inattendues: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree)
    return builder


def test_extended_class_resolves_super_and_merges_attributes_via_repository():
    repo = ModelRepository([FIXTURES_DIR])
    builder = _build(repo)
    c = builder.symbol_table.resolve("ExtDerived.T2.C")
    assert isinstance(c.Super, MetaInstance)
    assert c.Super.Name == "C"
    assert list(attributes_of(c).keys()) == ["attr1", "attr2"]


def test_extended_class_without_the_base_model_degrades_gracefully():
    """No `--repo`: the base topic's class is unreachable - never a crash (RULE #5)."""
    from interlis.builder.errors import UnresolvedNamedReference

    builder = _build(None)
    c = builder.symbol_table.resolve("ExtDerived.T2.C")
    assert isinstance(c.Super, UnresolvedNamedReference)
    assert list(attributes_of(c).keys()) == ["attr2"]


def test_view_attribute_referencing_an_inherited_attribute_resolves_its_type():
    """`a1 := C -> attr1` (`attr1` inherited from the base topic's `C`) gets a real `Type`, not `None`."""
    repo = ModelRepository([FIXTURES_DIR])
    builder = _build(repo)
    view = next(
        inst
        for inst in builder.symbol_table.all_registered()
        if isinstance(inst, MetaInstance)
        and inst._qualified_class == "IlisMeta16.ModelData.View"
        and inst.Name == "view_c"
    )
    by_name = {a.Name: a for a in view.ClassAttribute}
    assert isinstance(getattr(by_name["a1"], "Type", None), MetaInstance)  # inherited attr1
    assert isinstance(getattr(by_name["a2"], "Type", None), MetaInstance)  # own attr2
