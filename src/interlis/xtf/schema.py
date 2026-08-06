"""Couche SEMANTIQUE (Lot 30) : croise les objets XTF structurels (parse.py,
Lot 27) avec le schema deja construit par InterlisModelBuilder (Class/
AttrOrParam/Type), pour que validate.py puisse interpreter chaque attribut
selon son type DECLARE plutot que sa seule forme XML brute.

Reutilise directement la SymbolTable/ModelRepository deja construites par le
ModelBuilder (voir builder/forward_refs.py, builder/repository.py) - un
Class instance est deja enregistre sous son nom qualifie complet
(Model.Topic.ClassName, via InterlisModelBuilder._qualify_name) qui
correspond EXACTEMENT a XtfObject.qualified_class (meme convention de
nommage - confirme empiriquement, RULE #1)."""
from dataclasses import dataclass

from interlis.builder.repository import ModelRepository
from interlis.builder.forward_refs import SymbolTable
from interlis.metamodel.instance import MetaInstance


def resolve_class(qualified_class: str, *, symbol_table: SymbolTable, repository: ModelRepository | None) -> MetaInstance | None:
    """Retrouve l'instance IlisMeta16.ModelData.Class correspondant a un
    XtfObject.qualified_class ("Model.Topic.ClassName"). Cherche d'abord dans
    la table du modele racine (classe locale ou deja resolue par import),
    puis - si absente et qu'un repository est fourni - dans le modele
    designe par le PREMIER segment du nom qualifie (meme mecanisme que la
    resolution de reference croisee du ModelBuilder, ForwardRefResolver)."""
    found = symbol_table.resolve(qualified_class, kind_hint=["Class"])
    if isinstance(found, MetaInstance):
        return found
    if repository is not None and "." in qualified_class:
        model_name = qualified_class.split(".", 1)[0]
        found = repository.resolve_external(model_name, qualified_class, kind_hint=["Class"])
        if isinstance(found, MetaInstance):
            return found
    return None


def attributes_of(class_instance: MetaInstance) -> dict[str, MetaInstance]:
    """Nom d'attribut -> instance AttrOrParam, pour les attributs PROPRES a
    cette classe (association ClassAttr, role ClassAttribute - voir
    spec/grammar/mapping/04_attributes.yml, attributeDef.parent).
    N'inclut PAS les attributs herites via EXTENDS - limite documentee
    (README/PROGRESS), hors perimetre de ce lot (necessiterait de remonter
    la chaine Inheritance, jamais exercee par le corpus XTF cible a ce
    jour)."""
    return {
        a.Name: a
        for a in (getattr(class_instance, "ClassAttribute", None) or [])
        if isinstance(a, MetaInstance) and getattr(a, "Name", None)
    }


@dataclass
class ResolvedAttribute:
    """Un attribut de schema pret a etre interprete/valide : son instance
    AttrOrParam, son Type resolu (peut etre None si non resolu - reference
    externe hors perimetre, cf. docs xtf-transfer-encoding-notes.md /
    README Known limitations), et le nom court de la classe metamodele
    concrete du Type (ex. "TextType", "NumType", "EnumType", "ReferenceType",
    "Class" - jamais l'abstrait "DomainType")."""
    attr: MetaInstance
    type_instance: MetaInstance | None
    type_kind: str | None
    mandatory: bool


def resolve_attribute(attr: MetaInstance) -> ResolvedAttribute:
    type_instance = getattr(attr, "Type", None)
    type_instance = type_instance if isinstance(type_instance, MetaInstance) else None
    type_kind = type_instance._qualified_class.rsplit(".", 1)[-1] if type_instance is not None else None
    mandatory = bool(getattr(type_instance, "Mandatory", False)) if type_instance is not None else False
    return ResolvedAttribute(attr=attr, type_instance=type_instance, type_kind=type_kind, mandatory=mandatory)


def enum_values(enum_type: MetaInstance) -> set[str]:
    """Tous les noms de EnumNode atteignables depuis EnumType.TopNode, en
    descendant recursivement EnumNode.Sub (chaine construite par
    AttachmentResolver._chain_onto_self_referencing_association - voir
    builder/attach.py ; nom de champ "Sub" confirme empiriquement, pas
    dans ilismeta16-associations.yml sous ce nom litteral - association
    reelle non identifiee avec certitude, voir note dans validate.py)."""
    values: set[str] = set()

    def walk(node: MetaInstance | None) -> None:
        if node is None:
            return
        name = getattr(node, "Name", None)
        if name:
            values.add(name)
        for child in getattr(node, "Sub", None) or []:
            walk(child)

    walk(getattr(enum_type, "TopNode", None))
    return values
