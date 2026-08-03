"""AttachmentResolver : decide comment une valeur resolue par
source_resolver doit etre posee sur l'instance en cours de construction -
attribut propre (own/inherited) ou lien d'association (role de l'autre
bout). Utilise a la fois pour attribute_bindings et pour `parent`.

Ordre de resolution strict (le premier qui matche gagne) :
1. association+role explicites fournis par l'appelant (binding ou `parent`).
2. la cle == un attribut own/inherited de la classe de l'instance -> assignation directe.
3. recherche dans ilismeta16-associations.yml d'une association dont un
   bout cible la classe (ou une superclasse via all_superclasses) et dont
   l'AUTRE bout a pour role la cle -> lien d'association.
4. aucun match -> BuildError explicite (defensif ; ne devrait plus arriver
   apres phase 2, mais le moteur doit rester robuste face a un futur
   binding mal forme)."""
from interlis.builder.errors import BuildError
from interlis.metamodel.instance import MetaInstance
from interlis.spec.uml_index import UmlIndex


class AttachmentResolver:
    def __init__(self, uml: UmlIndex):
        self.uml = uml

    def attach(
        self,
        instance: MetaInstance,
        key: str,
        value,
        *,
        association: str | None = None,
        role: str | None = None,
        rule: str = "",
    ) -> None:
        if association and role:
            self._attach_via_named_association(instance, association, role, value, rule=rule)
            return

        element = self.uml.qualified.get(instance._qualified_class, {})
        if self.uml.attribute_exists(element, key):
            setattr(instance, key, value)
            return

        found = self._find_association_for_role(instance._qualified_class, key)
        if found is not None:
            _assoc_name, other_role, upper = found
            self._set_field(instance, other_role, value, upper)
            return

        raise BuildError(
            f"impossible d'attacher {key!r} sur {instance._qualified_class} "
            f"(ni attribut own/inherited, ni role d'association trouve)",
            rule=rule,
        )

    def _attach_via_named_association(self, instance, association, role, value, *, rule):
        assoc_el = self.uml.find_association_by_name(association)
        if assoc_el is None:
            raise BuildError(f"association {association!r} introuvable", rule=rule)
        upper = None
        for end in assoc_el.get("ends", []):
            if end.get("role") == role:
                upper = end.get("upper")

        if assoc_el.get("qualified_name") == instance._qualified_class:
            # `instance` EST une instance de CETTE association elle-meme
            # (ex. Import, construite comme un objet-lien via
            # InterlisModelBuilder.wrap_bag_as_instance/for_each - voir
            # registry.MetamodelRegistry._build_association_class) : chaque
            # bout d'une instance de lien vaut EXACTEMENT une valeur, meme
            # si la multiplicite '*' de l'autre cote de l'association decrit
            # combien de LIENS DIFFERENTS peuvent partager le meme Package,
            # pas combien de valeurs CETTE instance-ci porte pour ce role.
            setattr(instance, role, value)
            return

        current = getattr(instance, role, None)
        if upper != "*" and current is not None and isinstance(current, MetaInstance):
            # Role singulier deja occupe (ex. EnumType.TopNode, upper=1) :
            # plusieurs elements successifs (ex. enumElement x N, chacun
            # s'auto-attachant via son propre parent: TopNode) doivent former
            # une CHAINE, pas ecraser le precedent - chercher une association
            # auto-referente sur la classe de `value` (meme pattern que
            # SubNode : ParentNode<->Node) et y accrocher `value` a la SUITE
            # de la chaine plutot que de le perdre.
            chained = self._chain_onto_self_referencing_association(current, value, rule)
            if chained:
                return
        self._set_field(instance, role, value, upper)

    def _chain_onto_self_referencing_association(self, head: MetaInstance, value: MetaInstance, rule: str) -> bool:
        """Cherche, sur la classe de `head` (une instance deja attachee a un
        role singulier), une association ou les DEUX bouts ciblent cette
        meme classe (ex. SubNode : ParentNode<->Node sur EnumNode) - avance
        jusqu'a la fin de la chaine existante (via le bout multiple) puis y
        attache `value`. Retourne False si aucune telle association n'existe
        (l'appelant retombe alors sur un simple remplacement)."""
        own_classes = {head._qualified_class, *(self.uml.qualified.get(head._qualified_class, {}).get("all_superclasses") or [])}
        for assoc_el in self.uml.qualified.values():
            if assoc_el.get("kind") != "Association":
                continue
            ends = assoc_el.get("ends", [])
            if len(ends) != 2:
                continue
            multi_end = next((e for e in ends if e.get("upper") == "*" and e.get("target") in own_classes), None)
            single_end = next((e for e in ends if e is not multi_end and e.get("target") in own_classes), None)
            if multi_end is None or single_end is None:
                continue
            node = head
            children = getattr(node, multi_end["role"], None) or []
            while children:
                node = children[-1]
                children = getattr(node, multi_end["role"], None) or []
            self._set_field(node, multi_end["role"], value, "*")
            return True
        return False

    def _find_association_for_role(self, qualified_class: str, role: str) -> tuple[str, str, str] | None:
        element = self.uml.qualified.get(qualified_class, {})
        own_classes = {qualified_class, *(element.get("all_superclasses") or [])}
        for assoc_el in self.uml.qualified.values():
            if assoc_el.get("kind") != "Association":
                continue
            ends = assoc_el.get("ends", [])
            if len(ends) != 2:
                continue
            for i, end in enumerate(ends):
                other = ends[1 - i]
                if end.get("target") in own_classes and other.get("role") == role:
                    return assoc_el.get("name"), other.get("role"), other.get("upper")
        return None

    # Associations structurelles generiques (deja gerees separement - extends/
    # all_superclasses pour Inheritance, arbre de composition pour
    # PackageElements/MetaAttributes) : trop promiscuces pour ce fallback
    # (elles relient quasi n'importe quelle paire de classes via
    # all_superclasses) - exclues de la recherche par connexion de classes.
    _GENERIC_ASSOCIATIONS = {"Inheritance", "PackageElements", "MetaAttributes"}

    def find_association_connecting(self, from_class: str, to_class: str) -> tuple[str, str, str] | None:
        """Cherche une association qui relie deux classes CONNUES (pas une
        recherche par nom de role) - utilise quand un enfant non reclame par
        attribute_bindings (sweep, voir InterlisModelBuilder) produit un
        resultat dont la regle propre n'a pas de `parent:` (ex. attrTypeDef,
        dont la note dit explicitement "feeds the enclosing
        AttrOrParamType.Type association end (handled by the parent
        construct)" - la regle englobante doit deviner l'association a
        partir des DEUX classes concretes, pas d'un nom de cle). Prefere une
        correspondance EXACTE (classes reelles, pas superclasses) ; ne
        retombe sur all_superclasses que si aucune correspondance exacte
        n'existe, pour eviter de matcher une association trop generale.
        Retourne (nom, role_cote_to_class, upper_cote_to_class), ou None si
        aucune association ne relie ces deux classes."""
        exact = self._search_connecting(from_class, {from_class}, to_class, {to_class})
        if exact is not None:
            return exact
        from_el = self.uml.qualified.get(from_class, {})
        from_classes = {from_class, *(from_el.get("all_superclasses") or [])}
        to_el = self.uml.qualified.get(to_class, {})
        to_classes = {to_class, *(to_el.get("all_superclasses") or [])}
        return self._search_connecting(from_class, from_classes, to_class, to_classes)

    def _search_connecting(self, from_class, from_classes, to_class, to_classes) -> tuple[str, str, str] | None:
        # Plusieurs associations peuvent relier les deux memes classes (ex.
        # AttrOrParamType vs LocalType, toutes deux AttrOrParam<->Type) :
        # preferer une reference simple (aggregation: none) a une
        # composition (aggregation: composite, semantique de type imbrique
        # localement, cas plus specifique/rare) - a defaut, ordre stable
        # par nom d'association pour un resultat deterministe.
        candidates: list[tuple[str, str, str, str]] = []  # (aggregation, name, role, upper)
        for assoc_el in self.uml.qualified.values():
            if assoc_el.get("kind") != "Association" or assoc_el.get("name") in self._GENERIC_ASSOCIATIONS:
                continue
            ends = assoc_el.get("ends", [])
            if len(ends) != 2:
                continue
            for i, end in enumerate(ends):
                other = ends[1 - i]
                if end.get("target") in from_classes and other.get("target") in to_classes:
                    candidates.append((other.get("aggregation") or "none", assoc_el.get("name"), other.get("role"), other.get("upper")))
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c[0] == "composite", c[1]))
        _aggregation, name, role, upper = candidates[0]
        return name, role, upper

    @staticmethod
    def _set_field(instance, field: str, value, upper: str | None) -> None:
        if upper == "*":
            current = getattr(instance, field, None)
            if current is None:
                current = []
                setattr(instance, field, current)
            if isinstance(value, list):
                current.extend(value)
            else:
                current.append(value)
        else:
            setattr(instance, field, value)
