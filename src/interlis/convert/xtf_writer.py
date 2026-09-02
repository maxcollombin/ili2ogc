"""XtfObject/XtfBasket -> .xtf (XTF 2.3 wire text) writer - the reverse of `xtf/parse.py`.

Two layers, deliberately separated:

- `render_xtf` (generic): serializes an already-assembled `XtfTransfer`
  (HEADERSECTION/MODELS + DATASECTION/baskets/objects) to XTF 2.3 wire
  text. Knows nothing about VIEW TOPIC - a basket's objects are written
  exactly as their `XtfObject`/`RawNode` already hold them, whether they
  came straight off `xtf.parse.parse_xtf` (a plain class instance,
  round-tripped as-is) or were built by `write_view_basket` below. Proven
  independently of any VIEW by a structural ROUND-TRIP test
  (`tests/test_xtf_writer_roundtrip.py`): parse a real .xtf, render it
  back, re-parse the result, compare - isolates this layer's correctness
  from VIEW-evaluation correctness (`convert/jsonfg.evaluate_view_objects`).
- `write_view_basket` (VIEW-specific): evaluates one VIEW TOPIC's `View`
  instances against a source transfer (`convert/jsonfg.evaluate_view_objects`)
  and assembles the one new `XtfBasket` its instances belong in, honoring
  refman eCH-0031 V2.1.0 SS4.3.8's "Codierung von Sichten" (`tid`, no
  `operation`, only `ATTRIBUTE`/`ALL OF`-declared attributes) and SS4.3.7's
  attribute ORDER rule (a compiled schema's XSD `xsd:sequence` enforces
  the VIEW's own `ClassAttribute` declaration order exactly - a raw
  object's own wire order, inherited from whichever base object(s) it was
  projected/merged from, cannot be trusted to already match that).

Refman SS4182/SS4728 gate VIEW transfer on `VIEW TOPIC` specifically (a VIEW inside a plain
`TOPIC` compiles with `ili2c` but is silently absent from the generated
XSD and from any transfer) - `write_view_basket` doesn't enforce this
itself (the caller already resolved `view` off a built model, where
`ViewUnit` on the containing topic already reflects it), it only
assembles the wire content once a VIEW TOPIC's basket is what's wanted.
"""

from xml.etree import ElementTree as ET

from interlis.builder.forward_refs import SymbolTable
from interlis.builder.repository import ModelRepository
from interlis.convert.jsonfg import evaluate_view_objects
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.parse import RawNode, XtfBasket, XtfModelRef, XtfObject, XtfTransfer

_XTF23_NAMESPACE = "http://www.interlis.ch/INTERLIS2.3"


def _raw_node_to_element(node: RawNode, *, tag: str) -> ET.Element:
    """`RawNode` -> its element, with `tag` overriding `node.tag` at THIS level only.

    For a plain object read straight off a real .xtf, `tag` always equals
    `node.tag` already (`xtf/parse.py`'s `parse_xtf` derives both from the
    SAME source element) - passing it explicitly is a no-op there. It is
    NOT a no-op for a VIEW-renamed attribute: `_project_object_under_view_names`
    (`convert/jsonfg.py`) re-keys `XtfObject.attributes` under the VIEW's
    own `Name` (e.g. `RoadNumber` -> `roadnumber`), but the `RawNode`
    VALUE it re-keys is copied unchanged - it still carries the SOURCE
    attribute's own `.tag` inside it (JSON-FG never notices: `object_to_
    feature` reads properties by dict key alone, never `RawNode.tag`).
    Nested children keep their OWN tags always (fixed COORD/Reference/
    STRUCTURE-element vocabulary, never an attribute name).
    """
    elem = ET.Element(tag, dict(node.attrib))
    if node.text is not None:
        elem.text = node.text
    for child in node.children:
        elem.append(_raw_node_to_element(child, tag=child.tag))
    return elem


def _object_element(obj: XtfObject, attr_order: list[str] | None) -> ET.Element:
    """One `XtfObject` -> its `<Model.Topic.Class TID="...">` element.

    `attr_order`, when given, overrides `obj.attributes`' own dict
    iteration order with this explicit attribute-name sequence (see
    module docstring - the XSD-sequence order requirement). `None` keeps
    the dict's own order (correct for a plain round-tripped object: it
    already satisfied its schema's sequence once to be valid in the first
    place). An attribute absent from `obj.attributes` (an optional VIEW
    attribute with no value for this row) is simply skipped, same as any
    OPTIONAL attribute already is in a real transfer.
    """
    attrib = {"TID": obj.tid} if obj.tid is not None else {}
    elem = ET.Element(obj.qualified_class, attrib)
    for name in attr_order if attr_order is not None else list(obj.attributes):
        for node in obj.attributes.get(name, []):
            elem.append(_raw_node_to_element(node, tag=name))
    return elem


def _basket_element(basket: XtfBasket, attr_order_by_class: dict[str, list[str]] | None) -> ET.Element:
    attrib = {"BID": basket.bid}
    if basket.kind is not None:
        attrib["KIND"] = basket.kind
    if basket.endstate is not None:
        attrib["ENDSTATE"] = basket.endstate
    elem = ET.Element(basket.qualified_topic, attrib)
    for obj in basket.objects:
        order = (attr_order_by_class or {}).get(obj.qualified_class)
        elem.append(_object_element(obj, order))
    return elem


def render_xtf(transfer: XtfTransfer, *, attr_order_by_class: dict[str, list[str]] | None = None) -> str:
    """Serialize `transfer` to XTF 2.3 wire text - the convention `xtf/parse.py` reads.

    See `tests/fixtures/xtf/sample.xtf`: bare, UPPERCASE envelope
    attributes, no `ili:` namespace prefix. Generic over what
    `transfer.baskets` contains - see module docstring.
    `attr_order_by_class`, keyed by `XtfObject.qualified_class`, overrides
    per-class attribute order for classes needing it (VIEW-derived ones);
    every other class keeps its objects' own `.attributes` dict order.
    """
    root = ET.Element("TRANSFER", {"xmlns": _XTF23_NAMESPACE})
    header = ET.SubElement(root, "HEADERSECTION", {"VERSION": "2.3"})
    if transfer.sender is not None:
        header.set("SENDER", transfer.sender)
    models_elem = ET.SubElement(header, "MODELS")
    for model in transfer.models:
        model_attrib = {"NAME": model.name}
        if model.version is not None:
            model_attrib["VERSION"] = model.version
        if model.uri is not None:
            model_attrib["URI"] = model.uri
        ET.SubElement(models_elem, "MODEL", model_attrib)
    data = ET.SubElement(root, "DATASECTION")
    for basket in transfer.baskets:
        data.append(_basket_element(basket, attr_order_by_class))
    return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")


def _view_attr_order(view: MetaInstance) -> list[str]:
    """The VIEW's own `ClassAttribute` declaration order - refman SS4.3.7's XSD-sequence order."""
    return [name for attr in getattr(view, "ClassAttribute", None) or [] if (name := getattr(attr, "Name", None))]


def write_view_basket(
    view: MetaInstance,
    transfer: XtfTransfer,
    *,
    bid: str,
    symbol_table: SymbolTable,
    repository: ModelRepository | None = None,
    kind: str | None = None,
) -> XtfBasket:
    """Evaluate `view` against `transfer` and assemble its VIEW TOPIC `XtfBasket`.

    `kind` defaults to `None` (omitted on the wire) - refman SS4.3.6:
    "%TransferKind%...Falls das Attribut fehlt, wird FULL angenommen"
    (if the attribute is missing, FULL is assumed), and SS4182/SS4728:
    VIEW objects have no stable identity across transfers, so a VIEW
    TOPIC basket is only ever meaningfully transferred `FULL` - omitting
    `KIND` is both
    spec-faithful (never redundant) and matches a real ili2c-compiled
    schema for a simple transfer (confirmed empirically: `ili2c -oXSD`
    does not declare a `KIND` attribute at all when the model has no
    incremental-transfer setup, rejecting ANY explicit value, correct or
    not - `xmllint --schema` against `Waldabstandslinien_V1_2_d`).
    `INITIAL`/`UPDATE` would need an identity-stability guarantee this
    runtime cannot make - pass one of `"FULL"`/`"INITIAL"`/`"UPDATE"`
    (refman's own enum, uppercase) explicitly if a caller ever needs to.
    `bid` is the caller's choice (a `VIEW TOPIC`'s own basket id, not
    derived from `transfer` - the source transfer's basket id belongs to
    a DIFFERENT topic).

    Every `FormationKind` is supported (`evaluate_view_objects` - only an
    `INSPECTION` of a single-hop SURFACE/AREA geometry raises `ValueError`
    there, no XTF-transferable shape at all).

    Object/basket tags: `evaluate_view_objects`' returned `XtfObject`s keep
    whichever `qualified_class` their SOURCE object(s) happened to carry
    (a Projection/Aggregation/Union row keeps its base object's own tag; a
    Join/Inspection row gets the VIEW's bare short `Name` - see
    `_merge_join_combo`/`_project_object_under_view_names`/
    `_inspection_elements` in `convert/jsonfg.py`, where that field is
    vestigial, never read by `object_to_feature`) - wrong either way for a
    wire tag here, which must be the VIEW's own fully qualified
    `Model.Topic.ViewName` (refman SS4.3.7's `Object` rule) regardless of
    FormationKind. Resolved via `symbol_table.qualified_name_of(view)`
    (the same dotted path the builder registered the VIEW under) rather
    than reconstructed by name-guessing; the basket's own tag is that path
    one level up (`Model.Topic`, refman SS4.3.6's `Basket` rule).
    """
    objects = evaluate_view_objects(view, transfer, symbol_table=symbol_table, repository=repository)
    qualified_view = symbol_table.qualified_name_of(view)
    if qualified_view is None or "." not in qualified_view:
        view_name = getattr(view, "Name", None)
        raise ValueError(f"write_view_basket: {view_name!r} isn't registered under a Model.Topic.Name path")
    qualified_topic = qualified_view.rsplit(".", 1)[0]
    objects = [XtfObject(tid=obj.tid, qualified_class=qualified_view, attributes=obj.attributes) for obj in objects]
    return XtfBasket(bid=bid, qualified_topic=qualified_topic, kind=kind, endstate=None, objects=objects)


def _unquote(value: str | None) -> str | None:
    """Strip one matching pair of surrounding `"` - `Model.Version`/`Model.At` keep their source quoting.

    `spec/grammar/mapping/02_packages.yml`'s `modelDef.At`/`Version`
    capture the raw `STRING` token text verbatim (`field: STRING`, no
    unquoting transform - true of every `field: STRING` capture in this
    spec, not special-cased here) - `MODEL Foo AT "http://x" VERSION
    "2020-01-01"` yields `At == '"http://x"'`, quotes included. Harmless
    for every EXISTING consumer (documentation text, comments - never
    displayed/compared raw), but wrong verbatim on an XML attribute value
    here (`&quot;http://x&quot;` in the rendered .xtf). Narrow, local
    fix - not a general STRING-unquoting change to the builder.
    """
    if value is not None and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _model_ref_for(qualified_name: str, symbol_table: SymbolTable) -> XtfModelRef:
    """`XtfModelRef` for the MODEL owning `qualified_name` (its leading path segment).

    `Version`/`At` (grammar keywords `VERSION`/`AT` on `MODEL ... = ...;`)
    are read straight off the built `Model` instance when resolvable;
    both stay `None` otherwise (unregistered model, or genuinely absent
    from the source `.ili` - both OPTIONAL on the wire: refman SS4.3.4's
    XTF 2.4 `Model` rule carries no version/uri at all, only `%ModelName%`
    - `NAME` is the one part every convention agrees is required).
    """
    model_name = qualified_name.split(".", 1)[0]
    model_instance = symbol_table.resolve(model_name)
    version = _unquote(getattr(model_instance, "Version", None)) if model_instance is not None else None
    uri = _unquote(getattr(model_instance, "At", None)) if model_instance is not None else None
    return XtfModelRef(name=model_name, version=version, uri=uri)


def write_xtf(
    view: MetaInstance,
    transfer: XtfTransfer,
    *,
    bid: str,
    symbol_table: SymbolTable,
    repository: ModelRepository | None = None,
    sender: str | None = None,
    merge_with_source: bool = False,
) -> str:
    """Compose one VIEW TOPIC's materialized data into a complete .xtf document - the `write-xtf` CLI's core.

    `merge_with_source=False` (the CLI default): a STANDALONE transfer
    holding ONLY the new VIEW basket, with ONLY the VIEW's own model in
    `MODELS` (its base model's short-name-qualified object tags never
    appear in this transfer at all - only VIEW instances do, refman
    SS4.3.4's "Hauptmodelle...zu welchen Objekte im Transfer vorkommen
    können"). This is the direct wire-form counterpart of "the result of
    evaluating this VIEW" - hand-verified against `ili2c`/`xmllint`.

    `merge_with_source=True`: refman SS4.3.5's `DataSection = { Basket }`
    - a transfer holding arbitrarily many baskets, even across models, is
    explicitly normal (SS4.3.6: "Behälter sind Instanzen eines konkreten
    TOPIC bzw. VIEW TOPIC", no singular-transfer restriction) - this mode
    APPENDS the VIEW basket to a COPY of `transfer`'s own baskets/models,
    so one file self-describes both the raw base data and its
    pre-computed VIEW result together, closer to the original
    SQL-identifier-naming-friction motivation for a `.xtf` writer at all
    than handing out a VIEW-only
    fragment. Not the default only because it widens
    what a single call has to get right at once (the untouched source
    baskets survive byte-for-byte ALONGSIDE the new one) - not because it
    is spec-questionable or overkill; it is the more natural reading of
    "a VIEW TOPIC's basket sits in a DataSection exactly like any other
    Topic's" and is a cheap follow-up once the standalone path is proven.
    """
    basket = write_view_basket(view, transfer, bid=bid, symbol_table=symbol_table, repository=repository)
    qualified_view = symbol_table.qualified_name_of(view)
    assert qualified_view is not None  # write_view_basket already raised ValueError otherwise
    view_model = _model_ref_for(qualified_view, symbol_table)
    models = list(transfer.models) if merge_with_source else []
    if not any(m.name == view_model.name for m in models):
        models.append(view_model)
    baskets = [*transfer.baskets, basket] if merge_with_source else [basket]
    out = XtfTransfer(sender=sender, ili_version=transfer.ili_version, models=models, baskets=baskets)
    return render_xtf(out, attr_order_by_class={qualified_view: _view_attr_order(view)})


__all__ = [
    "XtfBasket",
    "XtfModelRef",
    "XtfTransfer",
    "render_xtf",
    "write_view_basket",
    "write_xtf",
]
