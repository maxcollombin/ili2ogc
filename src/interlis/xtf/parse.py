"""Parseur STRUCTUREL (pas encore semantique) de fichiers de transfert
INTERLIS (.xtf) - premiere couche du futur validateur (Lot 26/27).

Delibérément generique : capture chaque attribut comme un sous-arbre XML
brut (`RawNode`), sans interpreter s'il s'agit d'une reference, d'une
coordonnee ou d'une valeur simple - cette interpretation depend du TYPE
DECLARE de l'attribut cote schema (.ili), pas de sa seule forme XML (voir
docs/xtf-transfer-encoding-notes.md, RULE #4 : verifie contre le
Reference Manual eCH-0031 V2.1.0 §4.3.9/4.3.11 ET contre de vrais fichiers
reels - les deux divergent, voir ce document). Une couche ulterieure,
croisant ce resultat avec IlisMeta16 (Class/AttrOrParam/DomainType deja
construits par InterlisModelBuilder), fera l'interpretation.

Streaming (`ET.iterparse` + `elem.clear()` a chaque objet/attribut) : le
corpus reel telecharge (`scripts/fetch_xtf_corpus.py`) contient des
fichiers de plusieurs centaines de Mo - ne jamais garder l'arbre XML
complet en memoire."""
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


@dataclass
class RawNode:
    """Sous-arbre XML brut d'UN attribut (ou d'un de ses descendants),
    namespace INTERLIS retire du tag, texte trim (None si vide/absent)."""
    tag: str
    text: str | None
    attrib: dict[str, str]
    children: list["RawNode"] = field(default_factory=list)


@dataclass
class XtfObject:
    """Une instance (classe OU relation non embarquee - encodees de
    facon identique cote transfert, §4.3.9.2) - `qualified_class` est le
    nom de balise complet tel qu'ecrit dans le fichier (ex.
    'RoadTrafficCensus_V1_1.RoadTrafficCensus.MeasurementLocation'), PAS
    encore resolu contre un schema."""
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
    """Parse un .xtf en (sender, version, models, baskets) - voir
    XtfTransfer. Ne resout AUCUNE reference, n'interprete AUCUN type -
    couche purement structurelle (RULE #1 : ne pas deviner ce qui
    necessite le schema pour etre tranche)."""
    sender: str | None = None
    ili_version: str | None = None
    models: list[XtfModelRef] = []
    baskets: list[XtfBasket] = []

    # Pile de (role, tag_sans_namespace) - le role est determine par la
    # POSITION structurelle (profondeur relative a DATASECTION), jamais
    # par le nom de balise lui-meme (les balises basket/objet/attribut
    # sont nommees d'apres le modele transfere, pas des mots-cles fixes).
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
                role = "raw"  # descendant d'un attribut deja en cours de capture
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
