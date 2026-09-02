"""OID clauses (classDef's own `OID AS .../NO OID`, topicDef's `BASKET OID AS .../OID AS ...`).

Mixed into `InterlisModelBuilder` - see `_scan_oid_clauses` for why these
need a positional scan rather than a declarative attribute_binding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.builder.forward_refs import ForwardRef
from interlis.metamodel.instance import MetaInstance

if TYPE_CHECKING:
    from interlis.builder._builder_protocol import _BuilderHost

    _Base = _BuilderHost
else:
    _Base = object  # real runtime base - see _builder_protocol.py for why


class _OidMixin(_Base):
    def _scan_oid_clauses(self, ctx: ParserRuleContext, rule_name: str) -> list[dict]:
        """Positionally scan `ctx.children` for OID clauses (classDef/topicDef).

        Both rules inline the SAME grammar fragment for an OID
        declaration - `(BASKET)? OID AS <domain-ref> SEMI` (topicDef, up
        to 2 occurrences: an optional basket-level clause then an
        optional class-default one) or `(OID AS <domain-ref> | NO OID)
        SEMI` (classDef, at most 1) - never delegated to a sub-rule
        (confirmed by direct read of ClassDefContext/TopicDefContext via
        `mappings/antlr-rule-index.yml`, RULE #2bis: neither exposes a
        `domainRef()` accessor, only bare `OID`/`AS`/`Name`/`DOT`/
        `UUIDOID`/`INTERLIS`/`ANYOID`/`NO`/`BASKET` tokens), so there's no
        accessor to bind declaratively - walked positionally instead
        (`ctx.getAltNumber()` is unusable here too, always 0 for this
        vendored grammar, see source_resolver.py).

        Telling a basket-level clause from a class-default one when a
        topic has exactly ONE bare `OID AS` clause (no `BASKET` keyword)
        can't rely on which internal ANTLR alternative fired - both
        produce an IDENTICAL parse tree (same tokens), so nothing in the
        tree records that choice. BASKET presence at the position
        immediately preceding `OID` is the only reliable, OBSERVABLE
        signal - confirmed against real corpus evidence
        (LWB_Perimeter_Terrassenreben_V2_0.ili: `TOPIC Terrassenreben =
        OID AS INTERLIS.UUIDOID;`, no BASKET keyword, immediately followed
        by `CLASS Bezugsjahr` with no OID clause of its own - for that
        class to get ANY identifier at all, per eCH-0031 V2.1.0 §3.5.2's
        "sofern bei der jeweiligen Klasse keine spezifische Definition
        dafuer gemacht wird", this bare clause must be the class-default,
        not a basket-only one; LWB_Perimeter_LandwirtschaftlicheNutzflaeche_
        Soemmerung_V2_0.ili confirms both clauses can coexist in the fixed
        `BASKET OID AS X; OID AS Y;` order the manual's citation shows).

        Returns clauses in source order, each `{"basket": bool, "no_oid":
        bool, "value": ForwardRef | MetaInstance | None}` (`value` is
        `None` only for a `no_oid` clause).
        """
        children = list(ctx.children or [])
        clauses: list[dict] = []
        i, n = 0, len(children)
        while i < n:
            child = children[i]
            if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.NO:
                clauses.append({"basket": False, "no_oid": True, "value": None})
                i += 2  # NO, OID
                continue
            if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.OID:
                basket = (
                    i > 0
                    and isinstance(children[i - 1], TerminalNode)
                    and children[i - 1].symbol.type == InterlisParser.BASKET
                )
                j = i + 2  # skip OID, AS
                ref_tokens: list[Any] = []
                while j < n and not (
                    isinstance(children[j], TerminalNode) and children[j].symbol.type == InterlisParser.SEMI
                ):
                    ref_tokens.append(children[j])
                    j += 1
                value = self._resolve_oid_domain_ref(ref_tokens, ctx, rule_name)
                clauses.append({"basket": basket, "no_oid": False, "value": value})
                i = j
                continue
            i += 1
        return clauses

    def _resolve_oid_domain_ref(self, tokens: list[Any], ctx: ParserRuleContext, rule_name: str) -> Any:
        """Resolve one OID clause's domain-ref tokens (between AS and SEMI).

        `UUIDOID`/`ANYOID` (bare or `INTERLIS.`-qualified) are reserved
        lexer tokens, never resolvable via a name lookup like an ordinary
        domain - built directly as a bare
        `AnyOIDType` marker instead, same construction as `oIDType`'s own
        "OID ANY"/"UUIDOID" alternative (spec/grammar/mapping/06_types.yml:
        "no ANY/UUIDOID distinction is carried"). Otherwise (`Name` /
        `Name DOT Name` / `INTERLIS DOT Name`), a genuine named domain
        reference - `ForwardRef`, same dot-join convention as
        `_build_control_points_ref`.
        """
        if any(
            isinstance(t, TerminalNode) and t.symbol.type in (InterlisParser.UUIDOID, InterlisParser.ANYOID)
            for t in tokens
        ):
            return self.registry.new_instance("IlisMeta16.ModelData.AnyOIDType")
        names = [t.getText() for t in tokens if isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.Name]
        if not names:
            return None
        interlis_prefixed = any(
            isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.INTERLIS for t in tokens
        )
        qualified = ".".join((["INTERLIS"] if interlis_prefixed else []) + names)
        return ForwardRef(
            name=qualified,
            rule=rule_name,
            home_model=self._current_model_name(),
            topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _attach_class_oid(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Attach classDef's own `OID AS <domain-ref>` / `NO OID` clause, if present.

        `ObjectOID` (Class <-> Oid:DomainType, ilismeta16-associations.yml).
        Marks `instance._own_oid_clause` whenever a clause is present at
        all (even `NO OID`, or one whose domain-ref failed to resolve to
        anything) - `_apply_topic_oid_clauses`'s class-default propagation
        must never override a class that already made its own decision,
        per eCH-0031 V2.1.0 §3.5.2 ("sofern bei der jeweiligen Klasse
        keine spezifische Definition dafuer gemacht wird").
        """
        clauses = self._scan_oid_clauses(ctx, "classDef")
        if not clauses:
            return
        instance._own_oid_clause = True
        value = clauses[0]["value"]
        if value is None:
            return
        self.attachment.attach(instance, "Oid", value, association="ObjectOID", role="Oid", rule="classDef")
        if isinstance(value, ForwardRef):
            self.forward_refs.register_pending(value, instance, "Oid")

    def _apply_topic_oid_clauses(self, ctx: ParserRuleContext, instances: dict[str, MetaInstance]) -> None:
        """Wire topicDef's OID clauses (see `_scan_oid_clauses`).

        The basket-level clause (`BASKET OID AS ...`) feeds `BasketOID`
        (DataUnit <-> Oid:DomainType) directly. The class-default clause
        (bare `OID AS ...`) is propagated to every `Class` (Kind='Class' -
        excludes STRUCTURE, which shares the same metamodel class but is
        never directly identified) declared in this topic that carries no
        `_own_oid_clause` of its own (`_attach_class_oid`) - same
        `ObjectOID` association as an explicit per-class clause, per eCH-
        0031 V2.1.0 §3.5.2's default-value semantics.
        """
        clauses = self._scan_oid_clauses(ctx, "topicDef")
        data_unit = instances.get("DataUnit")
        basket_clause = next((c for c in clauses if c["basket"]), None)
        if basket_clause is not None and basket_clause["value"] is not None and data_unit is not None:
            value = basket_clause["value"]
            self.attachment.attach(data_unit, "Oid", value, association="BasketOID", role="Oid", rule="topicDef")
            if isinstance(value, ForwardRef):
                self.forward_refs.register_pending(value, data_unit, "Oid")

        default_clause = next((c for c in clauses if not c["basket"]), None)
        if default_clause is None or default_clause["value"] is None:
            return
        # classDef's `parent: {association: PackageElements, role:
        # Element}` attaches every Class to SubModel, not DataUnit
        # (topicDef.definitions is an UNPREFIXED binding key, applied to
        # the FIRST of the two linked instances, i.e. SubModel - confirmed
        # empirically, DataUnit.Element is always empty).
        container = instances.get("SubModel")
        if container is None:
            return
        elements = getattr(container, "Element", None) or []
        if isinstance(elements, MetaInstance):
            elements = [elements]
        for element in elements:
            if not isinstance(element, MetaInstance):
                continue
            if element._qualified_class != "IlisMeta16.ModelData.Class" or getattr(element, "Kind", None) != "Class":
                continue
            if getattr(element, "_own_oid_clause", False):
                continue
            value = default_clause["value"]
            self.attachment.attach(element, "Oid", value, association="ObjectOID", role="Oid", rule="topicDef")
            if isinstance(value, ForwardRef):
                self.forward_refs.register_pending(value, element, "Oid")
