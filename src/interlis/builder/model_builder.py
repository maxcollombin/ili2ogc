"""Generic ModelBuilder engine.

InterlisModelBuilder(InterlisParserVisitor) has NO rule-specific visitXxx
method. The generic visit(ctx) derives the rule name from
type(ctx).__name__, loads the matching spec/grammar/mapping/*.yml entry,
and applies one of 4 execution strategies depending on `kind`.
"""
from pathlib import Path
from typing import Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.antlr.InterlisParser import InterlisParser
from interlis.antlr.InterlisParserVisitor import InterlisParserVisitor
from interlis.builder import context_access as ca
from interlis.builder.attach import AttachmentResolver
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef, ForwardRefResolver, SymbolTable
from interlis.builder.repository import ModelRepository
from interlis.builder.source_resolver import resolve_source
from interlis.metamodel.instance import MetaInstance
from interlis.metamodel.registry import MetamodelRegistry
from interlis.metamodel.uml_schema import MetamodelSchema
from interlis.spec.models import SpecEntry
from interlis.spec.spec_index import load_spec

class InterlisModelBuilder(InterlisParserVisitor):
    def __init__(self, mappings_dir: Path, spec_dir: Path, *, repository: ModelRepository | None = None):
        schema = MetamodelSchema.load(mappings_dir)
        registry = MetamodelRegistry.build(schema)
        spec: dict[str, SpecEntry] = load_spec(spec_dir)
        attachment = AttachmentResolver(schema.uml)
        self._init_shared(schema, registry, spec, attachment, repository)

    @classmethod
    def _from_shared(cls, schema, registry, spec, attachment, repository) -> "InterlisModelBuilder":
        """Build a sub-builder for an imported file (see ModelRepository).

        Reuses the session's shared components instead of reloading them
        from mappings_dir/spec_dir - critical for the dynamic Pydantic
        classes (MetamodelRegistry) to be the same Python objects across
        files, not a distinct class per file despite the same
        qualified_name.
        """
        self = cls.__new__(cls)
        self._init_shared(schema, registry, spec, attachment, repository)
        return self

    def _init_shared(self, schema, registry, spec, attachment, repository) -> None:
        self.schema = schema
        self.registry = registry
        self.spec = spec
        self.attachment = attachment
        # A "bare" ModelRepository (no search directory) always backs the
        # predefined INTERLIS model (see repository.py, _BUILTIN_SOURCES) -
        # not really cross-file resolution (no disk consulted beyond the
        # embedded text), so it stays active even without an explicit
        # --repo/repository=... REAL cross-file resolution (search
        # directories supplied by the caller) stays opt-in as before.
        self.repository = repository if repository is not None else ModelRepository([])
        self.symbol_table = SymbolTable()
        self.forward_refs = ForwardRefResolver(self.symbol_table)
        self.parser_symbolic_names = InterlisParser.symbolicNames
        self._construction_stack: list[dict] = []
        self._parent_stack: list[MetaInstance] = []
        # eCH-0117 meta-attribute capture (see build()/
        # _attach_pending_meta_attributes) - empty unless build() is
        # called with meta_attributes=... (root file only; an imported
        # file's own comments aren't captured yet, see build()'s docstring).
        self._pending_meta_attributes: list[tuple[int, str, str]] = []
        self._meta_attribute_index = 0
        self.repository.bind_builder_factory(self._make_sub_builder)

    def _make_sub_builder(self) -> "InterlisModelBuilder":
        return InterlisModelBuilder._from_shared(self.schema, self.registry, self.spec, self.attachment, self.repository)

    # ------------------------------------------------------------------
    # Point d'entree public
    # ------------------------------------------------------------------
    def build(self, tree: ParserRuleContext, *, meta_attributes: list[tuple[int, str, str]] | None = None) -> Any:
        """Build `tree`, optionally capturing eCH-0117 `!!@Name=Value` comments.

        `meta_attributes` (typically `runtime.parse.meta_attribute_comments(text)`
        for this SAME source text) is attached to the built instances via
        `_attach_pending_meta_attributes`, per eCH-0117's "first following
        language construct" rule - a `MetaAttribute` instance per pair, via
        the real `MetaAttributes` association (IlisMeta16.ModelData), so it
        shows up as `instance.MetaAttribute` (a list) exactly like any
        other multi-valued association. Omitted (the default): no
        meta-attribute capture, zero behavior change from before this was
        added. Only the ROOT tree passed here is covered - an imported
        model's own comments are not (each is built by its own sub-builder,
        via ModelRepository, without this argument).
        """
        self._pending_meta_attributes = sorted(meta_attributes or [], key=lambda triple: triple[0])
        self._meta_attribute_index = 0
        result = self.visit(tree)
        self.forward_refs.resolve_all(repository=self.repository)
        return result

    @staticmethod
    def _ctx_line(ctx: Any) -> int | None:
        """Return the source line of a ParserRuleContext OR a bare Token."""
        start = getattr(ctx, "start", None)
        return getattr(start if start is not None else ctx, "line", None)

    def _attach_pending_meta_attributes(self, instance: MetaInstance, ctx: Any) -> None:
        """Attach every pending meta-attribute comment up to `ctx`'s own line.

        eCH-0117 SS3: "le meta-attribut se rapporte a la premiere
        construction de langue suivante" - since built instances are
        visited depth-first in source order (matching ANTLR's own
        traversal), and `_pending_meta_attributes` is consumed by a single
        monotonic index (never re-scanned/reset), the first instance whose
        own line is >= a pending comment's line IS that "first following
        construct". No-op if `ctx`'s line can't be determined (e.g. a
        synthetic instance with no real source position) or nothing is
        pending.
        """
        line = self._ctx_line(ctx)
        if line is None or self._meta_attribute_index >= len(self._pending_meta_attributes):
            return
        while self._meta_attribute_index < len(self._pending_meta_attributes):
            comment_line, name, value = self._pending_meta_attributes[self._meta_attribute_index]
            if comment_line > line:
                break
            meta = self.registry.new_instance("IlisMeta16.ModelData.MetaAttribute")
            meta.Name = name
            meta.Value = value
            self.attachment.attach(
                instance, "MetaAttribute", meta, association="MetaAttributes", role="MetaAttribute", rule="metaAttribute",
            )
            self._meta_attribute_index += 1

    # ------------------------------------------------------------------
    # Generic dispatch (replaces ANTLR's visitXxx pattern)
    # ------------------------------------------------------------------
    def visit(self, ctx: ParserRuleContext) -> Any:
        if ctx is None:
            return None
        rule_name = self._rule_name(ctx)
        entry = self.spec.get(rule_name)
        if entry is None:
            return super().visitChildren(ctx)

        if entry.kind in ("Instance", "ValueObject"):
            return self._build_instance(ctx, rule_name, entry)
        if entry.kind == "Conditional":
            return self._build_conditional(ctx, rule_name, entry)
        if entry.kind in ("Container", "Dispatcher"):
            return self._relay(ctx, rule_name, entry)
        if entry.kind == "Reference":
            return self._resolve_or_defer(ctx, rule_name, entry)
        raise BuildError(f"kind inconnu {entry.kind!r}", rule=rule_name, ctx=ctx)

    @staticmethod
    def _rule_name(ctx: ParserRuleContext) -> str:
        name = type(ctx).__name__
        if name.endswith("Context"):
            name = name[: -len("Context")]
        return name[0].lower() + name[1:] if name else name

    # ------------------------------------------------------------------
    # Strategie 1 : BuildInstance (kind Instance / ValueObject)
    # ------------------------------------------------------------------
    def _build_instance(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        if isinstance(entry.target, list):
            return self._build_multi_target(ctx, rule_name, entry)

        if rule_name == "modeldef":
            self._register_unqualified_imports(ctx)

        instance = self.registry.new_instance(entry.target)
        instance._source_ctx = ctx
        self._attach_pending_meta_attributes(instance, ctx)

        outer_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()

        if entry.discriminant:
            value = self._resolve_discriminant(ctx, rule_name, entry.discriminant, outer_ctx, consumed)
            if value is not None:
                setattr(instance, entry.discriminant.attribute, value)

        # Push BEFORE capturing the context used by THIS rule's
        # attribute_bindings: otherwise a reference to `context_key:
        # <rule_name>` (the instance currently being built itself, e.g.
        # modeldef.imports.ImportingP) would only find the ENCLOSING rule's
        # context, never this one - see the field: null mechanism.
        self._push_construction_context(rule_name, instance)
        construction_ctx = self._construction_stack[-1]
        self._parent_stack.append(instance)
        try:
            if entry.attribute_bindings:
                self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        finally:
            self._parent_stack.pop()
            self._pop_construction_context()
        self._attach_unclaimed_results(instance, sweep_results, rule_name)

        self._maybe_register_symbol(instance)

        if rule_name == "classDef":
            self._attach_class_oid(instance, ctx)
        elif rule_name == "unitDef":
            self._register_unit_alias(instance, ctx)

        if entry.parent and self._parent_stack:
            self.attachment.attach(
                self._parent_stack[-1], entry.parent.role, instance,
                association=entry.parent.association, role=entry.parent.role, rule=rule_name,
            )

        return instance

    def _register_unqualified_imports(self, ctx: ParserRuleContext) -> None:
        """Detect which `IMPORTS` names are prefixed with `UNQUALIFIED`.

        Reads the raw ModeldefContext (e.g. `IMPORTS UNQUALIFIED
        INTERLIS;`). Feeds symbol_table.unqualified_imports, used by
        ForwardRefResolver._resolve_one to let an unqualified reference
        resolve into an imported model. Not expressible via the generic
        for_each/attribute_bindings mechanism (see modeldef.imports,
        spec/grammar/mapping/02_packages.yml): the Import metamodel class
        has no attribute of its own for UNQUALIFIED, so this stays an
        internal detail of the resolution engine, never persisted on a
        MetaInstance.

        UNQUALIFIED always immediately precedes the name it modifies in the
        grammar ('IMPORTS UNQUALIFIED? (Name|INTERLIS) (COMMA UNQUALIFIED?
        (Name|INTERLIS))* SEMI'), so a simple positional walk over
        ctx.children is enough - no need for more complex correlation.
        """
        if not ca.has_accessor(ctx, "UNQUALIFIED"):
            return
        children = list(ctx.children or [])
        for node in ca.call_list(ctx, "UNQUALIFIED"):
            idx = children.index(node)
            if idx + 1 < len(children):
                nxt = children[idx + 1]
                if isinstance(nxt, TerminalNode):
                    self.symbol_table.unqualified_imports.add(nxt.getText())

    def _build_multi_target(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        """Build the two linked targets of a topicDef: SubModel + DataUnit.

        Not generalized to N targets - topicDef is the only one of the 121
        rules that needs this.
        """
        instances: dict[str, MetaInstance] = {}
        for target in entry.target:
            short = target.rsplit(".", 1)[-1]
            inst = self.registry.new_instance(target)
            inst._source_ctx = ctx
            instances[short] = inst

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()

        prefixed_bindings: dict[str, dict[str, Any]] = {short: {} for short in instances}
        for key, binding in (entry.attribute_bindings or {}).items():
            if "." in key:
                prefix, sub_key = key.split(".", 1)
                if prefix in prefixed_bindings:
                    prefixed_bindings[prefix][sub_key] = binding
                    continue
            # unprefixed key: applied to the first instance (rare, defensive)
            prefixed_bindings[next(iter(instances))][key] = binding

        submodel = instances.get("SubModel")
        if submodel is not None:
            # eCH-0117 SS5: a TopicDef's meta-attributes belong to the
            # SCHEMA side (SubModel), never DataUnit - the two twins share
            # the same source ctx, so this must be scoped explicitly
            # rather than left to the generic _build_instance hook (which
            # _build_multi_target bypasses entirely).
            self._attach_pending_meta_attributes(submodel, ctx)
            self._push_construction_context(rule_name, submodel)
            self._parent_stack.append(submodel)
            try:
                self._apply_bindings(submodel, ctx, rule_name, prefixed_bindings.get("SubModel", {}), construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()
            self._maybe_register_symbol(submodel)

        for short, inst in instances.items():
            if short == "SubModel":
                continue
            self._push_construction_context(rule_name, inst)
            self._parent_stack.append(inst)
            try:
                self._apply_bindings(inst, ctx, rule_name, prefixed_bindings.get(short, {}), construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()

        if submodel is not None:
            for short, inst in instances.items():
                if inst is not submodel:
                    submodel._twin = inst
                    inst._twin = submodel

        self._sweep_unclaimed_children(ctx, consumed)

        self._apply_topic_oid_clauses(ctx, instances)

        if entry.parent and self._parent_stack:
            for inst in instances.values():
                self.attachment.attach(
                    self._parent_stack[-1], entry.parent.role, inst,
                    association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                )

        return submodel if submodel is not None else next(iter(instances.values()))

    # ------------------------------------------------------------------
    # Cas special : multi_declaration (domainDef uniquement - voir _relay)
    # ------------------------------------------------------------------
    def _build_multi_declaration(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        """Split a domainDef() into its N independent declarations.

        domainDef() is the only one of the 121 rules that grammatically
        loops over N independent declarations sharing a single DOMAIN
        keyword (e.g. "DOMAIN Code = 0..255; MultRange = 0..2147483647;
        LengthRange EXTENDS MultRange = 1..2147483647;" - confirmed on the
        real generated code, InterlisParser.py, domainDef(): a "while _alt
        != 2" loop, each iteration Name (...)? EQ MANDATORY?
        (type_()|numeric()|enumeration()|STRING DOTDOT STRING|CLASS
        RESTRICTION(...)) SEMI). Each SEMI delimits a declaration - splits
        ctx.children into segments by position rather than accessor name
        (no existing generic mechanism, e.g. for_each, correlates several
        DIFFERENT accessors - Name/type_/numeric/enumeration - at the SAME
        position across N occurrences).

        `RESTRICTION LPAR classOrAssociationRef (SEMI classOrAssociationRef)*
        RPAR` contains its own INTERNAL SEMIs (separators between
        candidates, not declaration terminators, e.g.
        "CHCantonCode_Extended = CLASS RESTRICTION(sAbroadCode;
        sCHCantonCode);") - a naive split on every SEMI would cut a segment
        in the middle of the candidate list, silently truncating
        `_build_domain_class_restriction` to its 1st candidate only (the
        rest landing in a residual segment with no leading Name, rejected
        by `name_node is None: continue` below - never a crash, just silent
        data loss). This now tracks LPAR/RPAR depth: a SEMI only delimits a
        declaration outside any open parenthesis.
        """
        children = list(ctx.children or [])
        segments: list[list[Any]] = []
        current: list[Any] = []
        paren_depth = 0
        for child in children:
            current.append(child)
            if isinstance(child, TerminalNode):
                if child.symbol.type == InterlisParser.LPAR:
                    paren_depth += 1
                elif child.symbol.type == InterlisParser.RPAR:
                    paren_depth -= 1
                elif child.symbol.type == InterlisParser.SEMI and paren_depth == 0:
                    segments.append(current)
                    current = []
        if current:
            segments.append(current)

        results = []
        for segment in segments:
            name_node = next(
                (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.Name), None
            )
            if name_node is None:
                continue  # segment with no Name (e.g. leftover before the 1st token) - nothing to build
            mandatory = any(
                isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.MANDATORY for c in segment
            )
            content_node = next(
                (c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) in ("type", "numeric", "enumeration")),
                None,
            )
            if content_node is None:
                class_token = next(
                    (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.CLASS), None,
                )
                if class_token is None:
                    continue  # unreachable in practice: a bare "STRING DOTDOT STRING" domainDef alternative parses via type_'s own alternative instead (ANTLR resolves the ambiguity there - see type.text_range_alt, spec/grammar/mapping/06_types.yml) - kept as a defensive no-op, not a real gap
                instance = self._build_domain_class_restriction(segment, rule_name)
                if not isinstance(instance, MetaInstance):
                    continue
                # _build_domain_class_restriction builds its instance
                # directly (not via _build_instance/visit_wrapped), so it
                # never got a _source_ctx - fall back to this segment's own
                # Name token (a bare Token, not a ParserRuleContext -
                # _ctx_line handles both shapes).
                instance._source_ctx = instance._source_ctx or name_node.symbol
                self._attach_pending_meta_attributes(instance, instance._source_ctx)
                self._attach_domain_extends(instance, segment, rule_name)
                if getattr(instance, "Name", None) is None:
                    instance.Name = name_node.getText()
                    self._maybe_register_symbol(instance)
                if mandatory and getattr(instance, "Mandatory", None) is None:
                    instance.Mandatory = True
                if entry.parent and self._parent_stack:
                    self.attachment.attach(
                        self._parent_stack[-1], entry.parent.role, instance,
                        association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                    )
                results.append(instance)
                continue
            content_rule = self._rule_name(content_node)
            if content_rule == "type":
                # `_rule_name` derives "type" from `TypeContext` (grammar rule
                # `type_()`, the Python method renamed to avoid the `type`
                # builtin - `_rule_name` only knows the ANTLR CLASS name,
                # which is never renamed). Confirmed on BasketOID/MetaElemOID/
                # LanguageCode (models/IlisMeta16.ili, the file's 1st DOMAIN
                # block) - without this match, none of the 3 domains (all via
                # type_(), e.g. OID TEXT / TEXT*5) was ever built.
                instance = self.visit(content_node)
            else:
                target = "IlisMeta16.ModelData.NumType" if content_rule == "numeric" else "IlisMeta16.ModelData.EnumType"
                instance = self.visit_wrapped(content_node, target, rule_name)
            if not isinstance(instance, MetaInstance):
                continue
            self._attach_domain_extends(instance, segment, rule_name)
            if getattr(instance, "Name", None) is None:
                instance.Name = name_node.getText()
                self._maybe_register_symbol(instance)
            if mandatory and getattr(instance, "Mandatory", None) is None:
                instance.Mandatory = True
            if entry.parent and self._parent_stack:
                self.attachment.attach(
                    self._parent_stack[-1], entry.parent.role, instance,
                    association=entry.parent.association, role=entry.parent.role, rule=rule_name,
                )
            results.append(instance)

        if not results:
            return None
        return results[0] if len(results) == 1 else results

    def _attach_domain_extends(self, instance: MetaInstance, segment: list[Any], rule_name: str) -> None:
        """Attach `Super` for a `DOMAIN X EXTENDS Y = ...;` declaration.

        `domainDef()`'s optional `(EXTENDS domainRef)?` clause sits just
        before `EQ`, inside the same segment already split out by
        `_build_multi_declaration` (e.g. `LengthRange EXTENDS MultRange =
        1..2147483647;`, `models/IlisMeta16.ili`, or `DirectedLine EXTENDS
        Line = DIRECTED POLYLINE;`, CHBase). Previously never attached,
        regardless of the domain's content type (`NumType`/`EnumType`/
        `CoordType`/`LineType`/...) - content construction (Min/Max/Axis/
        Kind/etc.) only handled its own content alternative, never the
        optional `EXTENDS domainRef` prefix in front of it. Needed for
        `schema.line_coord_type` to walk the EXTENDS chain of a `LineType`
        with no `VERTEX` of its own. Same policy as
        `classDef`/`structureDef.Super`: `graceful=True` - an unresolved
        base domain (external model missing from `--repo`, or itself
        failing to build) degrades to `UnresolvedNamedReference`, never
        crashes the whole file.
        """
        extends_idx = next(
            (i for i, c in enumerate(segment) if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.EXTENDS),
            None,
        )
        if extends_idx is None:
            return
        domain_ref_node = next(
            (c for c in segment[extends_idx + 1:] if isinstance(c, ParserRuleContext) and self._rule_name(c) == "domainRef"),
            None,
        )
        if domain_ref_node is None:
            return
        value = self.visit(domain_ref_node)
        if not isinstance(value, ForwardRef):
            return
        value.graceful = True
        self.attachment.attach(instance, "Super", value, association="Inheritance", role="Super", rule=rule_name)
        self.forward_refs.register_pending(value, instance, "Super")

    # ------------------------------------------------------------------
    # OID clauses (classDef's own `OID AS .../NO OID`, topicDef's `BASKET
    # OID AS .../OID AS ...`) - see `_scan_oid_clauses` for why these need
    # a positional scan rather than a declarative attribute_binding.
    # ------------------------------------------------------------------
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
                    i > 0 and isinstance(children[i - 1], TerminalNode)
                    and children[i - 1].symbol.type == InterlisParser.BASKET
                )
                j = i + 2  # skip OID, AS
                ref_tokens: list[Any] = []
                while j < n and not (isinstance(children[j], TerminalNode) and children[j].symbol.type == InterlisParser.SEMI):
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
        domain (see docs/dev-notes/predefined-interlis-namespace.md,
        "ANYOID/UUIDOID deliberately absent") - built directly as a bare
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
            name=qualified, rule=rule_name, home_model=self._current_model_name(),
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

    def _build_domain_class_restriction(self, segment: list[Any], rule_name: str) -> MetaInstance:
        """Build `DOMAIN X = CLASS RESTRICTION(A; B; ...);`.

        domainDef()'s 5th alternative, inlined directly in its ANTLR body
        (`CLASS (RESTRICTION LPAR classOrAssociationRef (SEMI
        classOrAssociationRef)* RPAR)?`, not a call to a dedicated
        sub-rule) - previously never built (found on
        RoadTrafficCensus_V1_1.ili: "CHCantonCode_Extended = CLASS
        RESTRICTION(sAbroadCode; sCHCantonCode);"). Corresponds to the
        manual's "ClassType" alternative (eCH-0031 V2.1.0, "ClassType =
        'CLASS' ['RESTRICTION' '(' ViewableRef {';' ViewableRef} ')'] |
        ...") - target metaclass isn't "ClassType" (absent from
        ilismeta16-*.yml) but IlisMeta16.ModelData.ReferenceType, the same
        target as referenceAttr() (REFERENCE TO ...): Class EXTENDS Type,
        so usable directly as AttrOrParamType's Type role - but this
        grammar alternative has no EXTERNAL clause (unlike referenceAttr),
        so External=False unconditionally. BaseClass attaches via the same
        association as referenceAttr.BaseClass/roleDef.BaseClass
        (ClassRelatedType) - CRT {0..*} <-> BaseClass {0..*}
        (ilismeta16-associations.yml, already multi-valued on the
        BaseClass side): attaches ALL classOrAssociationRef in the segment
        (not just the first), needed to interpret the 3rd XTF encoding
        form - `CLASS RESTRICTION(A; B; C)` with several real candidates,
        e.g. `Owner = CLASS RESTRICTION (sCHOwnerCode; sCHCantonCode;
        sCHMunicipalityCode)` (RoadTrafficCensus_V1_1.ili, see
        xtf/schema.py: restriction_candidates). Each `attach()` on this
        multi-valued role APPENDS to the list
        (`AttachmentResolver._set_field`, upper='*'), never replaces - no
        change in shape for the single-candidate case (a plain REFERENCE
        TO, roleDef): `.BaseClass` was already a one-element list before
        this, this loop just appends further elements if any.
        """
        instance = self.registry.new_instance("IlisMeta16.ModelData.ReferenceType")
        instance.External = False
        refs = [c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) == "classOrAssociationRef"]
        for ref_ctx in refs:
            value = self.visit(ref_ctx)
            if value is None:
                continue
            self.attachment.attach(
                instance, "BaseClass", value, association="BaseClass", role="BaseClass", rule=rule_name,
            )
            if isinstance(value, ForwardRef):
                self.forward_refs.register_pending(value, instance, "BaseClass")
        return instance

    def _build_type_string_range(self, ctx: ParserRuleContext) -> MetaInstance | None:
        """Build type()'s 3rd alternative: a bare `STRING DOTDOT STRING` domain.

        E.g. `Angle_DMS_90 EXTENDS Angle_DMS = "-90:00:00.000" ..
        "90:00:00.000";` (CoordSys-20151124.ili). Maps to
        `IlisMeta16.ModelData.FormattedType` (`Min`/`Max`), by analogy with
        `formattedType()`'s own STRING DOTDOT STRING alternative - the same
        target class, same field semantics. Built by hand (not via
        `resolve_source`/`alt:`) because `ctx.getAltNumber()` is
        unconditionally 0 for unlabeled-alternative rules in the vendored
        grammar - a known, wider engine limitation affecting every `alt:
        <int>` binding, not fixed here. Full design rationale and the
        related grammar findings: docs/dev-notes/type-string-range-investigation.md.
        """
        strings = [c for c in (ctx.children or []) if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.STRING]
        if len(strings) != 2:
            return None
        instance = self.registry.new_instance("IlisMeta16.ModelData.FormattedType")
        instance.Min = strings[0].getText()
        instance.Max = strings[1].getText()
        return instance

    def _build_control_points_ref(self, ctx: ParserRuleContext, rule_name: str) -> ForwardRef | None:
        """Build a ForwardRef from `controlPoints()` (`VERTEX Name(DOT Name)*`).

        E.g. "VERTEX Coord2" - geometry XTF's 5th special case. Its
        declarative binding (`_resolution: {field: Name, multi: true}`)
        used to produce only a LIST of Name token texts, never attached
        anywhere (`LineType.CoordType` stayed `None` for every built
        instance, even `Line = POLYLINE ... VERTEX Coord2;` on
        `CHBase_Part1_GEOMETRY_V1.ili`) - the generic `_resolve_or_defer`
        (kind: Reference) doesn't fit either: its `ctx.getText()` would
        capture the VERTEX token along with the qualified name
        (`controlPoints()` carries its own keyword, unlike `domainRef()`
        which contains only the name). This rebuilds the qualified name by
        hand from the `Name` tokens only (ignoring VERTEX), same dot-join
        convention as `domainRef`. Returns a `ForwardRef` (never resolved
        here) - attached generically by `_apply_one_binding` via the
        `lineType.CoordType` binding (`association: LineCoord, role:
        CoordType`, see 06_types.yml), which already handles the
        `isinstance(value, ForwardRef)` case on its own (attach +
        `register_pending`, no extra code needed here).
        """
        names = [n.getText() for n in ca.call_list(ctx, "Name")]
        if not names:
            return None
        hints = self._expand_kind_hint("CoordType")
        return ForwardRef(
            name=".".join(names), resolves_to_hint=hints or None, rule=rule_name,
            home_model=self._current_model_name(), topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _build_enumeration_tree(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry) -> None:
        """Build the EnumNode tree of an `enumeration()` correctly.

        Each enumElement self-attaching via `parent: {association: TopNode,
        role: TopNode}` (the naive per-element approach) can only represent
        a linear chain, never a branching tree - for a nested definition
        like "KGS_Kategorie : MANDATORY (A (A, verstaerkter_Schutz), B);"
        it would produce TopNode=A/A.Sub=[B], losing A's real children
        entirely and conflating B - a sibling of A at the root level - with
        a child. See `enumElement` (spec/grammar/mapping/06_types.yml) for
        the binding this method replaces. `models/IlisMeta16.ili` documents
        the convention on EnumNode: "MetaElement.Name := 'TOP' for topnode"
        - a synthetic root node, never a real element.

        Distinguishes the level by the current PARENT's class
        (`_parent_stack[-1]`, already pushed by `visit_wrapped` for the TOP
        call, or by `_build_instance` of the enclosing enumElement for a
        NESTED call - Sub-Enumeration):
        - TOP call (parent = EnumType): creates the synthetic TOP node,
          attaches it as EnumType.TopNode, attaches EACH enumElement of the
          flat list to it as a direct child (role Node, association
          SubNode) - Order/Final apply to the EnumType itself.
        - NESTED call (parent = EnumNode, the enclosing enumElement):
          attaches this Sub-Enumeration's enumElements directly as children
          of THAT node (no extra synthetic TOP - the enclosing enumElement
          already serves as local root) - Final applies to that node itself
          (marks it non-extensible), Order is `not_applicable` at this
          level (already documented as such).
        """
        parent_context = self._parent_stack[-1] if self._parent_stack else None
        is_top_level = isinstance(parent_context, MetaInstance) and parent_context._qualified_class == "IlisMeta16.ModelData.EnumType"

        if is_top_level:
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                parent_context, "TopNode", top, association="TopNode", role="TopNode", rule=rule_name,
            )
            children_target = top
            final_target = parent_context
        else:
            children_target = parent_context
            final_target = parent_context

        if children_target is not None and ca.has_accessor(ctx, "enumElement"):
            for el_ctx in ca.call_list(ctx, "enumElement"):
                node = self.visit(el_ctx)
                if isinstance(node, MetaInstance):
                    self.attachment.attach(
                        children_target, "Node", node, association="SubNode", role="Node", rule=rule_name,
                    )

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()
        final_binding = entry.attribute_bindings.get("Final") if entry.attribute_bindings else None
        if final_target is not None and isinstance(final_binding, dict):
            final_value = self._resolve_binding_value(ctx, rule_name, "Final", final_binding, construction_ctx, consumed)
            if final_value:
                self.attachment.attach(final_target, "Final", final_value, rule=rule_name)
        order_binding = entry.attribute_bindings.get("Order") if entry.attribute_bindings else None
        if is_top_level and isinstance(order_binding, dict):
            order_value = self._resolve_binding_value(ctx, rule_name, "Order", order_binding, construction_ctx, consumed)
            if order_value:
                self.attachment.attach(parent_context, "Order", order_value, rule=rule_name)
        return None

    # ------------------------------------------------------------------
    # Strategie 2 : Conditional
    # ------------------------------------------------------------------
    def _build_conditional(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        for token_or_rule, branch in (entry.when_present or {}).items():
            if not ca.has_accessor(ctx, token_or_rule):
                continue
            node = ca.call(ctx, token_or_rule)
            if node is None:
                continue
            instance = self.registry.new_instance(branch.target)
            instance._source_ctx = ctx
            if branch.discriminant:
                setattr(instance, branch.discriminant.attribute, branch.discriminant.model_extra.get("value"))

            construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
            consumed: set[int] = set()
            self._push_construction_context(rule_name, instance)
            self._parent_stack.append(instance)
            try:
                if isinstance(node, ParserRuleContext):
                    # `token_or_rule` designates a REAL grammar rule (not a
                    # plain token), with its own structured content described
                    # by ITS OWN spec entry (e.g. oIDType.numeric -> numeric(),
                    # Min/Max/Circular/Clockwise/Unit) - visit it and merge
                    # its bag into `instance`, same mechanism as visit_wrapped
                    # (explicit wrap:). This content used to never be picked
                    # up at all.
                    self._merge_bag_into_instance(instance, self.visit(node), rule_name)
                if entry.attribute_bindings:
                    self._apply_bindings(instance, ctx, rule_name, entry.attribute_bindings, construction_ctx, consumed)
            finally:
                self._parent_stack.pop()
                self._pop_construction_context()
            return instance

        # No when_present branch matched: pure pass-through (e.g. term ->
        # term0 without EQ GT, predicate -> factor without NOT/DEFINED). Do
        # NOT reuse _relay/its generic bag mechanism here: a Conditional
        # rule's attribute_bindings (e.g. term2.SubExpressions =
        # [predicate(0), predicate(1)] "multi: true") describe the content of
        # the MATCHED branch (already handled above), not a generic
        # pass-through recipe - applying them anyway built a residual bag
        # {'SubExpressions': [predicate(0)], '_relation_child': None} instead
        # of purely relaying to the single real child (found on
        # models/IlisMeta16.ili, a term/term0/term1/term2 chain with no
        # operator). Plain sweep of unclaimed children instead, the same way
        # predicate -> factor already does successfully (the only remaining
        # real unconsumed child is the one to relay to).
        consumed: set[int] = set()
        sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        real_results = [v for _rule, v in sweep_results if v is not None]
        if real_results:
            return real_results[0] if len(real_results) == 1 else real_results
        return None

    # ------------------------------------------------------------------
    # Strategie 3 : Relay (Container / Dispatcher)
    # ------------------------------------------------------------------
    def _relay(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        # multi_declaration: a special case confirmed unique among the 121
        # rules (like topicDef/_build_multi_target, the other already-wired
        # special case) - domainDef() grammatically loops over N domain
        # declarations sharing a single DOMAIN keyword; no existing generic
        # mechanism (bag/for_each) correlates several different accessors
        # (Name/type_/numeric/enumeration) at the SAME position - see
        # _build_multi_declaration.
        if entry.multi_declaration:
            return self._build_multi_declaration(ctx, rule_name, entry)

        # enumeration(): 3rd special case among the 121 rules (same family
        # as multi_declaration/_build_multi_target) - see
        # _build_enumeration_tree for the fixed bug's detail.
        if rule_name == "enumeration":
            return self._build_enumeration_tree(ctx, rule_name, entry)

        # type(): 4th and last special case - its 3rd bare grammar
        # alternative "STRING DOTDOT STRING" (neither baseType() nor
        # lineType() present) has neither a sub-rule to dispatch to nor a
        # usable declarative binding (see _build_type_string_range for the
        # full detail, including a note on `alt:` being inert throughout
        # the whole mapping).
        if rule_name == "type" and not ca.is_present(ctx, "baseType") and not ca.is_present(ctx, "lineType"):
            instance = self._build_type_string_range(ctx)
            if instance is not None:
                return instance

        # controlPoints(): 5th special case (XTF geometry) - see
        # _build_control_points_ref for the full detail (joining Name tokens
        # while ignoring VERTEX, a ForwardRef then attached generically by
        # lineType.CoordType).
        if rule_name == "controlPoints":
            return self._build_control_points_ref(ctx, rule_name)

        # children: multi-visit dispatch (e.g. definitions -> classDef*,
        # topicDef*, ...) - all occurrences count, not a single
        # alternative.
        if entry.children:
            for child_rule in entry.children:
                if not ca.has_accessor(ctx, child_rule):
                    continue
                for node in ca.call_list(ctx, child_rule):
                    self.visit(node)
            return None

        # dispatches_to: only one grammar alternative is actually taken
        # among the named rules (e.g. classOrStructureDef). If NONE
        # matches, don't stop there: some rules (e.g. attrTypeDef,
        # dispatches_to=[attrType, lineType]) ALSO have their own direct
        # alternatives described in attribute_bindings (numeric/enumeration/
        # bare NUMERIC) - fall through to the bag processing below instead
        # of returning None prematurely.
        if entry.dispatches_to:
            for name in entry.dispatches_to:
                if ca.has_accessor(ctx, name):
                    node = ca.call(ctx, name)
                    if node is not None:
                        return self.visit(node) if isinstance(node, ParserRuleContext) else node.getText()

        # Neither children nor dispatches_to matched: compute this rule's
        # own attribute_bindings (if any - notes-only entries like
        # _dispatch produce nothing), make them available to the
        # construction context (the field: null mechanism, e.g.
        # interlis2def.iliVersion -> modeldef.iliVersion), THEN sweep the
        # unclaimed children (e.g. interlis2def -> modeldef, never in its
        # own bindings).
        bag: dict[str, Any] = {}
        consumed: set[int] = set()
        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        if entry.attribute_bindings:
            for key, binding in entry.attribute_bindings.items():
                if not isinstance(binding, dict):
                    continue
                if "target" in binding:
                    bag[key] = self._build_nested(ctx, rule_name, key, binding, construction_ctx, consumed)
                    continue
                if not isinstance(binding.get("source"), dict):
                    continue  # notes-only entry (e.g. _dispatch): nothing to compute
                bag[key] = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)

        real_values = {k: v for k, v in bag.items() if v is not None}

        # A bag key can itself ALREADY carry a concrete instance (e.g. a
        # `wrap:` binding on a bare numeric()/enumeration() alternative, see
        # domainDef._domain_content) rather than a plain scalar value - in
        # that case THIS instance is the rule's real content (the bag's
        # other keys, e.g. Name/Mandatory, only describe it): same
        # application rules as a result found by sweeping (see
        # _apply_sibling_bag_values below), but without waiting for the
        # sweep since the node was already consumed by the binding's own
        # computation.
        # Excludes "hollow" instances (e.g. numeric()._refsys_clause ->
        # NumsRefSys built unconditionally by _build_nested even when the
        # reference clause is NOT present in the source, all its fields
        # staying None) - without this filter, such a hollow instance
        # wrongly won this fast-path and was returned AS-IS instead of the
        # bag dict, preventing visit_wrapped (which already has its OWN
        # hollow-value filtering logic, see below) from ever reaching it -
        # silently losing Min/Max/Circular/Clockwise/Unit for EVERY numeric
        # domain/type, including on models/IlisMeta16.ili itself (e.g.
        # "Code = 0..255;").
        wrapped = [v for v in real_values.values() if isinstance(v, MetaInstance) and not self._is_hollow(v)]
        if len(wrapped) == 1:
            self._apply_sibling_bag_values(wrapped[0], real_values)
            return wrapped[0]

        if real_values:
            self._construction_stack.append({**construction_ctx, **real_values})
        try:
            sweep_results = self._sweep_unclaimed_children(ctx, consumed)
        finally:
            if real_values:
                self._construction_stack.pop()

        # A visited unclaimed child (e.g. interlis2def -> modeldef) usually
        # carries THE rule's real content - takes priority over the bag of
        # own values (which then often only feeds the construction context,
        # e.g. iliVersion).
        real_results = [v for _rule, v in sweep_results if v is not None]
        if real_results:
            result = real_results[0] if len(real_results) == 1 else real_results
            if isinstance(result, MetaInstance) and real_values:
                self._apply_sibling_bag_values(result, real_values)
            return result

        if len(bag) == 1:
            (only_value,) = bag.values()
            return only_value
        if bag:
            return bag
        return None

    # ------------------------------------------------------------------
    # Strategy 4: Reference (produces a ForwardRef, never resolved immediately)
    # ------------------------------------------------------------------
    def _resolve_or_defer(self, ctx: ParserRuleContext, rule_name: str, entry: SpecEntry):
        # A project-specific extension, already documented (see
        # 04_attributes.yml, restrictedStructureRef._inline_type,
        # status: not_applicable): restrictedStructureRef() has a type_()
        # grammar alternative ABSENT from the official manual (an anonymous
        # inline type, e.g. "TEXT*50") in addition to structureRef()/
        # ANYSTRUCTURE - this is NOT a name to resolve (a ForwardRef on
        # ctx.getText() always failed with a BuildError "unresolved and not
        # attributable to an import" for every real occurrence, e.g.
        # Holznutzungsbewilligung_V1_0.ili "TEXT*50"). Relay directly to
        # building the inline type instead of name resolution.
        has_structure_ref = ca.has_accessor(ctx, "structureRef") and bool(ca.call_list(ctx, "structureRef"))
        if ca.has_accessor(ctx, "type_") and not has_structure_ref:
            node = ca.call(ctx, "type_")
            if node is not None:
                return self.visit(node)
        name = ctx.getText()
        # `resolves_to` is already a short name (e.g. "Class"); `target`
        # (fallback) is a full qualified name (e.g.
        # "IlisMeta16.ModelData.Class") - SymbolTable always compares
        # against the instance's real metamodel class short name
        # (_qualified_class), so normalize here rather than propagating an
        # inconsistent format depending on the source.
        hint = entry.resolves_to or entry.target
        if isinstance(hint, list):
            hint = [h.rsplit(".", 1)[-1] for h in hint]
        elif hint:
            hint = [hint.rsplit(".", 1)[-1]]
        else:
            hint = []
        expanded: list[str] = []
        for h in hint:
            expanded.extend(self._expand_kind_hint(h))
        return ForwardRef(
            name=name, resolves_to_hint=expanded or None, rule=rule_name, home_model=self._current_model_name(),
            topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _current_topic_extends_hint(self, ctx: ParserRuleContext) -> str | None:
        """Return the raw text of ctx's enclosing TOPIC's 1st topicRef(), if any.

        Only if that TOPIC carries an EXTENDS (see
        ForwardRef.topic_extends_hint for the full rationale,
        forward_refs.py). Walks `ctx.parentCtx` up to the first
        `TopicDefContext` found (the ANTLR tree exactly mirrors the
        grammar's nesting - classDef/structureDef/etc. are always direct
        descendants of the topicDef containing them, so this walk always
        finds the right enclosing topic, no other correlation needed).
        Reads raw text rather than consulting already-built state
        (DataUnit.Super): avoids any dependency on the order pending
        ForwardRefs get resolved in (`definitions` - i.e. a topic's
        classDef/structureDef - is always processed BEFORE
        `extends_topicRef` in `_build_multi_target`, so DataUnit.Super
        wouldn't be resolved yet when this text is needed).
        """
        node = ctx.parentCtx
        while node is not None and type(node).__name__ != "TopicDefContext":
            node = node.parentCtx
        if node is None or node.EXTENDS() is None:
            return None
        refs = node.topicRef()
        if not refs:
            return None
        return refs[0].getText()

    def _current_model_name(self) -> str | None:
        """Return the name of the MODEL enclosing the current construction.

        Walks `_parent_stack` from the end. Used to disambiguate a short
        name that's still present in several models of a multi-MODEL file
        ("PointStructure" declared separately in
        BaseModel_SectoralPlans_LV03_V1_4 AND _LV95_V1_4, same file, SAME
        symbol_table - previously invisible since only one model per file
        was ever built).
        """
        for inst in reversed(self._parent_stack):
            if inst._qualified_class == "IlisMeta16.ModelData.Model":
                return getattr(inst, "Name", None)
        return None

    def _expand_kind_hint(self, short_name: str) -> list[str]:
        """Expand an abstract metamodel class hint into its concrete subclasses.

        A hint (`resolves_to`/`target`) can name an ABSTRACT metamodel
        class (e.g. "DomainType", isAbstract=true in the XMI - no instance
        is ever literally of that class, always a concrete subclass like
        EnumType/NumType/TextType) - the strict `_qualified_class == hint`
        comparison in `SymbolTable.resolve` therefore never matched for
        `domainRef` (hint="DomainType"), making its disambiguation
        completely inert (found on models.geo.admin.ch:
        "Bodenbedeckungsart: Bodenbedeckungsart;", the attribute and its
        EnumType domain share the same short name, never disambiguated). A
        hint that's already concrete (e.g. "Class") is returned unchanged.
        """
        qualified = next((qn for qn in self.schema.uml.qualified if qn.rsplit(".", 1)[-1] == short_name), None)
        if qualified is None:
            return [short_name]
        element = self.schema.uml.qualified[qualified]
        if not element.get("abstract"):
            return [short_name]
        descendants = [
            qn.rsplit(".", 1)[-1] for qn, el in self.schema.uml.qualified.items()
            if qualified in (el.get("all_superclasses") or [])
        ]
        return descendants or [short_name]

    # ------------------------------------------------------------------
    # Applying attribute_bindings
    # ------------------------------------------------------------------
    def _apply_bindings(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        bindings: dict[str, Any],
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        for key, binding in bindings.items():
            if not isinstance(binding, dict):
                continue
            if binding.get("status") in ("not_applicable", "unresolved") and "source" not in binding and "target" not in binding:
                continue

            try:
                self._apply_one_binding(instance, ctx, rule_name, key, binding, construction_ctx, consumed)
            except BuildError:
                source = binding.get("source") if isinstance(binding.get("source"), dict) else {}
                if source.get("optional") or binding.get("optional"):
                    # A binding marked optional whose resolution/attachment
                    # failed (e.g. modeldef.imports: Import construction by
                    # name, outside the current generic engine's scope).
                    # Doesn't block the rest of the construction.
                    continue
                raise

    def _apply_one_binding(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        if "for_each" in binding:
            self._apply_for_each_binding(instance, ctx, rule_name, key, binding, construction_ctx, consumed)
            return

        if "target" in binding:
            nested = self._build_nested(ctx, rule_name, key, binding, construction_ctx, consumed)
            if nested is None:
                return
            self.attachment.attach(
                instance, key, nested,
                association=binding.get("association"), role=binding.get("role"), rule=rule_name,
            )
            return

        value = self._resolve_binding_value(ctx, rule_name, key, binding, construction_ctx, consumed)
        if key.startswith("_"):
            # Internal/documentation-only key (project convention, e.g.
            # _dispatch: describes the grammar alternatives for the
            # mapping's readability, but each real alternative
            # self-constructs via its own rule/binding - never a real
            # attribute/role name. The computation above is still kept
            # (possible side effects, e.g. construction-context
            # propagation), only the literal attach is skipped - same
            # convention as _attach_unclaimed_results (needed for e.g.
            # `factor.dispatch`, which has no 'dispatch' attribute/role on
            # the metamodel to attach under).
            return
        if value is None:
            # Nothing to attach - either a binding with no source
            # (notes-only), or an absent value (optional), or a rule
            # visited only for its side effects (e.g. topicDef.definitions:
            # each classDef/etc. self-attaches via ITS OWN parent:, this key
            # has nothing to receive back - see its note).
            return
        if isinstance(value, list) and not value:
            # Empty multi-value list (nothing present in the .ili): nothing to attach.
            return
        if isinstance(value, ForwardRef):
            if binding.get("association") == "Inheritance" and binding.get("role") == "Super":
                # EXTENDS: never fatal if unresolved, see
                # ForwardRef.graceful (symmetric to domainRef/BaseClass).
                value.graceful = True
                hint = value.resolves_to_hint
                hints = hint if isinstance(hint, list) else ([hint] if hint else [])
                if "SubModel" in hints:
                    # TOPIC EXTENDS: only this case resolves to a SubModel
                    # rather than directly to the right ExtendableME
                    # subclass (classDef/structureDef/domainDef already
                    # resolve to Class/DomainType) - see
                    # ForwardRef.resolve_via_twin.
                    value.resolve_via_twin = True
            if binding.get("association") == "LineCoord" and binding.get("role") == "CoordType":
                # VERTEX (XTF geometry): a named CoordType can live in an
                # imported model not loaded via --repo (same category of
                # limitation as BaseClass/EXTENDS) - never fatal.
                value.graceful = True
            self.attachment.attach(
                instance, key, value,
                association=binding.get("association"), role=binding.get("role"), rule=rule_name,
            )
            self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, key, binding))
            return
        self.attachment.attach(
            instance, key, value,
            association=binding.get("association"), role=binding.get("role"), rule=rule_name,
        )

    def _resolved_field_name(self, instance: MetaInstance, key: str, binding: dict) -> str:
        role = binding.get("role")
        if role:
            return role
        element = self.schema.uml.qualified.get(instance._qualified_class, {})
        if self.schema.uml.attribute_exists(element, key):
            return key
        found = self.attachment._find_association_for_role(instance._qualified_class, key)
        return found[1] if found else key

    def _build_nested(
        self, ctx: ParserRuleContext, rule_name: str, key: str, binding: dict, construction_ctx: dict, consumed: set[int]
    ) -> MetaInstance | None:
        target = binding["target"]
        sub_bindings = {
            k: v for k, v in binding.items()
            if k not in ("target", "note", "association", "role") and isinstance(v, dict) and "source" in v
        }
        if not sub_bindings:
            # target: with no real source: sub-binding (e.g.
            # modeldef/topicDef.default_class_oid): the associated note
            # explicitly documents procedural logic not covered by the
            # generic engine (e.g. "apply afterwards to each Class in the
            # topic that doesn't define its own OID") - not a standard
            # nested construction, so no hollow instance is built to avoid
            # wrongly attaching it.
            return None
        instance = self.registry.new_instance(target)
        instance._source_ctx = ctx
        self._parent_stack.append(instance)
        try:
            self._apply_bindings(instance, ctx, rule_name, sub_bindings, construction_ctx, consumed)
        except BuildError:
            # A nested construction (e.g. attrTypeDef._collection ->
            # MultiValue) represents ONE grammar alternative among others,
            # not an always-present structure - if one of its required
            # sub-bindings (a discriminant marker, e.g. BAG|LIST) fails,
            # that's a sign this alternative simply wasn't taken here, not a
            # real error. Discards the whole nested construction rather than
            # letting it propagate (same logic as Conditional: inapplicable
            # branch -> nothing built).
            return None
        finally:
            self._parent_stack.pop()
        return instance

    def _apply_for_each_binding(
        self,
        instance: MetaInstance,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> None:
        """Build one `target` instance per element of a resolved list.

        For `for_each:` bindings, instead of a single instance - needed for
        rules where a grammatical loop (e.g. modeldef.imports: a repeated
        IMPORTS clause) must produce N distinct linked instances (e.g. N
        Import associations), not a single standard nested construction
        (see _build_nested, which assumes one instance per binding).

        Each sub-binding can read the loop's current element via
        `source: {field: null, context_key: '__item__'}` (the generic
        field: null mechanism, just under a synthetic key dedicated to this
        loop - same principle as the construction context propagated
        elsewhere, e.g. interlis2def.iliVersion).
        """
        items = self._resolve_binding_value(ctx, rule_name, key, {"source": binding["for_each"]}, construction_ctx, consumed)
        if items is None:
            return
        if not isinstance(items, list):
            items = [items]
        if not items:
            return

        target = binding["target"]
        sub_bindings = {
            k: v for k, v in binding.items()
            if k not in ("for_each", "target", "note", "association", "role") and isinstance(v, dict)
        }
        for item in items:
            item_ctx = {**construction_ctx, "__item__": item}
            built = self.registry.new_instance(target)
            built._source_ctx = ctx
            self._apply_bindings(built, ctx, rule_name, sub_bindings, item_ctx, consumed)
            if binding.get("association"):
                self.attachment.attach(
                    instance, key, built,
                    association=binding.get("association"), role=binding.get("role"), rule=rule_name,
                )
            else:
                # No collection role on the metamodel side (e.g. Import: a
                # pure ImportingP<->ImportedP association, no inverse
                # "Model.Imports" association declared in
                # ilismeta16-associations.yml) - still keeps the built
                # instances, under the binding's raw key, so they stay
                # reachable (otherwise lost to the garbage collector, no
                # other reference holds them).
                current = getattr(instance, key, None)
                if current is None:
                    current = []
                    setattr(instance, key, current)
                current.append(built)

    def _resolve_binding_value(
        self, ctx: ParserRuleContext, rule_name: str, key: str, binding: dict, construction_ctx: dict, consumed: set[int]
    ) -> Any:
        source = binding.get("source")
        if not isinstance(source, dict):
            return None
        # This guard used to mark "consumed" only if
        # `_alt_matches(ctx, source["alt"])` - a mechanism removed from the
        # whole mapping (see source_resolver.py: `ctx.getAltNumber()` always
        # returns 0 for this grammar, so `alt: <int>` was ALWAYS false and
        # this guard NEVER marked a node shared between alternatives as
        # consumed - harmless only because resolve_source itself also
        # always returned None for these same bindings). The 15 affected
        # bindings (e.g. predicate: alt1 = bare factor, alt3 = DEFINED LPAR
        # factor RPAR - same "factor" accessor) now rely on their
        # accessor's NATURAL presence (already exclusive to the grammar
        # alternative actually taken, verified case by case): consuming
        # unconditionally is therefore correct, this guard is no longer
        # needed.
        for name in self._accessor_names_in_source(source):
            if ca.has_accessor(ctx, name):
                for node in ca.call_list(ctx, name):
                    consumed.add(id(node))
        rule_map = binding.get("rule") if isinstance(binding.get("rule"), dict) else binding.get("mapping")
        wrap_map = binding.get("wrap") if isinstance(binding.get("wrap"), dict) else None
        return resolve_source(
            ctx, source, rule=rule_name, construction_context=construction_ctx, builder=self,
            rule_map=rule_map, binding_key=key, wrap_map=wrap_map,
        )

    def visit_wrapped(self, node: ParserRuleContext, target: str, rule_name: str) -> MetaInstance:
        """Visit a bag rule, wrapped into a typed metamodel instance.

        For a Container/ValueObject rule (e.g. numeric()/enumeration()),
        wraps it into a typed metamodel instance (e.g. NumType/EnumType) -
        used when an enclosing rule (e.g. attrTypeDef.Type) knows which
        class this bag actually represents.

        The instance is created and pushed onto the construction stack
        BEFORE the visit, not after: some children of the bag rule
        self-attach via their OWN `parent:` DURING the visit (e.g.
        enumElement -> TopNode/SubNode association). Wrapping afterward
        would attach them to the wrong parent (whatever is already on top
        of the stack at that point, e.g. the enclosing AttrOrParam) instead
        of this new instance.
        """
        instance = self.registry.new_instance(target)
        instance._source_ctx = node
        self._attach_pending_meta_attributes(instance, node)
        self._parent_stack.append(instance)
        try:
            bag = self.visit(node)
        finally:
            self._parent_stack.pop()

        self._merge_bag_into_instance(instance, bag, rule_name)
        return instance

    def _merge_bag_into_instance(self, instance: MetaInstance, bag: Any, rule_name: str) -> None:
        """Merge a bag dict onto `instance`.

        The bag is the result of `_relay` on a Container rule (e.g.
        numeric()/enumeration()) - attaches every non-empty/non-hollow
        field, resolves pending ForwardRefs. Shared by `visit_wrapped`
        (explicit wrap:) and `_build_conditional` (`when_present` branch
        whose token/rule IS itself a rule with its own structured content,
        e.g. oIDType.numeric -> numeric()) - previously, only
        `visit_wrapped` applied this hollow filter; `_build_conditional`
        never visited the matched branch's node at all, losing all of
        numeric()/textType()'s own content (Min/Max/Circular/Clockwise/Unit)
        for any numeric/text OID rule (e.g. `I32OID = OID
        0..2147483647;`, predefined INTERLIS namespace).
        """
        if not isinstance(bag, dict):
            return
        for field, value in bag.items():
            if field == "Elements":
                continue  # already attached via enumElement.parent: during the visit above
            if value is None or (isinstance(value, list) and not value) or self._is_hollow(value):
                # None/empty list/"hollow" (a nested instance whose fields
                # are all None/empty, e.g. numeric._refsys_clause when no
                # reference clause is present): nothing to attach.
                continue
            if isinstance(value, ForwardRef):
                self.attachment.attach(instance, field, value, rule=rule_name)
                self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, field, {}))
                continue
            self.attachment.attach(instance, field, value, rule=rule_name)

    @staticmethod
    def _is_hollow(value: Any) -> bool:
        if not isinstance(value, MetaInstance):
            return False
        fields = {**value.__dict__, **(value.model_extra or {})}
        return all(v is None or v == [] for k, v in fields.items() if not k.startswith("_"))
        return instance

    @staticmethod
    def _accessor_names_in_source(source: dict) -> list[str]:
        names: list[str] = []
        field = source.get("field")
        if field:
            names.extend(n.strip() for n in field.split("|"))
        anchor = source.get("anchor")
        if anchor:
            names.append(anchor)
        if "join" in source:
            for k, v in source.items():
                if v is None and k not in ("field", "optional"):
                    names.append(k)
        return names

    def _resolve_discriminant(self, ctx, rule_name, discriminant, construction_ctx, consumed) -> Any:
        extra = discriminant.model_extra or {}
        if "value" in extra and not isinstance(extra["value"], dict):
            return extra["value"]
        if isinstance(extra.get("value"), dict):
            nested = extra["value"]
            source = nested.get("source")
            if isinstance(source, dict):
                for name in self._accessor_names_in_source(source):
                    if ca.has_accessor(ctx, name):
                        for node in ca.call_list(ctx, name):
                            consumed.add(id(node))
                rule_map = nested.get("mapping") or nested.get("rule")
                return resolve_source(
                    ctx, source, rule=rule_name, construction_context=construction_ctx, builder=self, rule_map=rule_map,
                )
        if extra.get("dynamic") and isinstance(extra.get("rule"), dict):
            for token_or_rule, value in extra["rule"].items():
                if ca.has_accessor(ctx, token_or_rule) and ca.is_present(ctx, token_or_rule):
                    return value
        return None

    # ------------------------------------------------------------------
    # Contexte de construction propage (mecanisme field: null)
    # ------------------------------------------------------------------
    def _push_construction_context(self, rule_name: str, instance: MetaInstance) -> None:
        ctx_dict = dict(self._construction_stack[-1]) if self._construction_stack else {}
        ctx_dict[rule_name] = instance
        name = getattr(instance, "Name", None)
        if name is not None:
            ctx_dict[f"{rule_name}.Name"] = name
        self._construction_stack.append(ctx_dict)

    def _pop_construction_context(self) -> None:
        self._construction_stack.pop()

    # ------------------------------------------------------------------
    # Table de symboles
    # ------------------------------------------------------------------
    def _maybe_register_symbol(self, instance: MetaInstance) -> None:
        element = self.schema.uml.qualified.get(instance._qualified_class, {})
        if not self.schema.uml.attribute_exists(element, "Name"):
            return
        name = getattr(instance, "Name", None)
        if not name:
            return
        qualified = self._qualify_name(name)
        self.symbol_table.register(qualified, instance)

    def _register_unit_alias(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Register a UNIT's bracketed short-name alias under the SAME instance too.

        Grammar (unitDef, InterlisParser.py): `UNIT? Name (LSBR Name RSBR)?
        ...` - `ctx.Name(0)` is the primary Name (already registered by
        `_maybe_register_symbol` above), `ctx.Name(1)` the optional
        bracketed alias (e.g. `CubicMeterPerSecond [m3sec] = ...;`). The
        metamodel's `Unit` class has no dedicated ShortName attribute (only
        the inherited `Name`, `ilismeta16-classes.yml`), so without this the
        alias is never registered anywhere - confirmed on real corpus data
        (`PlanerischerGewaesserschutz_V1_1.ili`/`Hazard_Mapping_V1_3.ili`):
        a later `[m3sec]`/`[m2s]` unitRef elsewhere in the SAME file raised
        `BuildError: unresolved reference, not attributable to an import`,
        since only the primary Name was ever findable. Registers a SECOND
        symbol table entry pointing at the SAME instance - same alias
        mechanism already used by `SymbolTable.rekey_model_prefix`, applied
        here per-unit instead of per-model.
        """
        names = ctx.Name()
        if len(names) < 2:
            return
        alias = names[1].getText()
        self.symbol_table.register(self._qualify_name(alias), instance)

    def _qualify_name(self, name: str) -> str:
        parts = [getattr(inst, "Name", None) for inst in self._parent_stack if getattr(inst, "Name", None)]
        parts.append(name)
        return ".".join(parts)

    def _apply_sibling_bag_values(self, result: MetaInstance, real_values: dict) -> None:
        """Apply sibling bag values that `result` actually inherits/owns.

        A Container rule with its OWN attribute_bindings (e.g.
        domainDef.Name/Mandatory) whose real content is ANOTHER value from
        the same bag (e.g. domainDef._domain_content, wrap: on bare
        numeric()/enumeration()) or comes from an unclaimed child (e.g.
        enumerationType -> EnumType, via the type_() dispatcher) used to
        lose these values - pushed only into the construction context
        (field: null mechanism), never applied onto the returned instance
        itself, even when that instance genuinely INHERITS the field
        concerned (e.g. EnumType.Name via MetaElement - confirmed in
        ilismeta16-classes.yml, DomainType.attributes.inherited.Name).
        Concrete consequence found: a DOMAIN named via a bare enumeration
        (e.g. "DOMAIN CodeWeekDayType = (MON, TUE, ...);", no ENUM keyword)
        produced an EnumType with no Name -> never registered in the
        SymbolTable -> any later reference by name
        (domainRef/restrictedStructureRef) failed with a BuildError "not
        resolved and not attributable to an import". Only applies keys
        that are genuinely own/inherited on `result`'s metamodel class, and
        not already set.
        """
        element = self.schema.uml.qualified.get(result._qualified_class, {})
        registered_name = False
        for key, value in real_values.items():
            if value is result:
                continue
            if getattr(result, key, None) is not None:
                continue
            if not self.schema.uml.attribute_exists(element, key):
                continue
            setattr(result, key, value)
            if key == "Name":
                registered_name = True
        if registered_name:
            self._maybe_register_symbol(result)

    # ------------------------------------------------------------------
    # Sweep of children not claimed by attribute_bindings (e.g.
    # modeldef -> definitions, never mentioned in modeldef's bindings -
    # needed because the structural relationship goes through `parent:` on
    # the child rule, not through an explicit binding on the enclosing
    # rule).
    # ------------------------------------------------------------------
    def _sweep_unclaimed_children(self, ctx: ParserRuleContext, consumed: set[int]) -> list[tuple[str, Any]]:
        results = []
        for child in ctx.children or []:
            if isinstance(child, ParserRuleContext) and id(child) not in consumed:
                results.append((self._rule_name(child), self.visit(child)))
        return results

    def _attach_unclaimed_results(self, instance: MetaInstance, sweep_results: list[tuple[str, Any]], rule_name: str) -> None:
        """Try to attach unclaimed children that produced a concrete result.

        For a child not claimed by attribute_bindings whose rule has NO
        `parent:` of its own (so it hasn't already self-attached) but
        produces a concrete result: try to attach it via the association
        connecting the two known classes (e.g. attrTypeDef ->
        AttrOrParamType.Type, see
        AttachmentResolver.find_association_connecting). Best-effort:
        silent if no association connects the two classes (the result then
        stays only whatever side effect it already produced, e.g.
        registration in the symbol table).
        """
        for child_rule, value in sweep_results:
            if value is None:
                continue
            child_entry = self.spec.get(child_rule)
            if child_entry is not None and child_entry.parent:
                continue  # already self-attached via its own parent:
            if isinstance(value, dict):
                # The unclaimed rule is a bag (Container/Dispatcher with
                # several keys, e.g. attrTypeDef -> {Mandatory, Type,
                # _collection}): each non-empty key is attached
                # individually onto `instance` under its own name (same
                # mechanism as wrap_bag_as_instance, but onto the existing
                # ENCLOSING instance rather than a new one). If a key
                # doesn't attach onto `instance` (e.g. Mandatory, documented
                # as NOT concerning AttrOrParam but the concrete Type
                # produced alongside it in the same bag - attrTypeDef.
                # Mandatory), retry on a sibling MetaInstance value from the
                # same bag before giving up.
                siblings = [v for v in value.values() if isinstance(v, MetaInstance)]
                has_unresolved_sibling = any(isinstance(v, ForwardRef) for v in value.values())
                for key, sub_value in value.items():
                    if sub_value is None or (isinstance(sub_value, list) and not sub_value) or self._is_hollow(sub_value):
                        continue
                    if key.startswith("_"):
                        # Internal key (project convention, e.g. _collection,
                        # _refsys_clause): never a real attribute/role name,
                        # do NOT try attach(instance, "_collection", ...)
                        # (would always fail by construction) - look
                        # directly for the association connecting the two
                        # KNOWN classes (same mechanism as for an unclaimed
                        # MetaInstance at the top level). Best-effort,
                        # silent if no association fits.
                        if isinstance(sub_value, MetaInstance):
                            found = self.attachment.find_association_connecting(
                                instance._qualified_class, sub_value._qualified_class
                            )
                            if found is not None:
                                _assoc_name, role, upper = found
                                self.attachment._set_field(instance, role, sub_value, upper)
                        continue
                    attached_on = None
                    attached_field = key
                    try:
                        self.attachment.attach(instance, key, sub_value, rule=rule_name)
                        attached_on = instance
                        attached_field = self._resolved_field_name(instance, key, {})
                    except BuildError:
                        for sibling in siblings:
                            if sibling is sub_value:
                                continue
                            try:
                                self.attachment.attach(sibling, key, sub_value, rule=rule_name)
                                attached_on = sibling
                                attached_field = self._resolved_field_name(sibling, key, {})
                                break
                            except BuildError:
                                continue
                        if attached_on is None:
                            # `attach()` (above) requires a role literally
                            # named `key` (here "Type") on an association
                            # connecting the two classes - but roleDef() also
                            # calls attrTypeDef() (inline type role), and NO
                            # Role<->(Class or DomainType) association is
                            # named "Type" (the role is named differently,
                            # e.g. BaseClass). Generic fallback by KNOWN
                            # CLASS (same mechanism as the
                            # `isinstance(value, MetaInstance)`/`ForwardRef`
                            # block further below in this method, for a RAW
                            # unclaimed child) - applied here for a value
                            # nested in a bag, for a `sub_value` that is a
                            # MetaInstance OR a ForwardRef (target class hint
                            # via resolves_to_hint).
                            candidate_classes: list[str] = []
                            if isinstance(sub_value, MetaInstance):
                                candidate_classes = [sub_value._qualified_class]
                            elif isinstance(sub_value, ForwardRef):
                                hints = sub_value.resolves_to_hint if isinstance(sub_value.resolves_to_hint, list) else (
                                    [sub_value.resolves_to_hint] if sub_value.resolves_to_hint else []
                                )
                                candidate_classes = [
                                    qn for hint in hints for qn in self.schema.uml.qualified
                                    if qn.rsplit(".", 1)[-1] == hint
                                ]
                            for qualified_class in candidate_classes:
                                found = self.attachment.find_association_connecting(instance._qualified_class, qualified_class)
                                if found is not None:
                                    _assoc_name, role, upper = found
                                    self.attachment._set_field(instance, role, sub_value, upper)
                                    attached_on = instance
                                    attached_field = role
                                    break
                        if attached_on is None:
                            if not isinstance(sub_value, ForwardRef) and (has_unresolved_sibling or not siblings):
                                # Best-effort (see this method's docstring): a
                                # sibling bag key (e.g. attrTypeDef.Mandatory,
                                # meant for the Type built alongside it) has
                                # nowhere to attach to. Two distinct cases,
                                # both non-critical (never a `sub_value` that
                                # IS ITSELF the broken reference - see the
                                # `raise` below, unchanged for that case):
                                # 1. has_unresolved_sibling: this Type is
                                #    still an unresolved ForwardRef (e.g. a
                                #    reference to an existing named DOMAIN,
                                #    potentially SHARED across several uses of
                                #    the attribute - e.g. "Owner: MANDATORY
                                #    Owner;", domain and attribute sharing a
                                #    name). Applying Mandatory on the SHARED
                                #    instance once resolved would be
                                #    uncertain (which use would be right if
                                #    several differ?).
                                # 2. not siblings: NO sibling
                                #    MetaInstance/ForwardRef exists in this
                                #    bag AT ALL to even attempt an attach -
                                #    confirmed real on `HAli: MANDATORY
                                #    HALIGNMENT;`
                                #    (CHBase_Part6_GRAPHICANNOTATIONS_V1/V2.ili):
                                #    `alignmentType()` (grammar - "HALIGNMENT"/
                                #    "VALIGNMENT" are RESERVED tokens,
                                #    grammatically impossible to declare via
                                #    domainDef - confirmed, `DOMAIN HALIGNMENT
                                #    = ...` is a syntax rejection, not a
                                #    missing binding) deliberately builds NO
                                #    instance at all (target: null, same
                                #    category as booleanType/oIDType for
                                #    BOOLEAN/ANYOID/UUIDOID) - so its bag only
                                #    contains an opaque TEXT value
                                #    ("resolved_type"), never an instance for
                                #    Mandatory to land on. Before this fix,
                                #    `has_unresolved_sibling` stayed FALSE (no
                                #    ForwardRef, just an inert dict) and the
                                #    code fell into the `raise` -
                                #    `interlis build`/`validate` crashed
                                #    entirely as soon as an attribute typed
                                #    HALIGNMENT/VALIGNMENT MANDATORY, even
                                #    though this type otherwise stays
                                #    deliberately uninterpreted (same
                                #    documented limitation as BOOLEAN) -
                                #    silently dropping Mandatory (secondary
                                #    info) rather than a total crash for an
                                #    otherwise valid attribute.
                                continue
                            raise
                    if attached_on is not None and isinstance(sub_value, ForwardRef):
                        # A ForwardRef nested INSIDE the bag (e.g.
                        # attrTypeDef.Type when attrType() dispatches to
                        # domainRef(), a reference to an existing named
                        # domain) used to never be registered for deferred
                        # resolution - unlike the "bare" ForwardRef case (not
                        # nested in a dict, already handled further below in
                        # this method) - it stayed a literal ForwardRef,
                        # never replaced by the real instance or by
                        # UnresolvedNamedReference, silently, `resolve_all()`
                        # never seeing it.
                        self.forward_refs.register_pending(sub_value, attached_on, attached_field)
                continue
            if isinstance(value, list):
                continue
            if isinstance(value, ForwardRef):
                # A Reference-kind rule (e.g. domainRef/classRef) visited as
                # an unclaimed child (e.g. attrTypeDef -> attrType ->
                # domainRef, never captured by an explicit binding on
                # attributeDef - attributeDef.attribute_bindings has no Type
                # key) produces a ForwardRef, not yet a MetaInstance - the
                # `isinstance(value, MetaInstance)` check below silently
                # ignored it, losing the Type attribute for EVERY domain/
                # class reference used as an attribute type (e.g. "Kind:
                # MANDATORY Base.PersonKind;"), local or cross-file. Same
                # association-by-class mechanism as for a MetaInstance
                # below, but via the ForwardRef's target class hint
                # (resolves_to_hint, a short name - its full qualified name
                # is looked up in the schema) since the real class isn't
                # known yet before resolution.
                hints = value.resolves_to_hint if isinstance(value.resolves_to_hint, list) else (
                    [value.resolves_to_hint] if value.resolves_to_hint else []
                )
                for hint in hints:
                    qualified_hint = next(
                        (qn for qn in self.schema.uml.qualified if qn.rsplit(".", 1)[-1] == hint), None
                    )
                    if qualified_hint is None:
                        continue
                    found = self.attachment.find_association_connecting(instance._qualified_class, qualified_hint)
                    if found is not None:
                        _assoc_name, role, upper = found
                        self.attachment._set_field(instance, role, value, upper)
                        self.forward_refs.register_pending(value, instance, role)
                        break
                continue
            if not isinstance(value, MetaInstance):
                continue
            found = self.attachment.find_association_connecting(instance._qualified_class, value._qualified_class)
            if found is not None:
                _assoc_name, role, upper = found
                self.attachment._set_field(instance, role, value, upper)
