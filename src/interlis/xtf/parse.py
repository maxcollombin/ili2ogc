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

Accepts both real wire conventions for the envelope (section tags,
BID/TID, model name/version/uri, sender) - XTF 2.3 (bare, UPPERCASE
attributes; every file in xtf_corpus/, the only convention seen in real
Swiss open data so far) and XTF 2.4 (`ili:`-namespaced, lowercase, model
name as element text - see `_get_attr_ci` and the "sender"/"model" role
handling below; confirmed only via non-production reference/test fixtures,
see tests/fixtures/xtf/xtf24allerrors/NOTICE and xtf24envelope/NOTICE).
"""

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _model_name_from_tag(tag: str) -> str | None:
    """Recover a namespaced XTF 2.4 data tag's declaring MODEL name, or `None` for a bare (XTF 2.3) tag.

    A basket/object/attribute tag in real XTF 2.4 data uses a genuine XML
    namespace whose URI is the transferred model's own name, one segment
    per model (`xmlns:Holznutzungsbewilligung_V1_0=
    "http://www.interlis.ch/xtf/2.4/Holznutzungsbewilligung_V1_0"` -
    confirmed on multiple real geodienste.ch XTF 2.4 files, one `xmlns:`
    declaration per IMPORTed model, always ending in the model's own bare
    Name) - unlike XTF 2.3, where the SAME information is already baked
    into the bare, undotted-namespace tag text itself (e.g.
    `RoadTrafficAccidentLocation_V2.RoadTrafficAccident`, `_strip_ns` is a
    no-op there). `_strip_ns` alone loses this prefix for a REAL
    namespace (Clark notation `{uri}local`, ElementTree's own
    resolution), taking only `local` - fine for the fixed `ili:`/`geom:`
    structural keywords (HEADERSECTION/COORD/...), but silently wrong for
    a basket/object tag, which needs the full `Model.Topic[.Class]` to
    resolve against a schema. Returns `None` (not a real transferred
    model) for anything not matching this URI shape, including the fixed
    `ili:` namespace itself (`.../2.4/INTERLIS`) - never mistaken for a
    model named "INTERLIS".
    """
    if not tag.startswith("{"):
        return None
    uri = tag[1:].split("}", 1)[0]
    prefix = "http://www.interlis.ch/xtf/2.4/"
    if not uri.startswith(prefix):
        return None
    name = uri[len(prefix) :]
    return name if name and name != "INTERLIS" else None


def _get_attr_ci(elem: ET.Element, name: str) -> str | None:
    """Case/namespace-insensitive attribute lookup - `name` is UPPERCASE.

    Two real wire conventions exist for the same envelope fields: XTF 2.3
    attributes are bare and UPPERCASE (BID/TID/KIND/ENDSTATE/NAME/VERSION/
    URI/SENDER); XTF 2.4 equivalents are `ili:`-namespaced and lowercase
    (confirmed via iox-ili's Xtf24Reader.java, e.g. QNAME_ILI_BID/QNAME_ILI_TID)
    - ElementTree exposes a namespaced attribute's key in Clark notation
    (`{uri}localname`), so the bare-name fast path is tried first, then
    every attribute's namespace-stripped, uppercased local name.
    """
    if name in elem.attrib:
        return elem.attrib[name]
    for key, value in elem.attrib.items():
        if _strip_ns(key).upper() == name:
            return value
    return None


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
            # Section-name keywords (HEADERSECTION/DATASECTION/MODELS/SENDER)
            # are compared case-insensitively - XTF 2.3 uses bare UPPERCASE,
            # XTF 2.4 uses `ili:`-namespaced lowercase (already stripped by
            # `_strip_ns` above; see `_get_attr_ci` for the same treatment
            # of attributes).
            upper_tag = tag.upper()
            if parent_role is None:
                role = "transfer"
            elif parent_role == "transfer":
                role = (
                    "headersection"
                    if upper_tag == "HEADERSECTION"
                    else ("datasection" if upper_tag == "DATASECTION" else "other")
                )
            elif parent_role == "headersection":
                # SENDER is a child element only in XTF 2.4 (an attribute of
                # HEADERSECTION itself in XTF 2.3 - handled below, on the
                # "headersection" end event).
                if upper_tag == "MODELS":
                    role = "models"
                elif upper_tag == "SENDER":
                    role = "sender"
                else:
                    role = "other"
            elif parent_role == "models":
                role = "model"
            elif parent_role == "datasection":
                role = "basket"
                model_name = _model_name_from_tag(elem.tag)
                qualified_topic = f"{model_name}.{tag}" if model_name else tag
                current_basket = XtfBasket(
                    bid=_get_attr_ci(elem, "BID") or "",
                    qualified_topic=qualified_topic,
                    kind=_get_attr_ci(elem, "KIND"),
                    endstate=_get_attr_ci(elem, "ENDSTATE"),
                )
            elif parent_role == "basket":
                role = "object"
                # XTF 2.4: the object's own namespace prefix names its
                # OWN model (e.g. via `TOPIC EXTENDS`, can differ from the
                # basket's) and its local tag is the bare Class name alone
                # - needs the enclosing basket's own TOPIC to reconstruct
                # `Model.Topic.Class` (namespace alone only gives `Model`).
                # XTF 2.3 (`model_name is None`): the bare tag is already
                # the complete `Model.Topic.Class` on its own (e.g.
                # `RoadTrafficAccidentLocation_V2.RoadTrafficAccident.
                # RoadTrafficAccident`) - used as-is, unchanged from before
                # this fix.
                model_name = _model_name_from_tag(elem.tag)
                basket_topic = current_basket.qualified_topic.rsplit(".", 1)[-1]
                qualified_class = f"{model_name}.{basket_topic}.{tag}" if model_name else tag
                current_object = XtfObject(
                    tid=_get_attr_ci(elem, "TID"), qualified_class=qualified_class, attributes={}
                )
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
            # XTF 2.3: NAME is an attribute (VERSION/URI alongside it). XTF
            # 2.4: the model name is the element's TEXT content instead - no
            # per-model version/uri in the header at all (confirmed via
            # iox-ili's Xtf24Reader.readModel, a bare `List<String>`).
            name = _get_attr_ci(elem, "NAME")
            if name is None:
                name = elem.text.strip() if elem.text else ""
            models.append(XtfModelRef(name=name, version=_get_attr_ci(elem, "VERSION"), uri=_get_attr_ci(elem, "URI")))
            elem.clear()
        elif role == "sender":
            sender = elem.text.strip() if elem.text else None
        elif role == "headersection":
            if sender is None:  # not already set via the XTF 2.4 "sender" child role above
                sender = _get_attr_ci(elem, "SENDER")
            ili_version = _get_attr_ci(elem, "VERSION")

    return XtfTransfer(sender=sender, ili_version=ili_version, models=models, baskets=baskets)
