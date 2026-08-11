"""STRUCTURAL (not yet semantic) parser for INTERLIS transfer files (.xtf).

The XTF validator's first layer.

Deliberately generic: captures each attribute as a raw XML subtree
(`RawNode`), without interpreting whether it's a reference, a coordinate,
or a plain value - that interpretation depends on the attribute's
DECLARED TYPE on the schema (.ili) side, not on its XML shape alone (see
docs/xtf-transfer-encoding-notes.md: checked against both the Reference
Manual eCH-0031 V2.1.0 §4.3.9/4.3.11 and real files - the two diverge, see
that document). A later layer, cross-referencing this result with
IlisMeta16 (Class/AttrOrParam/DomainType already built by
InterlisModelBuilder), does the interpretation.

Streaming (`ET.iterparse` + `elem.clear()` per object/attribute): the
downloaded real corpus (`scripts/fetch_xtf_corpus.py`) contains
files several hundred MB in size - the full XML tree must never be kept
in memory.
"""
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


@dataclass
class RawNode:
    """Raw XML subtree for ONE attribute (or one of its descendants).

    The INTERLIS namespace is stripped from the tag, text is trimmed
    (None if empty/absent).
    """

    tag: str
    text: str | None
    attrib: dict[str, str]
    children: list["RawNode"] = field(default_factory=list)


@dataclass
class XtfObject:
    """An instance: a class OR a non-embedded relation.

    Both are encoded identically on the transfer side (§4.3.9.2).
    `qualified_class` is the full tag name as written in the file (e.g.
    'RoadTrafficCensus_V1_1.RoadTrafficCensus.MeasurementLocation'), not
    yet resolved against a schema.
    """

    tid: str | None
    qualified_class: str
    attributes: dict[str, list[RawNode]]  # nom d'attribut -> occurrences (LIST/BAG : plusieurs)


@dataclass
class XtfBasket:
    bid: str
    qualified_topic: str
    kind: str | None
    endstate: str | None
    objects: list[XtfObject] = field(default_factory=list)


@dataclass
class XtfModelRef:
    name: str
    version: str | None
    uri: str | None


@dataclass
class XtfTransfer:
    sender: str | None
    ili_version: str | None
    models: list[XtfModelRef]
    baskets: list[XtfBasket]


def _to_raw_node(elem: ET.Element) -> RawNode:
    text = elem.text.strip() if elem.text and elem.text.strip() else None
    return RawNode(
        tag=_strip_ns(elem.tag),
        text=text,
        attrib=dict(elem.attrib),
        children=[_to_raw_node(c) for c in elem],
    )


def parse_xtf(path: Path) -> XtfTransfer:
    """Parse an .xtf into (sender, version, models, baskets), see XtfTransfer.

    Resolves NO reference, interprets NO type - a purely structural layer
    (don't guess what needs the schema to be decided).
    """
    sender: str | None = None
    ili_version: str | None = None
    models: list[XtfModelRef] = []
    baskets: list[XtfBasket] = []

    # Stack of (role, tag_without_namespace) - the role is determined by
    # STRUCTURAL POSITION (depth relative to DATASECTION), never by the
    # tag name itself (basket/object/attribute tags are named after the
    # transferred model, not fixed keywords).
    stack: list[tuple[str, str]] = []
    current_basket: XtfBasket | None = None
    current_object: XtfObject | None = None

    for event, elem in ET.iterparse(str(path), events=("start", "end")):
        tag = _strip_ns(elem.tag)
        if event == "start":
            parent_role = stack[-1][0] if stack else None
            if parent_role is None:
                role = "transfer"
            elif parent_role == "transfer":
                role = "headersection" if tag == "HEADERSECTION" else ("datasection" if tag == "DATASECTION" else "other")
            elif parent_role == "headersection":
                role = "models" if tag == "MODELS" else "other"
            elif parent_role == "models":
                role = "model"
            elif parent_role == "datasection":
                role = "basket"
                current_basket = XtfBasket(
                    bid=elem.get("BID", ""), qualified_topic=tag,
                    kind=elem.get("KIND"), endstate=elem.get("ENDSTATE"),
                )
            elif parent_role == "basket":
                role = "object"
                current_object = XtfObject(tid=elem.get("TID"), qualified_class=tag, attributes={})
            elif parent_role == "object":
                role = "attribute"
            else:
                role = "raw"  # descendant of an attribute already being captured
            stack.append((role, tag))
            continue

        # event == "end"
        role, _tag = stack.pop()
        if role == "attribute":
            current_object.attributes.setdefault(tag, []).append(_to_raw_node(elem))
            elem.clear()
        elif role == "object":
            current_basket.objects.append(current_object)
            current_object = None
            elem.clear()
        elif role == "basket":
            baskets.append(current_basket)
            current_basket = None
            elem.clear()
        elif role == "model":
            models.append(XtfModelRef(name=elem.get("NAME", ""), version=elem.get("VERSION"), uri=elem.get("URI")))
            elem.clear()
        elif role == "headersection":
            sender = elem.get("SENDER")
            ili_version = elem.get("VERSION")

    return XtfTransfer(sender=sender, ili_version=ili_version, models=models, baskets=baskets)
