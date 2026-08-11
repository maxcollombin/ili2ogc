"""AttachmentResolver: decide how a resolved value attaches to an instance.

Decides how a value resolved by source_resolver attaches to the instance
being built: as an own/inherited attribute, or as an association link.
Used for both attribute_bindings and `parent`.

Resolution order (first match wins):
1. Explicit association+role given by the caller (binding or `parent`).
2. Key matches an own/inherited attribute of the instance's class -> direct assignment.
3. An association in ilismeta16-associations.yml has one end targeting the
   class (or a superclass via all_superclasses) and the other end's role
   matches the key -> association link.
4. No match -> explicit BuildError (defensive: should not happen for a
   well-formed binding, but keeps the engine robust against a future one).
"""
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
            # `instance` IS an instance of THIS association itself
            # (e.g. Import, built as a link object via
            # InterlisModelBuilder.wrap_bag_as_instance/for_each - see
            # registry.MetamodelRegistry._build_association_class): each
            # end of a link instance holds EXACTLY one value, even if the
            # other side's '*' multiplicity describes how many DIFFERENT
            # links can share the same Package, not how many values THIS
            # instance carries for this role.
            setattr(instance, role, value)
            return

        current = getattr(instance, role, None)
        if upper != "*" and current is not None and isinstance(current, MetaInstance):
            # Singular role already occupied (e.g. EnumType.TopNode, upper=1):
            # several successive elements (e.g. enumElement x N, each
            # self-attaching via its own parent: TopNode) must form a CHAIN,
            # not overwrite the previous one - look for a self-referencing
            # association on `value`'s class (same pattern as
            # SubNode: ParentNode<->Node) and attach `value` at the END of
            # the chain rather than losing it.
            chained = self._chain_onto_self_referencing_association(current, value, rule)
            if chained:
                return
        self._set_field(instance, role, value, upper)

    def _chain_onto_self_referencing_association(self, head: MetaInstance, value: MetaInstance, rule: str) -> bool:
        """Chain `value` onto an existing self-referencing association, if any.

        Looks, on `head`'s class (an instance already attached to a singular
        role), for an association where BOTH ends target that same class
        (e.g. SubNode: ParentNode<->Node on EnumNode) - walks to the end of
        the existing chain (via the multi end) then attaches `value` there.
        Returns False if no such association exists (the caller then falls
        back to a plain replacement).
        """
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

    # Generic structural associations (already handled separately - extends/
    # all_superclasses for Inheritance, composition tree for
    # PackageElements/MetaAttributes): too promiscuous for this fallback
    # (they connect almost any pair of classes via all_superclasses) -
    # excluded from the class-connection search.
    _GENERIC_ASSOCIATIONS = {"Inheritance", "PackageElements", "MetaAttributes"}

    def find_association_connecting(self, from_class: str, to_class: str) -> tuple[str, str, str] | None:
        """Find an association connecting two known classes.

        Unlike `_find_association_for_role`, this isn't a role-name lookup -
        it's used when a child not claimed by attribute_bindings (sweep, see
        InterlisModelBuilder) produces a result whose own rule has no
        `parent:` (e.g. attrTypeDef, whose note explicitly says "feeds the
        enclosing AttrOrParamType.Type association end (handled by the
        parent construct)"): the enclosing rule must guess the association
        from the two concrete classes, not from a key name. Prefers an
        EXACT match (real classes, not superclasses); only falls back to
        all_superclasses if no exact match exists, to avoid matching an
        overly general association. Returns (name, role_on_to_class_side,
        upper_on_to_class_side), or None if no association connects these
        two classes.
        """
        exact = self._search_connecting(from_class, {from_class}, to_class, {to_class})
        if exact is not None:
            return exact
        from_el = self.uml.qualified.get(from_class, {})
        from_classes = {from_class, *(from_el.get("all_superclasses") or [])}
        to_el = self.uml.qualified.get(to_class, {})
        to_classes = {to_class, *(to_el.get("all_superclasses") or [])}
        return self._search_connecting(from_class, from_classes, to_class, to_classes)

    def _search_connecting(self, from_class, from_classes, to_class, to_classes) -> tuple[str, str, str] | None:
        # Several associations can link the same two classes (e.g.
        # AttrOrParamType vs LocalType, both AttrOrParam<->Type): prefer a
        # plain reference (aggregation: none) over a composition
        # (aggregation: composite, locally-nested-type semantics, a more
        # specific/rarer case) - otherwise, stable order by association
        # name for a deterministic result.
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
