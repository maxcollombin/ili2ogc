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

        `formationDef()` is a pure dispatcher (no own metamodel instance,
        spec/grammar/mapping/09_views_graphics.yml) - by the time this runs,
        the natural unclaimed-children sweep in `_build_instance` has
        already visited it once and, through it, built and attached every
        `RenamedBaseView` reachable from `projection()`/`join()`/etc. (each
        has its own `parent: {association: BaseViewDef, role:
        RenamedBaseView}` binding, applied against `instance` since it's
        still on top of `_parent_stack`). There is therefore nothing left
        to VISIT here - reads the raw `ViewDefContext` directly instead
        (`ctx.formationDef()`, then which of ITS OWN 5 sub-rule accessors
        matched) to determine the enum value, exactly once, with no side
        effects of its own. A view using `EXTENDS viewRef` instead of a
        `formationDef` (grammatically mutually exclusive, see viewDef())
        has no FormationKind of its own to set here - left unset,
        inherited from the extended view via the separate
        `extends_viewRef`/Inheritance binding.
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

        Same class of gap as `_stash_inspection_path` right below: the
        spec's `aggregation.FormationParameter` binding (`uniqueEl`,
        `feeds_into: PathOrInspFactor`) IS declared, but the generic
        `Container` resolution machinery only materializes it when a
        parent instance's own attribute_bindings drive the merge - called
        this way, directly from `_set_view_formation_kind` (no such parent
        instance in progress), `self.visit(...)` on the underlying
        `objectOrAttributePath` returns the raw bag dict
        (`{"PathEls": [...]}`, confirmed empirically - see
        `_merge_bag_into_instance`'s own docstring on why a `Container`
        rule with `feeds_into:` must stay a bag, never auto-unwrapped) -
        never a `PathOrInspFactor` instance on its own. Materialized here
        by hand: a fresh `PathOrInspFactor` + `_merge_bag_into_instance`,
        the SAME merge step a normal attribute-binding chain would apply
        automatically. `ALL` (no `uniqueEl` in this VIEW) leaves
        `_aggregation_key` unset - `convert/sql.py`'s `_build_aggregation_view`
        treats that as the `ALL` collapse-to-one-row-or-DISTINCT reading.
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

        `join()`'s own grammar (`RenamedViewableRef (',' RenamedViewableRef
        ['(' 'OR' 'NULL' ')'])*`, eCH-0031 V2.1.0 SS3.15) attaches an
        optional `(OR NULL)` to the IMMEDIATELY PRECEDING
        `renamedViewableRef` positionally among `join_ctx`'s own children -
        never to the 1st base (only "further" bases of a JOIN can be
        outer-joined against the first, per the manual's own text).
        `renamedViewableRef()` has no accessor of its own for this sibling
        token (it lives on `join()`'s context, one level up), so - like
        `_set_view_formation_kind` for `FormationKind` above - this reads
        `join_ctx.children` directly rather than a binding on
        `renamedViewableRef` itself (spec/grammar/mapping/09_views_graphics.yml,
        `join.attribute_bindings._resolution`/`renamedViewableRef.attribute_bindings.OrNull`
        both already documented this as fed by the caller, never
        implemented until this fix). Positional pairing with
        `view.RenamedBaseView` (i-th `renamedViewableRef` -> i-th base)
        relies on the same depth-first, source-order build guarantee
        already used by `_apply_pending_view_all_of`.

        Previously never implemented (no real corpus `(OR NULL)`
        occurrence existed to surface the gap) - `RenamedBaseView.OrNull`
        stayed `None`/falsy for every JOIN, silently breaking
        `convert/jsonfg.py`'s outer-join evaluation
        (`_join_combinations`) until a synthetic fixture caught it.
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

        `viewAttributes()` (spec/grammar/mapping/09_views_graphics.yml,
        `all_of_redefinition`, `status: not_applicable`) documents this as
        "a pure syntax shortcut on the ModelBuilder side ... iterate the
        attributes of the resolved BaseView Class and create one ClassAttr
        per attribute, empty Derivates" - `viewAttributes()`'s own
        `attribute_bindings` never produce a `ClassAttr` for this
        alternative (`ALL_OF` has no accessor of its own to bind against;
        it's a bare-token alternative), so without this, a View built this
        way has NO usable attribute list of its own for a future
        View->JSON Schema stage, even though it structurally builds fine.

        Only records here (`view`, `ctx`) rather than expanding immediately:
        the matched `RenamedBaseView.BaseView` can still be an unresolved
        `ForwardRef` at this point (e.g. a base declared later in the file,
        or in another TOPIC/model reached via `DEPENDS ON`/`IMPORTS`) -
        `forward_refs.resolve_all()` only runs once, at the very end of
        `build()`. The real expansion happens in
        `_apply_pending_view_all_of`, called right after that.
        """
        va = ca.call(ctx, "viewAttributes")
        if va is None or not ca.has_accessor(va, "ALL") or ca.call(va, "ALL") is None:
            return
        self._pending_view_all_of.append((view, ctx))

    def _apply_pending_view_all_of(self) -> None:
        """Expand every View recorded by `_expand_view_all_of`, once refs are resolved.

        Only OWN attributes of the matched base
        (`base.BaseView.ClassAttribute` - inherited attributes via EXTENDS
        not walked here: no real corpus evidence yet that a View's
        "ALL OF" base itself has an EXTENDS chain, and duplicating
        `xtf.schema.attributes_of`'s inheritance walk here would cross a
        layering boundary - `xtf/schema.py` imports FROM
        `interlis.builder`, not the other way around). A base that's
        still unresolved after `resolve_all()` (e.g.
        `UnresolvedNamedReference`, a genuinely absent cross-file model) is
        skipped, same "no crash on a known limit" stance as
        `xtf.schema.attributes_of`.

        `viewAttributes()`'s grammar (`vendor/interlis-antlr4/InterlisParser.g4`,
        matching the official EBNF, Reference Manual eCH-0031 V2.1.0 §3.15
        "ViewAttributes = [ATTRIBUTE] {'ALL' 'OF' Base-Name ';' |
        AttributeDef | Attribute-Name Properties<...> ':=' Expression
        ';'}.") loops (`(ALL OF Name SEMI | attributeDef | Name
        (Properties)? ASSIGN expression SEMI)*`), so ANY number of
        "ALL OF" clauses (freely interleaved with `attributeDef`/
        `Name := expression` redefinitions, in any order, exactly per the
        EBNF) land in the SAME `ViewAttributesContext` - a real, legal
        pattern with MULTIPLE consecutive "ALL OF Name;" statements (one
        per base of a multi-base `JOIN OF`/`UNION OF`, e.g.
        `ili_corpus/ERKAS_Strassen_V2_0.ili`'s `VIEW vVA`/`vER`,
        `ALL()`/`Name()` accessors both multi there). Walks `va.children`
        positionally (same technique as `_register_unqualified_imports`)
        to pair each `ALL` terminal with the `Name` terminal immediately
        following its `OF` - the ONLY reliable way to recover "which Name
        belongs to which ALL OF", since `Name` is ALSO used by the
        (unrelated) `Name := expression` alternative in the same repeated
        group. `attributeDef`-form attributes (bare `Name: Type;` inside
        ATTRIBUTE) are unaffected either way - already handled generically
        by the existing engine, independently of how many times it repeats.
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

        An `ATTRIBUTE ALL OF <Base>;` expansion (just above) and a bare
        `Name := expression` redefinition (`_build_view_bare_attributes`)
        attach to `ClassAttribute` at DIFFERENT TIMES during `build()`
        (bare ones immediately during the main tree walk; `ALL OF` only
        after `forward_refs.resolve_all()`, since its base can still be a
        `ForwardRef` until then) - so whichever forms are mixed in one
        `ATTRIBUTE` block, `ClassAttribute` used to end up with every
        bare-assign attribute BEFORE every `ALL OF` expansion, regardless
        of which was actually declared first in the source.

        Real corpus evidence this matters (RULE #7): `ALL OF <Base>;
        <Name> := <OtherBase> -> <Attr>;` is exactly the shape a `JOIN
        OF` VIEW needs to project one base wholesale and rename
        attributes from another (e.g. `Waldabstandslinien_V1_2`'s
        `Waldabstand_Linie`/`Typ`, item 13/15) - the wrong order is
        invisible to `.ili -> JSON Schema`/`.xtf -> JSON-FG`/`convert-sql`
        (none of the three care about `ClassAttribute` order), but a real
        `.xtf` writer (`convert/xtf_writer.py`) DOES:
        refman eCH-0031 V2.1.0 SS4.3.7's "Zwiebelprinzip" - a compiled
        schema's XSD `xsd:sequence` rejects an out-of-order instance -
        confirmed empirically (`ili2c -oXSD` + `xmllint --schema` on a
        real `Waldabstandslinien_V1_2_d` model) before this fix existed.

        Reorders by the position of each attribute's OWN declaring token
        in `va.children` (the `ALL` terminal for an `ALL OF` group -
        every attribute it expanded to moves together, keeping their OWN
        relative order - or the `Name` terminal for a bare redefinition),
        matched back to the already-built `ClassAttribute` entries by
        their `_all_of_identity` marker (`ALL OF` copies) or by name
        (bare redefinitions, unique names within one VIEW). An
        `attributeDef`-form attribute (bare `Name: Type;`, built
        generically elsewhere, not by either method above) has no
        recorded position here - no real corpus evidence yet of it mixed
        with `ALL OF` in the same VIEW (`_bare_view_attribute_assignments`'s
        own docstring) - so it keeps its current position, anchored via
        its own current list index (never worse than before this fix for
        that combination).
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
        (`convert/sql.py`'s `CREATE VIEW` `SELECT`) needs the same
        `Derivates` a `<attr> := <base> -> <attr>` redefinition would
        carry. Shape and `Kind="ReferenceAttr"` throughout match a real
        `Name := expression` path (see `_build_local_uniqueness_def` for
        the same permissive `Kind` convention). `Type` is still set
        directly from the base attribute, so this path is never walked for
        type resolution - only for value projection.
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

        Positional walk (same technique as `_register_unqualified_imports`):
        an `ALL` terminal is always immediately followed by `OF` then the
        base `Name` (`ALL OF Name SEMI`, see `_apply_pending_view_all_of`'s
        docstring) - `Name` alone isn't enough to disambiguate, since the
        SAME accessor is also used by the unrelated `Name ':=' expression`
        alternative in the same repeated group.
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

        `name` is the base's rename alias if one was given (`Name TILDE
        viewableRef`), or - the majority real-world case, e.g. `PROJECTION
        OF Test.Base.B;` + `ALL OF B;` - the base Class's own short name
        when no alias was used (`renamedViewableRef.Name` then stays
        unset).
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

        `viewAttributes()`'s 3rd alternative (spec/grammar/mapping/09_views_graphics.yml,
        `bare_redefinition_list`/`modifier_reassignment`) - real corpus proof:
        `tests/fixtures/fgdm4gs/` (5 real VIEW models, none use `ALL OF`).
        Same positional-walk technique as `_all_of_base_names` (`va.children`
        in source order): a top-level `Name` terminal belongs to THIS
        alternative unless it's the base name of an "ALL OF Name" triple
        (excluded via the same index arithmetic as `_all_of_base_names`) -
        `attributeDef`'s own `Name` is nested inside its own
        `AttributeDefContext` subtree, never a direct child of `va`, so no
        3-way ambiguity exists at this flat level. Scans forward from each
        such `Name` to its `ASSIGN`, collecting any modifier token
        (`ABSTRACT`/`EXTENDED`/`FINAL`/`TRANSIENT`) met along the way
        (inside the optional `LPAR ... RPAR`); a `SEMI` met before `ASSIGN`
        means this `Name` wasn't actually alt3 (defensive - never observed
        in real corpus, the grammar itself guarantees this won't happen).
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

        Reference Manual 2006-04-13 SS2.15: "it is sufficient to indicate
        the attribute name and the assignation to the basic attribute. Such
        definitions are always final" - `Final=True` unconditionally,
        regardless of the optional modifier bracket. Of the 4 possible
        modifier tokens (`ABSTRACT`/`EXTENDED`/`FINAL`/`TRANSIENT`), only
        `TRANSIENT` maps onto a confirmed `AttrOrParam` own attribute
        (`Transient`, ilismeta16-classes.yml); `ABSTRACT` has no evidence
        its refman `Class.Abstract`-like semantics were meant to apply to a
        single computed view attribute, and `EXTENDED` has no matching
        metamodel attribute at all anywhere in `ilismeta16-*.yml` - neither
        is guessed at (RULE #5), and no real corpus example uses this
        optional bracket at all (all 5 `tests/fixtures/fgdm4gs/` models use
        the bare form).

        `Type` is NOT set here - `RenamedBaseView.BaseView` (needed to walk
        the assigned expression's path) can still be an unresolved
        `ForwardRef` at this point, same timing issue as `_expand_view_all_of`.
        Recorded in `_pending_view_bare_attrs` for `_apply_pending_view_bare_attr_types`,
        called after `forward_refs.resolve_all()`.
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
        this only answers "what's this computed attribute's declared
        type", for `.ili -> JSON Schema`; evaluating the expression against
        real data (`.xtf -> JSON-FG`) is a separate concern, handled by
        `convert/jsonfg.py`'s own WHERE-clause evaluator instead. First `PathEl` selects
        the `RenamedBaseView` (same lookup `_find_renamed_base_view`
        already uses for `ALL OF`); each subsequent `PathEl` is tried
        first as a plain `ClassAttribute` by name (the JOIN OF case, e.g.
        `Axis -> AxisType` in `tests/fixtures/fgdm4gs/Axis_V1_1_d.ili`) and,
        if that fails, as an association `Role` by name whose `BaseClass`
        becomes the next hop's class (the PROJECTION OF an ASSOCIATION
        case, e.g. `TypPZ_Planungszone -> Planungszone -> Geometrie` in
        `tests/fixtures/fgdm4gs/Planungszonen_V2_d_B.ili` - `Planungszone`
        is a role of the `TypPZ_Planungszone` association, not an
        attribute). Only the LAST `PathEl` may resolve `Type` (a role alone
        has none); `_find_class_attribute` walks own-then-`Super` (real
        corpus case, `ISOS_V2.ili`'s `name := Ortsbild -> name`, `name`
        only declared on the base topic's `Ortsbild` before `(EXTENDED)`
        reopens it - see that method's own docstring, supersedes the
        former "own attributes only, no EXTENDS walk" stance). An
        expression that isn't a plain path (no real corpus example), or a
        path that fails to resolve at any hop, leaves `Type` unset - same
        graceful-degradation stance as everywhere else in this builder
        (RULE #5), not a crash.
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

        Real corpus evidence found - the `_fix_class_extended_super`
        gap fix (`ISOS_V2.ili`'s `TOPIC ISOS EXTENDS ISOS_V2.ISOSBase =
        CLASS Ortsbild (EXTENDED) = ...`) makes `Super` resolve for a
        reopened class, but a VIEW attribute assigned from one of the
        BASE topic's attributes (`name := Ortsbild -> name`, `name` only
        declared on `ISOSBase.Ortsbild`) still failed to resolve `Type`
        without this walk - own-only was previously a deliberate,
        evidence-based stance (see this method's former docstring/
        `_resolve_view_attribute_type`, "no real corpus evidence yet" -
        all 5 `tests/fixtures/fgdm4gs/` models only reference OWN
        attributes), now superseded by this real corpus case. Own wins
        over inherited on a name collision (checked before ascending to
        `Super`) - same precedence `xtf.schema.attributes_of` documents
        for the general case; a local walk here (not a call to that
        function) avoids a builder -> xtf import for a two-line loop.
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
        module imports FROM `interlis.builder`, not the other way around
        (see `_apply_pending_view_all_of`'s note on the same layering
        constraint).
        """
        base = getattr(role, "BaseClass", None)
        if isinstance(base, list):
            base = base[0] if base else None
        return base if isinstance(base, MetaInstance) else None
