"""Generic ModelBuilder engine.

InterlisModelBuilder(InterlisParserVisitor) has NO rule-specific visitXxx
method. The generic visit(ctx) derives the rule name from
type(ctx).__name__, loads the matching spec/grammar/mapping/*.yml entry,
and applies one of 4 execution strategies depending on `kind`.
"""

import warnings
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
        # `MANDATORY <named domain>;` (e.g. "Geom: MANDATORY Coord2D;") -
        # see _apply_pending_mandatory_overrides. AttrOrParam instances
        # whose own MANDATORY couldn't attach directly because their Type
        # was still an unresolved ForwardRef to a (possibly SHARED) named
        # domain at construction time - resolved AFTER
        # forward_refs.resolve_all(), same "record during construction,
        # apply after resolution" pattern as _pending_view_all_of below.
        self._pending_mandatory_overrides: list[MetaInstance] = []
        # "ATTRIBUTE ALL OF <Name>;" (see _expand_view_all_of) needs
        # RenamedBaseView.BaseView already resolved to a real Class -
        # still a ForwardRef at construction time whenever the base is
        # itself forward-referenced (a later TOPIC, or an imported model
        # resolved via ModelRepository), since forward_refs.resolve_all()
        # only runs once at the END of build(). Recorded here during
        # construction, actually expanded in build() AFTER resolve_all().
        self._pending_view_all_of: list[tuple[MetaInstance, ParserRuleContext]] = []
        # "Name := expression" view attributes (see
        # _build_view_bare_attributes) build their AttrOrParam/Derivates
        # eagerly (no forward-ref involved), but resolving their Type needs
        # RenamedBaseView.BaseView already resolved - same ForwardRef
        # timing issue as _pending_view_all_of above, same deferred fix.
        self._pending_view_bare_attrs: list[tuple[MetaInstance, MetaInstance, MetaInstance]] = []
        # `MODEL X (fr) ... TRANSLATION OF Y ["ver"]` - the grammar parses
        # the clause but the metamodel `Model` has no own field for it.
        # Recorded here during construction, aligned against the resolved
        # base model in build() AFTER resolve_all() - see
        # _apply_pending_translations.
        self._pending_translations: list[tuple[MetaInstance, str]] = []
        # Real IlisMeta16 instances for the five predefined INTERLIS types
        # whose names are reserved lexer tokens, so they can never be
        # declared as a `DOMAIN` and reach the builder only as an opaque
        # sentinel (a bare `"INTERLIS.HALIGNMENT"` string from
        # `alignmentType`, or an `INTERLIS.<token>` `structureRef`
        # ForwardRef that resolves to nothing). Materialised on demand by
        # `_predefined_type`, one shared instance per token per builder.
        self._predefined_type_cache: dict[str, MetaInstance] = {}
        self.repository.bind_builder_factory(self._make_sub_builder)

    def _make_sub_builder(self) -> "InterlisModelBuilder":
        return InterlisModelBuilder._from_shared(
            self.schema, self.registry, self.spec, self.attachment, self.repository
        )

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
        added. Only `tree` itself is covered by THIS call - an imported
        model gets the same treatment independently, via its own
        sub-builder's `build()` call (`ModelRepository._get_table` passes
        that model's own `meta_attribute_comments`/
        `meta_attribute_comments_in_file`, not this call's `meta_attributes`).
        """
        self._pending_meta_attributes = sorted(meta_attributes or [], key=lambda triple: triple[0])
        self._meta_attribute_index = 0
        result = self.visit(tree)
        self.forward_refs.resolve_all(repository=self.repository)
        self._apply_pending_mandatory_overrides()
        self._apply_pending_view_all_of()
        self._apply_pending_view_bare_attr_types()
        self._apply_pending_translations()
        return result

    def _apply_pending_mandatory_overrides(self) -> None:
        """Give each attribute queued in `_pending_mandatory_overrides` its OWN, Mandatory=True `Type` clone.

        `DomainType.Mandatory` (own attribute) is the ONLY place
        `MANDATORY` can attach in this metamodel (`AttrOrParam` itself has
        none - confirmed against `ilismeta16-classes.yml`) - correct for
        an INLINE type (a fresh instance already built just for that one
        attribute) but wrong for a NAMED domain reference: every attribute
        referencing that domain resolves to the SAME registered instance
        (confirmed empirically), so setting `Mandatory` directly on it
        would incorrectly mark every OTHER use of the same domain as
        mandatory too - eCH-0031 SS3.6 confirms `MANDATORY <DomainRef>` is
        real, legal syntax (`AttrTypeDef = 'MANDATORY' [ AttrType ] | ...`,
        `AttrType` includes `DomainRef`), and real corpus-wide (292 raw
        occurrences, `ili_corpus/`), not a rare edge case.

        Runs AFTER `forward_refs.resolve_all()`, so `instance.Type` (still
        a `ForwardRef` at the point `_attach_unclaimed_results` queued this
        instance) now holds the actual resolved instance - a plain shallow
        clone of it (own+inherited fields, no need to deep-copy any
        composite association like `MetaAttribute`: nothing mutates a
        built DomainType instance further after this point) with
        `Mandatory` forced `True` replaces `instance.Type`, leaving the
        original SHARED instance completely untouched for every other
        attribute still referencing it. A domain already declared
        `Mandatory=True` itself needs no clone (already correct).
        """
        for instance in self._pending_mandatory_overrides:
            resolved = getattr(instance, "Type", None)
            if not isinstance(resolved, MetaInstance) or bool(getattr(resolved, "Mandatory", False)):
                continue
            fields = {
                k: v for k, v in {**resolved.__dict__, **(resolved.model_extra or {})}.items() if not k.startswith("_")
            }
            fields["Mandatory"] = True
            instance.Type = self.registry.new_instance(resolved._qualified_class, **fields)

    # Reference Manual eCH-0031 V2.1.0 Annex A / §3.8: the predefined
    # INTERLIS types whose names are reserved lexer tokens.
    #   HALIGNMENT (FINAL) = (Left, Center, Right) ORDERED;
    #   VALIGNMENT (FINAL) = (Top, Cap, Half, Base, Bottom) ORDERED;
    #   BOOLEAN    (FINAL) = (false, true) ORDERED;   -> BooleanType, matching
    #                        this builder's own handling of the bare BOOLEAN keyword
    #   URI     (FINAL) = TEXT*1023;                  -> TextType Kind=Uri
    #   UUIDOID EXTENDS ANYOID = OID TEXT*36;         -> TextType (36 chars)
    _PREDEFINED_ENUM_ELEMENTS: dict[str, tuple[str, ...]] = {
        "HALIGNMENT": ("Left", "Center", "Right"),
        "VALIGNMENT": ("Top", "Cap", "Half", "Base", "Bottom"),
    }

    def _predefined_type(self, token: str) -> MetaInstance | None:
        """Return the shared IlisMeta16 instance for a predefined `INTERLIS.<token>` type.

        `token` is the bare segment (`HALIGNMENT`/`VALIGNMENT`/`BOOLEAN`/
        `URI`/`UUIDOID`). One instance per token per builder - `Mandatory`
        is never set here; an attribute needing it gets a private clone via
        `_pending_mandatory_overrides`, the same mechanism as a named
        DOMAIN reference.
        """
        cached = self._predefined_type_cache.get(token)
        if cached is not None:
            return cached
        instance: MetaInstance | None = None
        if token in self._PREDEFINED_ENUM_ELEMENTS:
            instance = self.registry.new_instance("IlisMeta16.ModelData.EnumType")
            instance.Name = token
            instance.Order = "Ordered"
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                instance,
                "TopNode",
                top,
                association="TopNode",
                role="TopNode",
                rule="<predefined>",
            )
            for element_name in self._PREDEFINED_ENUM_ELEMENTS[token]:
                node = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
                node.Name = element_name
                self.attachment.attach(
                    top,
                    "Node",
                    node,
                    association="SubNode",
                    role="Node",
                    rule="<predefined>",
                )
        elif token == "BOOLEAN":
            instance = self.registry.new_instance("IlisMeta16.ModelData.BooleanType")
            instance.Name = "BOOLEAN"
        elif token == "URI":
            instance = self.registry.new_instance("IlisMeta16.ModelData.TextType")
            instance.Name = "URI"
            instance.Kind = "Uri"
        elif token == "UUIDOID":
            instance = self.registry.new_instance("IlisMeta16.ModelData.TextType")
            instance.Name = "UUIDOID"
            instance.Kind = "Text"
            instance.MaxLength = 36
        if instance is not None:
            self._predefined_type_cache[token] = instance
        return instance

    def _resolve_predefined_type_sentinel(self, instance: MetaInstance, bag: dict[str, Any]) -> None:
        """Replace a predefined-`INTERLIS`-token sentinel in an `attrTypeDef` bag with a real Type.

        `bag["Type"]` reaches `_attach_unclaimed_results` as an opaque
        `"INTERLIS.HALIGNMENT"`/`"INTERLIS.VALIGNMENT"` string (from
        `alignmentType`, which builds no instance) or as an
        `INTERLIS.BOOLEAN`/`URI`/`UUIDOID` `structureRef` ForwardRef (which
        resolves to nothing - the name is not a real structure Class). Swap
        in the materialised type so the rest of the bag loop attaches it
        like any other; queue `_pending_mandatory_overrides` for a leading
        `MANDATORY` (dropped here so the shared instance is never mutated).
        """
        type_value = bag.get("Type")
        token: str | None = None
        if isinstance(type_value, str) and type_value in ("INTERLIS.HALIGNMENT", "INTERLIS.VALIGNMENT"):
            token = type_value.split(".", 1)[1]
        elif isinstance(type_value, ForwardRef) and type_value.name in (
            "INTERLIS.BOOLEAN",
            "INTERLIS.URI",
            "INTERLIS.UUIDOID",
        ):
            token = type_value.name.split(".", 1)[1]
        if token is None:
            return
        synthesized = self._predefined_type(token)
        if synthesized is None:
            return
        bag["Type"] = synthesized
        if bag.pop("Mandatory", False) is True:
            self._pending_mandatory_overrides.append(instance)

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
                instance,
                "MetaAttribute",
                meta,
                association="MetaAttributes",
                role="MetaAttribute",
                rule="metaAttribute",
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
        elif rule_name == "modeldef":
            self._register_translation_of(instance, ctx)
        elif rule_name == "unitDef":
            self._register_unit_alias(instance, ctx)
        elif rule_name == "viewDef":
            self._set_view_formation_kind(instance, ctx)
            self._expand_view_all_of(instance, ctx)
            self._build_view_bare_attributes(instance, ctx)
        elif rule_name == "constant":
            self._normalize_enumeration_const_value(instance)
        elif rule_name == "pathEl":
            self._set_path_el_kind(instance, ctx)

        if entry.parent and self._parent_stack:
            self.attachment.attach(
                self._parent_stack[-1],
                entry.parent.role,
                instance,
                association=entry.parent.association,
                role=entry.parent.role,
                rule=rule_name,
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

    def _register_translation_of(self, model: MetaInstance, ctx: ParserRuleContext) -> None:
        """Record `MODEL X (fr) ... TRANSLATION OF Y ["ver"]` for deferred alignment against `Y`.

        The grammar (`modeldef`) matches `TRANSLATION OF Name LSBR STRING
        RSBR`; the `Model` metamodel class has no field for it
        (`spec/grammar/mapping/02_packages.yml`). A translation model
        re-declares the base's whole structure with translated
        identifiers, matched POSITIONALLY (the grammar has no `==` rename
        syntax) - `_apply_pending_translations`, after `resolve_all()`,
        walks the two built element trees in parallel and records the name
        map. The transfer format is unchanged for a translation (refman
        §4.3.3), so this only feeds a `--lang` output overlay in the
        converters, never parsing.
        """
        if ca.call(ctx, "TRANSLATION") is None:
            return
        children = list(ctx.children or [])
        node = ca.call(ctx, "TRANSLATION")
        idx = children.index(node)
        # TRANSLATION OF <Name> - the base model name is two tokens on
        if idx + 2 < len(children) and isinstance(children[idx + 2], TerminalNode):
            self._pending_translations.append((model, children[idx + 2].getText()))

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
                self._apply_bindings(
                    submodel, ctx, rule_name, prefixed_bindings.get("SubModel", {}), construction_ctx, consumed
                )
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
                    self._parent_stack[-1],
                    entry.parent.role,
                    inst,
                    association=entry.parent.association,
                    role=entry.parent.role,
                    rule=rule_name,
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
            mandatory = any(isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.MANDATORY for c in segment)
            content_node = next(
                (
                    c
                    for c in segment
                    if isinstance(c, ParserRuleContext) and self._rule_name(c) in ("type", "numeric", "enumeration")
                ),
                None,
            )
            if content_node is None:
                class_token = next(
                    (c for c in segment if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.CLASS),
                    None,
                )
                if class_token is None:
                    # Unreachable in practice: a bare "STRING DOTDOT STRING" domainDef
                    # alternative parses via type_'s own alternative instead (ANTLR
                    # resolves the ambiguity there - see type.text_range_alt,
                    # spec/grammar/mapping/06_types.yml). A defensive no-op, not a real gap.
                    continue
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
                        self._parent_stack[-1],
                        entry.parent.role,
                        instance,
                        association=entry.parent.association,
                        role=entry.parent.role,
                        rule=rule_name,
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
                target = (
                    "IlisMeta16.ModelData.NumType" if content_rule == "numeric" else "IlisMeta16.ModelData.EnumType"
                )
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
                    self._parent_stack[-1],
                    entry.parent.role,
                    instance,
                    association=entry.parent.association,
                    role=entry.parent.role,
                    rule=rule_name,
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
            (
                i
                for i, c in enumerate(segment)
                if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.EXTENDS
            ),
            None,
        )
        if extends_idx is None:
            return
        domain_ref_node = next(
            (
                c
                for c in segment[extends_idx + 1 :]
                if isinstance(c, ParserRuleContext) and self._rule_name(c) == "domainRef"
            ),
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
        refs = [
            c for c in segment if isinstance(c, ParserRuleContext) and self._rule_name(c) == "classOrAssociationRef"
        ]
        for ref_ctx in refs:
            value = self.visit(ref_ctx)
            if value is None:
                continue
            self.attachment.attach(
                instance,
                "BaseClass",
                value,
                association="BaseClass",
                role="BaseClass",
                rule=rule_name,
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
        strings = [
            c for c in (ctx.children or []) if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.STRING
        ]
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
            name=".".join(names),
            resolves_to_hint=hints or None,
            rule=rule_name,
            home_model=self._current_model_name(),
            topic_extends_hint=self._current_topic_extends_hint(ctx),
        )

    def _build_local_uniqueness_def(self, ctx: ParserRuleContext) -> dict[str, Any]:
        """Build `{Kind: "LocalU", UniqueDef: [PathOrInspFactor, ...]}` for `localUniqueness()`.

        Grammar: `LPAR LOCAL RPAR (Name COLON)? Name (MINUS GT Name)* (COLON
        Name (COMMA Name)*)?` - real corpus usage (confirmed against every
        `UNIQUE (LOCAL)` occurrence in ili_corpus/) is always the single
        shape `RoleName: AttrName` (e.g. `UNIQUE (LOCAL) Entries: Code;`,
        `Entries` the `BAG`/`LIST OF STRUCTURE` attribute, `Code` a member
        of that structure) - never the optional leading `(Name COLON)?`
        label, never a multi-hop role path, never more than one trailing
        attribute name.

        This can't be a generic `attribute_bindings` entry: `ctx.Name()`
        returns EVERY `Name` token in the rule (label + role path + trailing
        attributes all share the one accessor, like `pathEl`'s alternatives)
        - AND, confirmed empirically (`ParserATNSimulator.adaptivePredict`
        instrumented directly against the real corpus shape), ANTLR's own
        `la_` decision for the leading `(Name COLON)?` is genuinely
        AMBIGUOUS for `Entries: Code` (both "label=Entries, role=[Code],
        no trailing attrs" and "no label, role=[Entries], trailing=[Code]"
        are equally valid completions of the rule alone) and resolves to
        the WRONG one for this project's real usage (greedily takes the
        optional branch, discarding `Entries` - the actual `BAG`/`LIST`
        attribute - as an unused label and misreading `Code` as the role
        path instead). The parse TREE (raw children) still holds the
        correct token sequence either way (ANTLR's internal branch choice
        only decides which `match()` calls fire, not what children get
        appended) - re-derived here directly, ignoring that internal
        choice entirely: everything up to the FIRST `COLON` is the role
        path (`Name`/`MINUS`/`GT` alternation), everything after is the
        comma-separated attribute list. No real corpus case has a second
        `COLON` (the label form) to conflict with this reading.

        One `PathOrInspFactor` per trailing attribute name, `PathEls` =
        the role path's `PathEl`s + that attribute's own `PathEl` (`Kind`
        always `"ReferenceAttr"`, same permissive convention as
        `globalUniqueness`'s cross-reference paths and
        `constraint_eval.py`'s `_resolve_path`) - `convert/sql.py` maps
        this directly onto the `<attr>_<subattr>`-flattened columns of the
        `BAG`/`LIST OF STRUCTURE`'s own child table. No trailing attribute
        at all (not seen in the real corpus either) falls back to a single
        `PathOrInspFactor` over the role path alone (the element's own
        scalar value must be locally unique).
        """
        children = list(ctx.getChildren())[3:]  # skip LPAR LOCAL RPAR
        colon_index = next(
            (
                i
                for i, c in enumerate(children)
                if isinstance(c, TerminalNode) and c.symbol.type == InterlisParser.COLON
            ),
            None,
        )
        role_tokens = children[:colon_index] if colon_index is not None else children
        attr_tokens = children[colon_index + 1 :] if colon_index is not None else []

        def path_el(name: str) -> MetaInstance:
            el = self.registry.new_instance("IlisMeta16.ModelData.PathEl")
            el.Kind = "ReferenceAttr"
            el.Ref = name
            return el

        role_names = [
            t.getText() for t in role_tokens if isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.Name
        ]
        attr_names = [
            t.getText() for t in attr_tokens if isinstance(t, TerminalNode) and t.symbol.type == InterlisParser.Name
        ]

        def factor(names: list[str]) -> MetaInstance:
            instance = self.registry.new_instance("IlisMeta16.ModelData.PathOrInspFactor")
            instance.PathEls = [
                path_el(name) for name in names
            ]  # a fresh PathEl per factor - never shared across UniqueDef entries
            return instance

        if not role_names:
            return {"Kind": "LocalU"}
        if not attr_names:
            return {"Kind": "LocalU", "UniqueDef": [factor(role_names)]}
        return {"Kind": "LocalU", "UniqueDef": [factor([*role_names, name]) for name in attr_names]}

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
        is_top_level = (
            isinstance(parent_context, MetaInstance)
            and parent_context._qualified_class == "IlisMeta16.ModelData.EnumType"
        )

        if is_top_level:
            top = self.registry.new_instance("IlisMeta16.ModelData.EnumNode")
            top.Name = "TOP"
            self.attachment.attach(
                parent_context,
                "TopNode",
                top,
                association="TopNode",
                role="TopNode",
                rule=rule_name,
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
                        children_target,
                        "Node",
                        node,
                        association="SubNode",
                        role="Node",
                        rule=rule_name,
                    )

        construction_ctx = self._construction_stack[-1] if self._construction_stack else {}
        consumed: set[int] = set()
        final_binding = entry.attribute_bindings.get("Final") if entry.attribute_bindings else None
        if final_target is not None and isinstance(final_binding, dict):
            final_value = self._resolve_binding_value(
                ctx, rule_name, "Final", final_binding, construction_ctx, consumed
            )
            if final_value:
                self.attachment.attach(final_target, "Final", final_value, rule=rule_name)
        order_binding = entry.attribute_bindings.get("Order") if entry.attribute_bindings else None
        if is_top_level and isinstance(order_binding, dict):
            order_value = self._resolve_binding_value(
                ctx, rule_name, "Order", order_binding, construction_ctx, consumed
            )
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
            if node is None or (isinstance(node, list) and not node):
                # A MULTI token accessor (e.g. term0's OR, term1's AND/MUL/
                # DIV - each can repeat via the grammar's `*`, so ANTLR
                # generates `getTokens`, never `getToken`) returns an EMPTY
                # LIST, never `None`, when the operator is absent - `node is
                # None` alone never caught this, so term0/term1 ALWAYS took
                # this branch (even for a single term2/term1 with no
                # operator at all), wrapping it in a spurious CompoundExpr
                # with exactly 1 SubExpressions entry instead of the correct
                # pure pass-through. Confirmed empirically (a plain `KBfrei
                # == #false` constraint produced a phantom
                # CompoundExpr(Operation='And', SubExpressions=[<1 item>])
                # wrapping the real Relation CompoundExpr).
                continue
            instance = self.registry.new_instance(branch.target)
            instance._source_ctx = ctx
            if branch.discriminant:
                value = branch.discriminant.model_extra.get("value")
                if rule_name == "term2" and token_or_rule == "relation":
                    # CompoundExpr.Operation=Relation is a PARENT kind-value
                    # with its own children (Equal/NotEqual/Less/Greater/
                    # LessOrEqual/GreaterOrEqual, ilismeta16-kind-values.yml)
                    # - the real sub-value relation() matched, never the
                    # literal group name "Relation" itself. relation()'s OWN
                    # attribute_bindings.RelationKind never actually worked
                    # (composed field names like "EQ+EQ"/"LT+GT" don't match
                    # any real ANTLR accessor - confirmed empirically,
                    # always resolved to `None`), so this is resolved here
                    # directly against RelationContext's real accessors
                    # instead (`_relation_kind`, same "read the raw ctx
                    # directly" pattern as `_set_view_formation_kind`/
                    # `_set_join_or_null`).
                    value = self._relation_kind(node) or value
                setattr(instance, branch.discriminant.attribute, value)

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
                if rule_name == "predicate" and token_or_rule == "DEFINED":
                    self._set_defined_subexpression(instance, ctx)
                if rule_name == "factor" and token_or_rule == "INTERLIS":
                    self._set_predefined_function_call(instance, ctx)
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

        # localUniqueness(): 6th special case - see _build_local_uniqueness_def
        # for why UniqueDef needs raw-children handling (a genuine grammar
        # ambiguity, not just a missing accessor).
        if rule_name == "localUniqueness":
            return self._build_local_uniqueness_def(ctx)

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
            ((only_key, only_value),) = bag.items()
            if only_key.startswith("_") or not entry.feeds_into:
                # Unwrap to the bare value in the common case: either a
                # single notes-only/relay key (e.g. attributePath's own
                # `_dispatch`, pure pass-through to objectOrAttributePath -
                # which IS this rule's real content once the internal-
                # marker wrapper is stripped), OR a rule with no
                # `feeds_into:` at all (e.g. textConst's `Value`, whose
                # single key exists only for readability in the spec - the
                # PARENT rule's own binding, e.g. constant.Value's
                # `alt_rule` dispatch, consumes the bare resolved value
                # directly via `_resolve_node`, never a {key: value} dict).
                return only_value
            # A single REAL (non-underscore) attribute_bindings key on a
            # rule that DOES declare `feeds_into:` (e.g.
            # objectOrAttributePath's `PathEls`, feeds_into: PathOrInspFactor)
            # must stay a {key: value} bag, not be unwrapped to a bare
            # value: a Conditional branch merges this rule's result via
            # `_merge_bag_into_instance`, which requires a dict (`if not
            # isinstance(bag, dict): return`) - unwrapping here silently
            # discarded it, the real root cause of
            # `PathOrInspFactor.PathEls` staying empty for every bare
            # attribute-name factor (e.g. `KBfrei` in `KBfrei == #false`),
            # confirmed empirically (no `attach(key='PathEls', ...)` call
            # ever fired) before this fix.
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
            name=name,
            resolves_to_hint=expanded or None,
            rule=rule_name,
            home_model=self._current_model_name(),
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
            qn.rsplit(".", 1)[-1]
            for qn, el in self.schema.uml.qualified.items()
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
            if (
                binding.get("status") in ("not_applicable", "unresolved")
                and "source" not in binding
                and "target" not in binding
            ):
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
                instance,
                key,
                nested,
                association=binding.get("association"),
                role=binding.get("role"),
                rule=rule_name,
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
                instance,
                key,
                value,
                association=binding.get("association"),
                role=binding.get("role"),
                rule=rule_name,
            )
            self.forward_refs.register_pending(value, instance, self._resolved_field_name(instance, key, binding))
            return
        self.attachment.attach(
            instance,
            key,
            value,
            association=binding.get("association"),
            role=binding.get("role"),
            rule=rule_name,
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
        self,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
    ) -> MetaInstance | None:
        target = binding["target"]
        sub_bindings = {
            k: v
            for k, v in binding.items()
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
        items = self._resolve_binding_value(
            ctx, rule_name, key, {"source": binding["for_each"]}, construction_ctx, consumed
        )
        if items is None:
            return
        if not isinstance(items, list):
            items = [items]
        if not items:
            return

        target = binding["target"]
        sub_bindings = {
            k: v
            for k, v in binding.items()
            if k not in ("for_each", "target", "note", "association", "role") and isinstance(v, dict)
        }
        for item in items:
            item_ctx = {**construction_ctx, "__item__": item}
            built = self.registry.new_instance(target)
            built._source_ctx = ctx
            self._apply_bindings(built, ctx, rule_name, sub_bindings, item_ctx, consumed)
            if binding.get("association"):
                self.attachment.attach(
                    instance,
                    key,
                    built,
                    association=binding.get("association"),
                    role=binding.get("role"),
                    rule=rule_name,
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
        self,
        ctx: ParserRuleContext,
        rule_name: str,
        key: str,
        binding: dict,
        construction_ctx: dict,
        consumed: set[int],
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
            ctx,
            source,
            rule=rule_name,
            construction_context=construction_ctx,
            builder=self,
            rule_map=rule_map,
            binding_key=key,
            wrap_map=wrap_map,
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
                    ctx,
                    source,
                    rule=rule_name,
                    construction_context=construction_ctx,
                    builder=self,
                    rule_map=rule_map,
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
        stayed `None`/falsy for every JOIN, silently breaking backlog item
        8 Lot C's outer-join evaluation (`convert/jsonfg.py`'s
        `_join_combinations`) until a synthetic fixture caught it.
        """
        bases = [b for b in getattr(view, "RenamedBaseView", None) or [] if isinstance(b, MetaInstance)]
        children = list(join_ctx.children or [])
        for i, ref_ctx in enumerate(ca.call_list(join_ctx, "renamedViewableRef")):
            if i >= len(bases):
                break
            idx = children.index(ref_ctx)
            if idx + 1 < len(children) and children[idx + 1].getText() == "(":
                bases[i].OrNull = True

    _RELATION_KIND_LT_GT = {(True, False): "Less", (False, True): "Greater", (True, True): "NotEqual"}

    def _relation_kind(self, ctx: ParserRuleContext) -> str | None:
        """Resolve `relation()`'s matched alternative to its `CompoundExpr.Operation` child value.

        See spec/grammar/mapping/07_constraints.yml's `relation` note for
        why this can't be a generic `attribute_bindings` entry (no real
        ANTLR accessor for a composed multi-token alternative like
        "EQ EQ"/"LT GT"). Reads `RelationContext`'s real accessors
        directly instead, same "read the raw ctx" pattern as
        `_set_view_formation_kind`/`_set_join_or_null`.

        `EQ()` is a MULTI accessor (alt1, "==", consumes 2 EQ tokens) -
        `>= 2` distinguishes it unambiguously from every other alternative
        (EQ never appears anywhere else in this rule). `LT()`/`GT()` are
        each used BOTH standalone (alt6/alt7, "<"/">") AND together
        (alt3, "<>" - not-equal) - only checking whether BOTH are present
        at once disambiguates "<>" from a lone "<" or ">".
        """
        if len(ca.call_list(ctx, "EQ")) >= 2:
            return "Equal"
        if ca.call(ctx, "NOT_EQ") is not None:
            return "NotEqual"
        if ca.call(ctx, "LTEQ") is not None:
            return "LessOrEqual"
        if ca.call(ctx, "GTEQ") is not None:
            return "GreaterOrEqual"
        has_lt = ca.call(ctx, "LT") is not None
        has_gt = ca.call(ctx, "GT") is not None
        return self._RELATION_KIND_LT_GT.get((has_lt, has_gt))

    def _set_path_el_kind(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Resolve `pathEl()`'s matched alternative to its `PathEl.Kind` value.

        See spec/grammar/mapping/07_constraints.yml's `pathEl` note for
        why this can't be a generic `attribute_bindings` entry (6 of the
        9 alternatives share the same leading `Name` token, so no
        composed field name matches a real ANTLR accessor). Checks the
        unambiguous single-token/single-subrule alternatives first
        (THIS/THISAREA/THATAREA/PARENT/associationPath/attributeRef),
        then disambiguates the 3 remaining bare-`Name` alternatives via
        COLON (alt6, Role), a double `EQ` (alt9, "Name==STRING",
        MetaObject) or `LSBR` (alt5 WITH its bracket, ViewBase) -
        anything else is alt5 without a bracket (ReferenceAttr).
        """
        if ca.call(ctx, "THIS") is not None:
            instance.Kind = "This"
        elif ca.call(ctx, "THISAREA") is not None:
            instance.Kind = "ThisArea"
        elif ca.call(ctx, "THATAREA") is not None:
            instance.Kind = "ThatArea"
        elif ca.call(ctx, "PARENT") is not None:
            instance.Kind = "Parent"
        elif ca.call(ctx, "associationPath") is not None:
            instance.Kind = "AssocPath"
        elif ca.call(ctx, "attributeRef") is not None:
            instance.Kind = "Attribute"
        elif ca.call(ctx, "COLON") is not None:
            instance.Kind = "Role"
        elif len(ca.call_list(ctx, "EQ")) >= 2:
            instance.Kind = "MetaObject"
        elif ca.call(ctx, "LSBR") is not None:
            instance.Kind = "ViewBase"
        else:
            instance.Kind = "ReferenceAttr"

    def _normalize_enumeration_const_value(self, instance: MetaInstance) -> None:
        """Join a `Constant(Type=Enumeration)`'s raw `enumerationConst` bag into a plain dotted-path string.

        `constant()`'s generic `Value` binding (spec/grammar/mapping/06_types.yml)
        dispatches via `alt_rule` to whichever sub-rule matched
        (numericConst/textConst/.../enumerationConst) and assigns its
        resolved value AS-IS - correct for every OTHER alternative
        (already a plain string), but `enumerationConst` is a `Container`
        with 2 REAL keys (`Value`: the Name segments, `Others`: a bool) -
        never unwrapped to a bare value by `_relay` (2 keys, not 1).
        Without this, `Constant.Value` for e.g. `#false` was the raw bag
        `{"Value": ["false"], "Others": False}` instead of the plain
        dotted-path string `"false"` the metamodel actually declares
        (TEXT, ilismeta16-datatypes.yml) - same "Name(.Name)*(.OTHERS)?"/
        bare "OTHERS" convention `xtf.schema.enum_values` already uses for
        a real `EnumType`'s own node tree (eCH-0031 V2.1.0 SS4.3.11.3).
        """
        value = getattr(instance, "Value", None)
        if not isinstance(value, dict):
            return
        segments = value.get("Value") or []
        if isinstance(segments, str):
            segments = [segments]
        if value.get("Others"):
            segments = [*segments, "OTHERS"]
        instance.Value = ".".join(segments)

    def _set_defined_subexpression(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Set `UnaryExpr(Operation=Defined).SubExpression` from `DEFINED LPAR factor RPAR`'s own factor.

        See spec/grammar/mapping/07_constraints.yml's `predicate` note:
        alt3's `factor` accessor has no direct role/attribute name to bind
        against via the generic `attribute_bindings` mechanism (unlike
        alt2's `expression`, which the SAME `SubExpression` binding
        already handles for the NOT branch) - `UnaryExpr.SubExpression`
        is typed `Expression`, and `factor()` builds a `Factor`
        (`Factor EXTENDS Expression`, ilismeta16-datatypes.yml), so a
        direct assignment is enough, no wrapping needed.

        Called only when `predicate`'s DEFINED branch matched (`ctx.factor()`
        unambiguously refers to alt3's factor in that case - `PredicateContext`
        has a single `factor()` accessor, shared by alt1 and alt3, but alt1
        never reaches here since it isn't a `when_present` branch).
        """
        factor_node = ca.call(ctx, "factor")
        if factor_node is None:
            return
        value = self.visit(factor_node)
        if value is not None:
            instance.SubExpression = value

    def _set_predefined_function_call(self, instance: MetaInstance, ctx: ParserRuleContext) -> None:
        """Set `FunctionCall.Function`/`.Arguments` from `factor`'s alt4 (predefined functions, e.g.
        `INTERLIS.len(...)`).

        See spec/grammar/mapping/07_constraints.yml's `factor` entry (the
        `INTERLIS` `when_present` branch) for why this needs a dedicated
        hook: `Function` needs 2 tokens joined (`INTERLIS` + the matched
        `Name`/`URI`/`UUIDOID`), and `Arguments` needs each bare
        `expression()` wrapped in a synthetic `ActualArgument` - this alt
        never goes through the `argument()` rule (unlike `functionCall`'s
        own `Arguments`), so neither has a matching direct accessor.
        """
        names = ctx.Name()
        name_token = names[0] if names else (ctx.URI() or ctx.UUIDOID())
        if name_token is None:
            return
        instance.Function = f"INTERLIS.{name_token.getText()}"
        arguments = []
        for expr_ctx in ctx.expression():
            value = self.visit(expr_ctx)
            if value is None:
                continue
            argument = self.registry.new_instance("IlisMeta16.ModelData.ActualArgument")
            argument.Kind = "Expression"
            argument.Expression = value
            arguments.append(argument)
        instance.Arguments = arguments

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

        Scope of this lot (RULE #7 - real corpus evidence only): only the
        FIRST "ALL OF Name;" in one viewAttributes() (the grammar itself
        only supports one per call - see the ATTRIBUTE-alt1 loop in
        viewAttributes()'s generated body, `(Name ASSIGN expression;)*`
        AFTER "ALL OF Name;", never a second "ALL OF"), and only OWN
        attributes of the matched base (`base.BaseView.ClassAttribute` -
        inherited attributes via EXTENDS not walked here: no real corpus
        evidence yet that a View's "ALL OF" base itself has an EXTENDS
        chain, and duplicating `xtf.schema.attributes_of`'s inheritance
        walk here would cross a layering boundary - `xtf/schema.py`
        imports FROM `interlis.builder`, not the other way around). A base
        that's still unresolved after `resolve_all()` (e.g.
        `UnresolvedNamedReference`, a genuinely absent cross-file model) is
        skipped, same "no crash on a known limit" stance as
        `xtf.schema.attributes_of`.

        `viewAttributes()`'s grammar (`vendor/interlis-antlr4/InterlisParser.g4`)
        used to allow only ONE "ALL OF Name;" per call, picked via a single
        top-level alternative among 4 mutually exclusive ones - a real,
        legal pattern with MULTIPLE consecutive "ALL OF Name;" statements
        (one per base of a multi-base `JOIN OF`/`UNION OF`, e.g.
        `ili_corpus/ERKAS_Strassen_V2_0.ili`'s `VIEW vVA`/`vER`) then had
        its 2nd (and further) "ALL OF" silently swallowed by
        `constraintDef()` instead (confirmed by direct AST inspection - it
        became its own bogus `ConstraintDefContext` with no usable
        content). **Fixed at the grammar level** (2026-08-24, matching the
        official EBNF, Reference Manual eCH-0031 V2.1.0 §3.15
        "ViewAttributes = [ATTRIBUTE] {'ALL' 'OF' Base-Name ';' |
        AttributeDef | Attribute-Name Properties<...> ':=' Expression
        ';'}." - a REPEATED, 0..* choice of 3 alternatives, not a single
        pick among 4): `viewAttributes()` now loops
        (`(ALL OF Name SEMI | attributeDef | Name (Properties)? ASSIGN
        expression SEMI)*`), so ANY number of "ALL OF" clauses (freely
        interleaved with `attributeDef`/`Name := expression` redefinitions,
        in any order, exactly per the EBNF) land in the SAME
        `ViewAttributesContext` - confirmed empirically on the ERKAS
        example above (`ALL()`/`Name()` accessors both become multi).
        Walks `va.children` positionally (same technique as
        `_register_unqualified_imports`) to pair each `ALL` terminal with
        the `Name` terminal immediately following its `OF` - the ONLY
        reliable way to recover "which Name belongs to which ALL OF",
        since `Name` is ALSO used by the (unrelated) `Name := expression`
        alternative in the same repeated group.
        `attributeDef`-form attributes (bare `Name: Type;` inside
        ATTRIBUTE) are unaffected either way - already handled generically
        by the existing engine (verified separately, no change needed
        here), independently of how many times it now repeats.
        """
        pending, self._pending_view_all_of = self._pending_view_all_of, []
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
                        and child.symbol.type in InterlisModelBuilder._VIEW_ATTRIBUTE_MODIFIER_TOKENS
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
        real data is a separate, still-open concern, see
        `.claude/PROGRESS.md`'s "cross-check WFS" item and the known
        `.xtf -> JSON-FG` WHERE-clause exclusion). First `PathEl` selects
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
        has none); own attributes only, no EXTENDS walk (same "no real
        corpus evidence yet" stance as `_apply_pending_view_all_of`) - a
        real corpus survey of all 5 `tests/fixtures/fgdm4gs/` models
        confirmed every referenced attribute is OWN on its class, none
        inherited. An expression that isn't a plain path (no real corpus
        example), or a path that fails to resolve at any hop, leaves
        `Type` unset - same graceful-degradation stance as everywhere else
        in this builder (RULE #5), not a crash.
        """
        pending, self._pending_view_bare_attrs = self._pending_view_bare_attrs, []
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
        for attr in getattr(cls_or_assoc, "ClassAttribute", None) or []:
            if attr.Name == name:
                return attr
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

    # ------------------------------------------------------------------
    # TRANSLATION OF - positional alignment of a translation model
    # against its base (recorded by _register_translation_of).
    # ------------------------------------------------------------------
    _TRANSLATION_CHILD_COLLECTIONS = ("Element", "ClassAttribute", "EnumElement")

    def _apply_pending_translations(self) -> None:
        """Align every `TRANSLATION OF` model against its base, building an `IlisMeta16.ModelTranslation.Translation`.

        A translation model re-declares the base's whole structure with
        translated identifiers, matched POSITIONALLY (the grammar has no
        `==` rename syntax; refman: the two models must "strukturell exakt
        uebereinstimmen"). `_align_translation` walks
        `Model.Element` / `Class.ClassAttribute` / `EnumType.EnumElement`
        of both trees in parallel; every base element whose name changed
        becomes one `METranslation` (`Of` = a real reference to that base
        `MetaElement`, `TranslatedName` = the new name) on a single
        `Translation` (`Language` = the translation model's own). This is
        exactly `IlisMeta16.ModelTranslation` (IlisMeta16.ili's
        `TOPIC ModelTranslation`) - a free-standing object referencing the
        base, no `Model <-> Translation` association exists, so it is kept
        reachable from Python as `model._translation_object` (same as how
        `Import` instances are kept under a raw key).

        Also derives the lookup maps the `--lang` converter overlay uses
        (`model._translation`, `elements`/`attributes` by short name -
        `convert/translation.py`). A base that did not resolve (not on
        `--repo`) is skipped with a warning; a structural mismatch aligns
        the common prefix of that scope and warns, never guesses (RULE #5).
        """
        pending, self._pending_translations = self._pending_translations, []
        for model, base_name in pending:
            base = self._find_model_by_name(base_name)
            if base is None:
                warnings.warn(
                    f"[BUILD-TRANSLATION-BASE-MISSING] TRANSLATION OF {base_name!r}: base model not resolved "
                    f"(pass its directory to --repo) - no name map built for {model.Name!r}",
                    stacklevel=2,
                )
                continue
            names: dict[str, str] = {}
            elements: dict[str, str] = {}
            attributes: dict[tuple[str, str], str] = {}
            pairs: list[tuple[MetaInstance, str]] = []
            self._align_translation(base, model, base.Name or base_name, None, names, elements, attributes, pairs)

            translation = self.registry.new_instance("IlisMeta16.ModelTranslation.Translation")
            translation.Language = getattr(model, "Language", None)
            # METranslation.Of is `REFERENCE TO (EXTERNAL) MetaElement` in
            # IlisMeta16.ili, flattened to a NAME by the metamodel
            # extraction - the base element's fully-qualified name.
            translation.Translations = [
                self.registry.new_instance(
                    "IlisMeta16.ModelTranslation.METranslation",
                    Of=base_qname,
                    TranslatedName=translated_name,
                )
                for base_qname, translated_name in pairs
            ]
            model._translation_object = translation
            model._translation = {
                "language": getattr(model, "Language", None),
                "of": base.Name,
                "names": names,  # {fully-qualified base name: translated short name}
                "elements": elements,  # {base class/view/topic/domain short name: translated}
                "attributes": attributes,  # {(owner short name, attribute short name): translated}
            }

    def _find_model_by_name(self, name: str) -> MetaInstance | None:
        def model_in(table) -> MetaInstance | None:
            for inst in table.all_registered() if table is not None else ():
                if (
                    isinstance(inst, MetaInstance)
                    and inst._qualified_class == "IlisMeta16.ModelData.Model"
                    and inst.Name == name
                ):
                    return inst
            return None

        # A `TRANSLATION OF <base>` is not an IMPORTS, so the base is not
        # loaded during the translation model's own build - ask the
        # repository to build it now (network-free, from --repo).
        return model_in(self.symbol_table) or model_in(self.repository.symbol_table_for(name))

    def _align_translation(
        self,
        base: MetaInstance,
        translated: MetaInstance,
        base_qname: str,
        owner_name: str | None,
        names: dict[str, str],
        elements: dict[str, str],
        attributes: dict[tuple[str, str], str],
        pairs: list[tuple[str, str]],
    ) -> None:
        b_name = getattr(base, "Name", None)
        t_name = getattr(translated, "Name", None)
        is_attr = base._qualified_class.rsplit(".", 1)[-1] == "AttrOrParam"
        if b_name is not None and t_name is not None and b_name != t_name:
            names[base_qname] = t_name
            pairs.append((base_qname, t_name))
            if is_attr and owner_name is not None:
                attributes[(owner_name, b_name)] = t_name
            elif not is_attr:
                elements[b_name] = t_name
        next_owner = b_name if base._qualified_class.rsplit(".", 1)[-1] in ("Class", "View") else owner_name
        for collection in self._TRANSLATION_CHILD_COLLECTIONS:
            b_children = [c for c in getattr(base, collection, None) or [] if isinstance(c, MetaInstance)]
            t_children = [c for c in getattr(translated, collection, None) or [] if isinstance(c, MetaInstance)]
            if len(b_children) != len(t_children):
                warnings.warn(
                    f"[BUILD-TRANSLATION-MISMATCH] TRANSLATION OF {base.Name}: {collection} count differs on "
                    f"{base_qname!r} ({len(b_children)} vs {len(t_children)}) - aligning the common prefix only",
                    stacklevel=2,
                )
            for b_child, t_child in zip(b_children, t_children):
                child_name = getattr(b_child, "Name", None)
                self._align_translation(
                    b_child,
                    t_child,
                    f"{base_qname}.{child_name}" if child_name else base_qname,
                    next_owner,
                    names,
                    elements,
                    attributes,
                    pairs,
                )

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

    def _attach_unclaimed_results(
        self, instance: MetaInstance, sweep_results: list[tuple[str, Any]], rule_name: str
    ) -> None:
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
            if child_rule == "constraintDef":
                # constraintDef is a Container dispatching to
                # mandatoryConstraint/uniquenessConstraint/setConstraint/...
                # - each of THOSE declares its own `parent:`
                # (ClassConstraint/Constraint) and has already self-attached
                # the built constraint. constraintDef itself has no
                # `parent:`, so without this the same constraint gets
                # attached a SECOND time below via
                # find_association_connecting (View/Class <-> Constraint is a
                # real association). A `classDef` never hits this - its
                # `constraintDef`s sit under the `classOrStructureDef`
                # wrapper - but `viewDef` has `constraintDef` as a direct
                # child (a real corpus case: every DMAV `*_Gueltig` VIEW
                # carries a view-level `UNIQUE CHxxxxxx:`).
                continue
            if child_rule == "formationDef":
                # viewDef-only: a pure dispatcher relaying to projection()/
                # join()/union()/aggregation()/inspection() (spec/grammar/
                # mapping/09_views_graphics.yml, "No direct IlisMeta16
                # instance expected"), none of which declare their OWN
                # `parent:` either (they're Containers too) - so the
                # `child_entry.parent` check below can't detect that
                # whatever bubbles up (a RenamedBaseView or list thereof)
                # was already self-attached several levels down by
                # renamedViewableRef's OWN `parent:` (BaseViewDef). Without
                # this, a single-base formationDef (PROJECTION OF) got its
                # one RenamedBaseView re-attached a SECOND time here
                # (find_association_connecting silently succeeds, since
                # View<->RenamedBaseView is a real association) - a
                # multi-base one (JOIN OF/UNION OF) never hit this because
                # its bubbled-up value is a list, not a MetaInstance, so the
                # dict/instance branches below never matched it anyway.
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
                if child_rule == "attrTypeDef":
                    self._resolve_predefined_type_sentinel(instance, value)
                siblings = [v for v in value.values() if isinstance(v, MetaInstance)]
                has_unresolved_sibling = any(isinstance(v, ForwardRef) for v in value.values())
                for key, sub_value in value.items():
                    if (
                        sub_value is None
                        or (isinstance(sub_value, list) and not sub_value)
                        or self._is_hollow(sub_value)
                    ):
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
                                hints = (
                                    sub_value.resolves_to_hint
                                    if isinstance(sub_value.resolves_to_hint, list)
                                    else ([sub_value.resolves_to_hint] if sub_value.resolves_to_hint else [])
                                )
                                candidate_classes = [
                                    qn
                                    for hint in hints
                                    for qn in self.schema.uml.qualified
                                    if qn.rsplit(".", 1)[-1] == hint
                                ]
                            for qualified_class in candidate_classes:
                                found = self.attachment.find_association_connecting(
                                    instance._qualified_class, qualified_class
                                )
                                if found is not None:
                                    _assoc_name, role, upper = found
                                    self.attachment._set_field(instance, role, sub_value, upper)
                                    attached_on = instance
                                    attached_field = role
                                    break
                        if attached_on is None:
                            # `key`'s own attach failed (e.g. "Mandatory",
                            # not a real AttrOrParam attribute) - a sibling
                            # bag key (e.g. attrTypeDef.Type, meant to be
                            # built alongside it) has nowhere for THIS key
                            # to land. Two distinct, non-critical cases
                            # (never a `sub_value` that IS ITSELF the broken
                            # reference - see the `raise` below, unchanged
                            # for that case):
                            if not isinstance(sub_value, ForwardRef) and has_unresolved_sibling:
                                # Case 1: this bag's Type is still an
                                # unresolved ForwardRef (e.g. a reference to
                                # an existing, possibly SHARED, named DOMAIN
                                # - "Geom: MANDATORY Coord2D;"). `Mandatory`
                                # specifically (2026-08-27, backlog item 14):
                                # queued in `_pending_mandatory_overrides`,
                                # resolved by
                                # `_apply_pending_mandatory_overrides` AFTER
                                # `forward_refs.resolve_all()` - a private,
                                # Mandatory=True clone of the resolved Type
                                # replaces `instance.Type`, never mutating
                                # the shared domain instance every OTHER
                                # attribute referencing it might also
                                # resolve to (applying it directly on the
                                # shared instance here, before resolution,
                                # would be wrong regardless of timing - which
                                # use would be right if several differ?).
                                # Anything else in this situation still has
                                # nowhere principled to land - silently
                                # skipped, same as before this fix.
                                if key == "Mandatory" and sub_value is True:
                                    self._pending_mandatory_overrides.append(instance)
                                continue
                            if not isinstance(sub_value, ForwardRef) and not siblings:
                                # Case 2: NO sibling MetaInstance/ForwardRef
                                # exists in this bag AT ALL to even attempt
                                # an attach - confirmed real on `HAli:
                                # MANDATORY HALIGNMENT;`
                                # (CHBase_Part6_GRAPHICANNOTATIONS_V1/V2.ili):
                                # `alignmentType()` (grammar - "HALIGNMENT"/
                                # "VALIGNMENT" are RESERVED tokens,
                                # grammatically impossible to declare via
                                # domainDef - confirmed, `DOMAIN HALIGNMENT
                                # = ...` is a syntax rejection, not a
                                # missing binding) deliberately builds NO
                                # instance at all (target: null, same
                                # category as booleanType/oIDType for
                                # BOOLEAN/ANYOID/UUIDOID) - so its bag only
                                # contains an opaque TEXT value
                                # ("resolved_type"), never an instance for
                                # Mandatory to land on. Before the original
                                # fix for this case, `has_unresolved_sibling`
                                # stayed FALSE (no ForwardRef, just an inert
                                # dict) and the code fell into the `raise` -
                                # `interlis build`/`validate` crashed
                                # entirely as soon as an attribute typed
                                # HALIGNMENT/VALIGNMENT MANDATORY, even
                                # though this type otherwise stays
                                # deliberately uninterpreted (same
                                # documented limitation as BOOLEAN) -
                                # silently dropping Mandatory (secondary
                                # info) rather than a total crash for an
                                # otherwise valid attribute.
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
                hints = (
                    value.resolves_to_hint
                    if isinstance(value.resolves_to_hint, list)
                    else ([value.resolves_to_hint] if value.resolves_to_hint else [])
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
