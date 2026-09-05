"""`CONTEXT <Name> = (domainRef EQ domainRef (OR domainRef)* SEMI)+` construction.

Builds the `GenericDef`/`ConcreteForGeneric` link objects a `contextDef`
represents (see spec/grammar/mapping/06_types.yml,
`contextDef.generic_concrete_pairs` for the reified-association design
this follows) - not expressible via a declarative `attribute_bindings`
entry because `ctx.domainRef()` returns every occurrence flattened into
one list, with no grouping of its own; grouping is recovered positionally
from `ctx.children`, splitting on `;`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.builder import context_access as ca
from interlis.builder.forward_refs import ForwardRef
from interlis.metamodel.instance import MetaInstance

if TYPE_CHECKING:
    from interlis.builder._builder_protocol import _BuilderHost

    _Base = _BuilderHost
else:
    _Base = object  # real runtime base - see _builder_protocol.py for why


class _ContextMixin(_Base):
    def _build_context_domain_pairs(self, context: MetaInstance, ctx: ParserRuleContext) -> None:
        """Build one `GenericDef` (+ its `ConcreteForGeneric` links) per `generic = concrete (OR concrete)*` pair-group.

        `context.GenericDef` and `generic_def.ConcreteForGeneric` are
        bookkeeping lists (no reverse role for either exists in
        ilismeta16-associations.yml - `GenericDef.Context`/`GenericDomain`
        and `ConcreteForGeneric.GenericDef`/`ConcreteDomain` are the only
        formally modeled ends), named after their own target class like
        every other IlisMeta16 child collection (e.g. `Class.ClassAttribute`),
        so `convert/jsonfg.py`'s CRS resolution can walk
        Context -> GenericDef -> ConcreteForGeneric without a dedicated index.
        """
        name_token = ca.call(ctx, "Name")
        if name_token is None:
            return
        children = list(ctx.children or [])
        start = children.index(name_token) + 1
        while start < len(children) and not (
            isinstance(children[start], TerminalNode) and children[start].symbol.type == InterlisParser.EQ
        ):
            start += 1
        start += 1
        for group in self._split_on_semi(children[start:]):
            self._build_one_generic_def(context, ctx, group)

    @staticmethod
    def _split_on_semi(children: list[Any]) -> list[list[Any]]:
        groups: list[list[Any]] = []
        current: list[Any] = []
        for child in children:
            if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.SEMI:
                if current:
                    groups.append(current)
                current = []
            else:
                current.append(child)
        if current:
            groups.append(current)
        return groups

    def _build_one_generic_def(self, context: MetaInstance, ctx: ParserRuleContext, group: list[Any]) -> None:
        domain_nodes = [c for c in group if isinstance(c, ParserRuleContext) and self._rule_name(c) == "domainRef"]
        if len(domain_nodes) < 2:
            return
        generic_node, *concrete_nodes = domain_nodes

        generic_def = self.registry.new_instance("IlisMeta16.ModelData.GenericDef")
        generic_def._source_ctx = ctx
        generic_def.Context = context
        generic_ref = self.visit(generic_node)
        generic_def.GenericDomain = [generic_ref]
        if isinstance(generic_ref, ForwardRef):
            self.forward_refs.register_pending(generic_ref, generic_def, "GenericDomain")

        context_generic_defs = getattr(context, "GenericDef", None) or []
        context_generic_defs.append(generic_def)
        context.GenericDef = context_generic_defs

        concrete_links: list[MetaInstance] = []
        for concrete_node in concrete_nodes:
            link = self.registry.new_instance("IlisMeta16.ModelData.ConcreteForGeneric")
            link._source_ctx = ctx
            link.GenericDef = [generic_def]
            concrete_ref = self.visit(concrete_node)
            link.ConcreteDomain = [concrete_ref]
            if isinstance(concrete_ref, ForwardRef):
                self.forward_refs.register_pending(concrete_ref, link, "ConcreteDomain")
            concrete_links.append(link)
        generic_def.ConcreteForGeneric = concrete_links
