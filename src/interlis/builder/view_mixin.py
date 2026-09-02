"""VIEW construction: FormationKind, ALL OF expansion, bare attribute assignments, VIEW attribute type resolution.

Mixed into `InterlisModelBuilder` - called from `_build_instance`'s
`viewDef` branch (immediate hooks) and `build()` (the post-`resolve_all()`
pending-queue drains, `ALL OF`/bare-attribute typing both need
`RenamedBaseView.BaseView` already resolved).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.builder import context_access as ca
from interlis.metamodel.instance import MetaInstance

if TYPE_CHECKING:
    from interlis.builder._builder_protocol import _BuilderHost

    _Base = _BuilderHost
else:
    _Base = object  # real runtime base - see _builder_protocol.py for why


class _ViewBuildingMixin(_Base):
    _FORMATION_KIND_BY_SUBRULE = {
        "projection": "Projection",
        "join": "Join",
        "union": "Union",
        "aggregation": "Aggregation",
        "inspection": "Inspection",
    }

    def _set_view_formation_kind(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Set View.FormationKind from which formationDef() alternative matched.

        By the time this runs, the unclaimed-children sweep in
        `_build_instance` has already visited `formationDef()` (a pure
        dispatcher) and built every `RenamedBaseView` reachable from it -
        nothing left to VISIT, so this reads the raw `ViewDefContext`
        directly instead. A view using `EXTENDS viewRef` has no
        `FormationKind` of its own to set - left unset, inherited from
        the extended view.
        """
        formation = ca.call(ctx, "formationDef")
        if formation is None:
            return
        for subrule, value in self._FORMATION_KIND_BY_SUBRULE.items():
            if ca.has_accessor(formation, subrule) and ca.call(formation, subrule) is not None:
                instance.FormationKind = value
                if subrule == "join":
                    self._set_join_or_null(instance, ca.call(formation, subrule))
                elif subrule == "inspection":
                    self._stash_inspection_path(instance, ca.call(formation, subrule))
                elif subrule == "aggregation":
                    self._stash_aggregation_key(instance, ca.call(formation, subrule))
                return

    def _stash_aggregation_key(self, view: MetaInstance, aggregation_ctx: ParserRuleContext) -> None:
        """Record `AGGREGATION OF ... EQUAL(uniqueEl)`'s grouping key as `view._aggregation_key`.

        The generic `Container` resolution machinery only materializes
        `uniqueEl` when a parent instance's own attribute_bindings drive
        the merge - called directly here instead (no such parent in
        progress), so it's materialized by hand: a fresh
        `PathOrInspFactor` + `_merge_bag_into_instance`. `ALL` (no
        `uniqueEl`) leaves `_aggregation_key` unset.
        """
        unique_el = ca.call(aggregation_ctx, "uniqueEl")
        if unique_el is None:
            return
        path = ca.call(unique_el, "objectOrAttributePath")
        if path is None:
            return
        bag = self.visit(path)
        factor = self.registry.new_instance("IlisMeta16.ModelData.PathOrInspFactor")
        self._merge_bag_into_instance(factor, bag, "aggregation")
        view._aggregation_key = factor

    @staticmethod
    def _stash_inspection_path(view: MetaInstance, inspection_ctx: ParserRuleContext) -> None:
        """Record the `-> Name (-> Name)*` chain of an `INSPECTION OF base -> attr` view.

        The spec's `inspection.FormationParameter` binding (a
        `PathOrInspFactor` from the `Name` chain) is not yet materialized
        by the generic `Container` machinery - stash the raw attribute
        names on `view._inspection_path` so `convert/jsonfg.evaluate_view`
        can iterate the inspected attribute. `ctx.Name()` returns every
        `Name` token of the rule (`(MINUS GT Name)+`), in order.
        """
        names = ca.call(inspection_ctx, "Name")
        if names is None:
            return
        tokens = names if isinstance(names, list) else [names]
        view._inspection_path = [t.getText() for t in tokens if t is not None]

    def _set_join_or_null(self, view: MetaInstance, join_ctx: ParserRuleContext) -> None:
        """Set `RenamedBaseView.OrNull` for every base of a `JOIN OF` carrying a trailing `(OR NULL)`.

        `join()`'s grammar attaches an optional `(OR NULL)` to the
        IMMEDIATELY PRECEDING `renamedViewableRef` positionally - never
        to the 1st base. `renamedViewableRef()` has no accessor of its
        own for this sibling token, so this reads `join_ctx.children`
        directly. Previously never implemented (no real corpus `(OR
        NULL)` occurrence existed to surface the gap).
        """
        bases = [b for b in getattr(view, "RenamedBaseView", None) or [] if isinstance(b, MetaInstance)]
        children = list(join_ctx.children or [])
        for i, ref_ctx in enumerate(ca.call_list(join_ctx, "renamedViewableRef")):
            if i >= len(bases):
                break
            idx = children.index(ref_ctx)
            if idx + 1 < len(children) and children[idx + 1].getText() == "(":
                bases[i].OrNull = True

    def _expand_view_all_of(self, view: MetaInstance, ctx: ParserRuleContext) -> None:
        """Record a View's `ATTRIBUTE ALL OF <Name>;` for deferred expansion.

        `viewAttributes()`'s own `attribute_bindings` never produce a
        `ClassAttr` for this bare-token alternative. Only records here
        rather than expanding immediately: `RenamedBaseView.BaseView` can
        still be an unresolved `ForwardRef` at this point. Real expansion
        happens in `_apply_pending_view_all_of`, after `resolve_all()`.
        """
        va = ca.call(ctx, "viewAttributes")
        if va is None or not ca.has_accessor(va, "ALL") or ca.call(va, "ALL") is None:
            return
        self._pending_view_all_of.append((view, ctx))

    def _apply_pending_view_all_of(self) -> None:
        """Expand every View recorded by `_expand_view_all_of`, once refs are resolved.

        Only OWN attributes of the matched base are walked - inherited
        attributes via EXTENDS are not (no real corpus evidence yet, and
        duplicating `xtf.schema.attributes_of`'s walk here would cross a
        layering boundary). `viewAttributes()`'s grammar loops, so ANY
        number of "ALL OF" clauses can be freely interleaved with
        redefinitions in the SAME `ViewAttributesContext` - walks
        `va.children` positionally to pair each `ALL` terminal with the
        `Name` immediately following its `OF`, the only reliable way to
        tell which name belongs to which `ALL OF`.
        """
        pending = self._pending_view_all_of
        self._pending_view_all_of = []
        for view, ctx in pending:
            va = ca.call(ctx, "viewAttributes")
            for all_of_name in self._all_of_base_names(va):
                base = self._find_renamed_base_view(view, all_of_name)
                if base is None or not isinstance(base.BaseView, MetaInstance):
                    continue
                for attr in getattr(base.BaseView, "ClassAttribute", None) or []:
                    copy = self.registry.new_instance("IlisMeta16.ModelData.AttrOrParam")
                    copy.Name = attr.Name
                    self.attachment.attach(
                        copy, "Type", attr.Type, association="AttrOrParamType", role="Type", rule="viewAttributes"
                    )
                    copy.Derivates = [self._identity_view_path(all_of_name, attr.Name)]
                    self.attachment.attach(
                        view,
                        "ClassAttribute",
                        copy,
                        association="ClassAttr",
                        role="ClassAttribute",
                        rule="viewAttributes",
                    )
            self._reorder_view_class_attributes(view, va)

    @staticmethod
    def _reorder_view_class_attributes(view: MetaInstance, va: ParserRuleContext | None) -> None:
        """Reorder `view.ClassAttribute` to match `viewAttributes()`'s own SOURCE order.

        An `ATTRIBUTE ALL OF <Base>;` expansion and a bare `Name :=
        expression` redefinition attach to `ClassAttribute` at DIFFERENT
        TIMES during `build()` (bare ones immediately; `ALL OF` only
        after `resolve_all()`) - so `ClassAttribute` used to end up with
        every bare-assign attribute BEFORE every `ALL OF` expansion,
        regardless of source order. A real `.xtf` writer needs the true
        order (refman §4.3.7's "Zwiebelprinzip" - a compiled XSD
        `xsd:sequence` rejects an out-of-order instance, confirmed
        empirically). Reorders by the position of each attribute's OWN
        declaring token in `va.children`, matched back to the
        already-built `ClassAttribute` entries by `_all_of_identity` or
        by name.
        """
        if va is None:
            return
        attrs = list(getattr(view, "ClassAttribute", None) or [])
        if not attrs:
            return
        current_index = {id(a): i for i, a in enumerate(attrs)}
        by_name: dict[str | None, list[MetaInstance]] = {}
        for a in attrs:
            by_name.setdefault(getattr(a, "Name", None), []).append(a)

        children = list(va.children or [])
        all_of_indices: dict[int, str] = {}
        for node in ca.call_list(va, "ALL"):
            idx = children.index(node)
            if idx + 2 < len(children):
                all_of_indices[idx] = children[idx + 2].getText()

        position_of_id: dict[int, int] = {}
        consumed_by_name: dict[str, int] = {}
        slot = 0
        i, n = 0, len(children)
        while i < n:
            if i in all_of_indices:
                base_name = all_of_indices[i]
                for a in attrs:
                    if id(a) in position_of_id:
                        continue
                    derivates = getattr(a, "Derivates", None) or []
                    if not derivates or not getattr(derivates[0], "_all_of_identity", False):
                        continue
                    path_els = getattr(derivates[0], "PathEls", None) or []
                    if path_els and getattr(path_els[0], "Ref", None) == base_name:
                        position_of_id[id(a)] = slot
                        slot += 1
                i += 3  # ALL OF Name
                continue
            node = children[i]
            if isinstance(node, TerminalNode) and node.symbol.type == InterlisParser.Name:
                name = node.getText()
                candidates = by_name.get(name) or []
                used = consumed_by_name.get(name, 0)
                if used < len(candidates):
                    position_of_id[id(candidates[used])] = slot
                    consumed_by_name[name] = used + 1
                    slot += 1
            i += 1

        attrs.sort(key=lambda a: position_of_id.get(id(a), 1_000_000 + current_index[id(a)]))
        setattr(view, "ClassAttribute", attrs)  # noqa: B010 - dynamic MetaInstance field, no static attribute to assign

    def _identity_view_path(self, base_ref: str, attr_name: str) -> MetaInstance:
        """Build the `<base> -> <attr>` identity `PathOrInspFactor` for one `ALL OF` view attribute.

        `ALL OF <base>` re-exports each base attribute unchanged; a
        consumer that projects a view by expression rather than by name
        (`convert/sql/views.py`'s `CREATE VIEW` `SELECT`) needs the same
        `Derivates` a `<attr> := <base> -> <attr>` redefinition would
        carry. `Type` is still set directly from the base attribute, so
        this path is never walked for type resolution.
        """
        factor = self.registry.new_instance("IlisMeta16.ModelData.PathOrInspFactor")
        els: list[MetaInstance] = []
        for ref in (base_ref, attr_name):
            el = self.registry.new_instance("IlisMeta16.ModelData.PathEl")
            el.Kind = "ReferenceAttr"
            el.Ref = ref
            els.append(el)
        factor.PathEls = els
        # Lets a converter tell an `ALL OF` pass-through from an explicit
        # `Name := expression`: the former may drop an attribute it cannot
        # project (e.g. a STRUCTURE with no single column) as a note, the
        # latter was asked for by name and must fail loudly.
        factor._all_of_identity = True
        return factor

    @staticmethod
    def _all_of_base_names(va: ParserRuleContext) -> list[str]:
        """Extract every "ALL OF <Name>" base name from a ViewAttributesContext, in order.

        Positional walk: an `ALL` terminal is always immediately followed
        by `OF` then the base `Name` - `Name` alone isn't enough to
        disambiguate, since the SAME accessor is also used by the
        unrelated `Name ':=' expression` alternative in the same group.
        """
        if not ca.has_accessor(va, "ALL"):
            return []
        children = list(va.children or [])
        names = []
        for node in ca.call_list(va, "ALL"):
            i = children.index(node)
            if i + 2 < len(children):
                names.append(children[i + 2].getText())
        return names

    @staticmethod
    def _find_renamed_base_view(view: MetaInstance, name: str) -> MetaInstance | None:
        """Find `view`'s RenamedBaseView referred to by "ALL OF <name>"/"<name> ASSIGN ...".

        `name` is the base's rename alias if one was given, or - the
        majority real-world case - the base Class's own short name when
        no alias was used.
        """
        for base in getattr(view, "RenamedBaseView", None) or []:
            base_view = base.BaseView if isinstance(base.BaseView, MetaInstance) else None
            candidate = base.Name or (base_view.Name if base_view is not None else None)
            if candidate == name:
                return base
        return None

    _VIEW_ATTRIBUTE_MODIFIER_TOKENS = (
        InterlisParser.ABSTRACT,
        InterlisParser.EXTENDED,
        InterlisParser.FINAL,
        InterlisParser.TRANSIENT,
    )

    @staticmethod
    def _bare_view_attribute_assignments(va: ParserRuleContext) -> list[tuple[str, set[int], ParserRuleContext]]:
        """Extract every "Name (Properties<...>)? ASSIGN expression SEMI" occurrence from a ViewAttributesContext.

        `viewAttributes()`'s 3rd alternative - real corpus proof:
        `tests/fixtures/fgdm4gs/` (5 real VIEW models, none use `ALL OF`).
        Same positional-walk technique as `_all_of_base_names`: a
        top-level `Name` terminal belongs to THIS alternative unless it's
        the base name of an "ALL OF Name" triple. Scans forward from each
        such `Name` to its `ASSIGN`, collecting any modifier token met
        along the way.
        """
        children = list(va.children or [])
        all_of_name_ids = set()
        for node in ca.call_list(va, "ALL"):
            i = children.index(node)
            if i + 2 < len(children):
                all_of_name_ids.add(id(children[i + 2]))

        results: list[tuple[str, set[int], ParserRuleContext]] = []
        i, n = 0, len(children)
        while i < n:
            node = children[i]
            if (
                isinstance(node, TerminalNode)
                and node.symbol.type == InterlisParser.Name
                and id(node) not in all_of_name_ids
            ):
                modifier_tokens: set[int] = set()
                j = i + 1
                while j < n:
                    child = children[j]
                    if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.ASSIGN:
                        break
                    if isinstance(child, TerminalNode) and child.symbol.type == InterlisParser.SEMI:
                        j = -1
                        break
                    if (
                        isinstance(child, TerminalNode)
                        and child.symbol.type in _ViewBuildingMixin._VIEW_ATTRIBUTE_MODIFIER_TOKENS
                    ):
                        modifier_tokens.add(child.symbol.type)
                    j += 1
                if j != -1 and j < n and j + 1 < n and isinstance(children[j + 1], ParserRuleContext):
                    results.append((node.getText(), modifier_tokens, children[j + 1]))
                    i = j + 2
                    continue
            i += 1
        return results

    def _build_view_bare_attributes(self, view: MetaInstance, ctx: ParserRuleContext) -> None:
        """Build one `AttrOrParam` per `viewAttributes()` "Name := expression" occurrence.

        Reference Manual 2006-04-13 §2.15: "such definitions are always
        final" - `Final=True` unconditionally. Of the 4 possible modifier
        tokens, only `TRANSIENT` maps onto a confirmed `AttrOrParam`
        attribute; `ABSTRACT`/`EXTENDED` are never guessed at (RULE #5),
        and no real corpus example uses the optional bracket at all.
        `Type` is NOT set here - `RenamedBaseView.BaseView` can still be
        unresolved; recorded in `_pending_view_bare_attrs` for
        `_apply_pending_view_bare_attr_types`, after `resolve_all()`.
        """
        va = ca.call(ctx, "viewAttributes")
        if va is None:
            return
        for name, modifier_tokens, expr_ctx in self._bare_view_attribute_assignments(va):
            expr = self.visit(expr_ctx)
            attr = self.registry.new_instance("IlisMeta16.ModelData.AttrOrParam")
            attr.Name = name
            attr.Final = True
            if InterlisParser.TRANSIENT in modifier_tokens:
                attr.Transient = True
            if expr is not None:
                attr.Derivates = [expr]
            self.attachment.attach(
                view,
                "ClassAttribute",
                attr,
                association="ClassAttr",
                role="ClassAttribute",
                rule="viewAttributes",
            )
            if expr is not None:
                self._pending_view_bare_attrs.append((view, attr, expr))

    def _apply_pending_view_bare_attr_types(self) -> None:
        """Resolve `Type` for every `AttrOrParam` recorded by `_build_view_bare_attributes`.

        Walks the assigned expression's `PathOrInspFactor.PathEls`
        statically against the metamodel (no XTF instance data needed -
        real-data evaluation is `convert/jsonfg.py`'s separate concern).
        First `PathEl` selects the `RenamedBaseView`; each subsequent
        `PathEl` is tried first as a plain `ClassAttribute` by name (JOIN
        OF), then as an association `Role` (PROJECTION OF an
        ASSOCIATION). Only the LAST `PathEl` may resolve `Type`. An
        expression that isn't a plain path, or fails to resolve at any
        hop, leaves `Type` unset (RULE #5), not a crash.
        """
        pending = self._pending_view_bare_attrs
        self._pending_view_bare_attrs = []
        for view, attr, expr in pending:
            type_instance = self._resolve_view_attribute_type(view, expr)
            if type_instance is not None:
                self.attachment.attach(
                    attr,
                    "Type",
                    type_instance,
                    association="AttrOrParamType",
                    role="Type",
                    rule="viewAttributes",
                )

    @classmethod
    def _resolve_view_attribute_type(cls, view: MetaInstance, expr: MetaInstance) -> MetaInstance | None:
        if not expr._qualified_class.endswith("PathOrInspFactor"):
            return None
        path_els = list(getattr(expr, "PathEls", None) or [])
        if len(path_els) < 2:
            return None
        base = cls._find_renamed_base_view(view, getattr(path_els[0], "Ref", None))
        if base is None or not isinstance(base.BaseView, MetaInstance):
            return None
        current: MetaInstance = base.BaseView
        last_index = len(path_els) - 1
        for i, path_el in enumerate(path_els[1:], start=1):
            ref = getattr(path_el, "Ref", None)
            if ref is None:
                return None
            is_last = i == last_index
            found_attr = cls._find_class_attribute(current, ref)
            if found_attr is not None:
                return found_attr.Type if is_last and isinstance(found_attr.Type, MetaInstance) else None
            if is_last:
                return None  # a role alone (no trailing attribute) has no scalar Type
            role = cls._find_association_role(current, ref)
            target = cls._role_target_class(role) if role is not None else None
            if target is None:
                return None
            current = target
        return None

    @staticmethod
    def _find_class_attribute(cls_or_assoc: MetaInstance, name: str) -> MetaInstance | None:
        """Own attribute by name, then inherited via the `Super` chain (`EXTENDS`/`(EXTENDED)`).

        Real corpus evidence found (the `_fix_class_extended_super` gap
        fix, `ISOS_V2.ili`'s reopened `Ortsbild` class): own-only was
        previously a deliberate, evidence-based stance, superseded once a
        VIEW attribute needed to resolve `Type` from a BASE topic's
        attribute. Own wins over inherited on a name collision.
        """
        current: MetaInstance | None = cls_or_assoc
        seen: set[int] = set()
        while isinstance(current, MetaInstance) and id(current) not in seen:
            seen.add(id(current))
            for attr in getattr(current, "ClassAttribute", None) or []:
                if attr.Name == name:
                    return attr
            current = getattr(current, "Super", None)
        return None

    @staticmethod
    def _find_association_role(cls_or_assoc: MetaInstance, name: str) -> MetaInstance | None:
        for role in getattr(cls_or_assoc, "Role", None) or []:
            if role.Name == name:
                return role
        return None

    @staticmethod
    def _role_target_class(role: MetaInstance) -> MetaInstance | None:
        """Return a `Role`'s target `Class` via `BaseClass` (same association `reference_target_class` uses for a plain
        REFERENCE TO).

        Duplicated here rather than imported from `xtf.schema` - that
        module imports FROM `interlis.builder`, not the other way around.
        """
        base = getattr(role, "BaseClass", None)
        if isinstance(base, list):
            base = base[0] if base else None
        return base if isinstance(base, MetaInstance) else None
