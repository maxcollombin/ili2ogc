"""Interpret a `source:` (plus sibling `rule:`/`mapping:`) binding entry.

Reads a spec/grammar/mapping/*.yml binding's `source:` against a real
ANTLR ctx. Core principle, which keeps the engine generic even for nested
constructions (e.g. attrTypeDef._collection) or the `path:` mechanism:
whenever an accessor returns a node, distinguish a token (TerminalNode ->
raw text) from a rule (ParserRuleContext -> recursively VISITED by the
builder, which applies that rule's own logic - kind, attribute_bindings).
See _resolve_node().

`source` forms covered: field+index, field+presence,
field+kind:alt_token(+optional), field+multi(+optional), field+optional,
field+alt+index+optional, field+anchor+optional, field+path+optional,
kind:constant+value, composed segments+join(+negative index),
sequence_pattern, field:null alone (value propagated by the build
context).
"""

import warnings
from typing import Any

from antlr4 import ParserRuleContext
from antlr4.tree.Tree import TerminalNode

from interlis.builder import context_access as ca
from interlis.builder.errors import BuildError
from interlis.builder.forward_refs import ForwardRef

ALT_KINDS = {"alt_token", "alt_rule", "alt_token_or_rule", "alt_token_presence"}


def _resolve_node(node: Any, builder: Any, rule: str) -> Any:
    if node is None:
        return None
    if isinstance(node, TerminalNode):
        return node.getText()
    if isinstance(node, ParserRuleContext):
        return builder.visit(node)
    if isinstance(node, list):
        # Some "multi" accessors return their full list even when the
        # binding didn't explicitly ask for a list (e.g. `index: null`
        # calls the accessor with no argument, which for a multi accessor
        # ALWAYS returns the list, never a single element) - resolve each
        # element rather than returning the raw list.
        resolved = [r for r in (_resolve_node(n, builder, rule) for n in node) if r is not None]
        if len(resolved) == 1:
            return resolved[0]
        return resolved if resolved else None
    return node


def _resolve_multi_node(node: Any, builder: Any, rule: str, wrap_map: dict) -> Any:
    """Like `_resolve_node`, but promotes a node to a typed metamodel instance.

    Promotes a node (rule-bag OR bare token) when `wrap_map` covers its
    rule/token name. Same principle as the `wrap:` fallback already used by `resolve_source`'s
    alt branch, extended here to the `multi: true` branch - needed for
    `coordinateType.Axis`, `AssociationAxisSpec.Axis {1..3} NumType
    ORDERED`: each bare `numeric()`/`NUMERIC` in the list must become its
    own `NumType`, never a shared raw bag (otherwise `CoordType.Axis` holds
    only raw dicts, never real `NumType` instances, silently unusable by
    downstream code expecting `MetaInstance` - e.g. schema.coord_axes). A
    bare token covered by `wrap_map` (e.g. `NUMERIC` with no range/unit)
    produces an empty instance of the target type (see coordinateType.Axis's
    note: "a bare NUMERIC keyword ... still produces a NumType instance").
    """
    if isinstance(node, ParserRuleContext):
        rule_name = builder._rule_name(node)
        if rule_name in wrap_map:
            return builder.visit_wrapped(node, wrap_map[rule_name], rule)
        return _resolve_node(node, builder, rule)
    if isinstance(node, TerminalNode):
        token_name = _token_name(node, builder)
        if token_name is not None and token_name in wrap_map:
            return builder.registry.new_instance(wrap_map[token_name])
        return node.getText()
    return _resolve_node(node, builder, rule)


def _token_name(node: Any, builder: Any) -> str | None:
    symbol = getattr(node, "symbol", None)
    if symbol is None:
        return None
    names = builder.parser_symbolic_names
    idx = symbol.type
    return names[idx] if 0 <= idx < len(names) else None


def resolve_source(
    ctx: Any,
    source: dict,
    *,
    rule: str,
    construction_context: dict,
    builder: Any,
    rule_map: dict | None = None,
    binding_key: str | None = None,
    wrap_map: dict | None = None,
) -> Any:
    if not source:
        return None

    optional = bool(source.get("optional"))

    if source.get("kind") == "constant":
        return source.get("value")

    # --- child_index: RAW position in ctx.children (not a named accessor) -
    # needed when several grammar alternatives share the SAME set of token
    # names at DIFFERENT positions, and another occurrence of the same
    # token type exists ELSEWHERE in the SAME ctx for an UNRELATED pattern
    # (see numeric.Min/Max, 06_types.yml, for a real case).
    # `numeric()` has 4 alternatives ('(Number|PosNumber|Dec) DOTDOT
    # (Number|PosNumber|Dec)', confirmed against the real ANTLR code)
    # ALWAYS at child positions 0/1/2 (operand, DOTDOT, operand) regardless
    # of which alternative matched - but the generic `alt_rule` mechanism
    # (composed field + has_accessor + index) was fooled by an UNRELATED
    # `PosNumber` existing further in the SAME ctx (the optional REFSYS
    # clause `{ Name PosNumber }`, e.g. `{CHLV03[1]}`): `ca.has_accessor(ctx,
    # "PosNumber")` is ALWAYS true (the method is always present on
    # NumericContext), and `ctx.PosNumber(0)` returned that refsys
    # PosNumber instead of None whenever the alternative ACTUALLY taken was
    # 'Dec DOTDOT Dec' (has_accessor tests whether the METHOD exists, not
    # whether THIS specific occurrence does). Confirmed real on
    # `ili_corpus/CHBase_Part1_GEOMETRY_V1.ili` (`Coord2 = COORD 460000.000
    # .. 870000.000 [m] {CHLV03[1]}, ...`: Min wrongly resolved to '1' - the
    # refsys PosNumber - instead of '460000.000', while Max (index 1, which
    # correctly fell back to Dec for lack of a 2nd PosNumber) stayed
    # correct). A simple RAW position (child 0 = 1st operand, child 2 = 2nd
    # operand - DOTDOT is always child 1) is reliable for this exact
    # pattern, unlike the accessor name.
    if "child_index" in source:
        idx = source["child_index"]
        children = list(ctx.children or [])
        node = children[idx] if 0 <= idx < len(children) else None
        if node is None:
            if optional:
                return None
            raise BuildError(f"child_index={idx!r} missing on {type(ctx).__name__}", rule=rule, ctx=ctx)
        return _resolve_node(node, builder, rule)

    field = source.get("field")

    # field: null alone -> value propagated by the enclosing rule, under the
    # SAME attribute key (e.g. interlis2def.iliVersion -> modeldef.iliVersion,
    # same name on both sides - a minority mechanism, unavoidable without
    # changing the spec, wired explicitly rather than a hidden generic
    # solution).
    if field is None:
        key = source.get("context_key") or binding_key or rule
        value = construction_context.get(key)
        if source.get("as_forward_ref") and isinstance(value, str):
            # Wraps a raw value (e.g. a name imported via modeldef.imports,
            # for_each loop - see InterlisModelBuilder) into a named
            # reference, resolved later like any other (SymbolTable, or
            # UnresolvedNamedReference if outside the current file - V1
            # IMPORTS scope decision).
            return ForwardRef(name=value, rule=rule, always_external=bool(source.get("always_external_ref")))
        return value

    kind = source.get("kind")

    # --- multi: list, each element resolved (token -> text, rule ->
    # visited). Checked BEFORE the alt/'|' case below: a composed field
    # (e.g. 'Name|INTERLIS') combined with multi:true must list ALL
    # occurrences of each alternative, not stop at the first (otherwise
    # ca.call(ctx, name) on a "multi" accessor called without an index
    # would return its FULL list, not a single node - a latent bug). -----
    if source.get("multi"):
        names = [n.strip() for n in field.split("|")] if "|" in field else [field]
        nodes: list[Any] = []
        for name in names:
            if ca.has_accessor(ctx, name):
                nodes.extend(ca.call_list(ctx, name))
        between = source.get("between")
        if between and nodes:
            # The same accessor (e.g. Name) reused at several distinct
            # grammar positions within the SAME rule (e.g. modeldef: its
            # own name, an optional language name, imported names, the
            # closing name after END - all 'Name') : keep only the
            # occurrences BETWEEN two anchor tokens (e.g. ['IMPORTS', 'END']
            # for modeldef.imports), by position in ctx.children rather
            # than a fixed flat index.
            start_name, end_name = between
            children = list(ctx.children or [])
            start_positions = [children.index(n) for n in ca.call_list(ctx, start_name) if n in children]
            end_positions = [children.index(n) for n in ca.call_list(ctx, end_name) if n in children]
            if start_positions and end_positions:
                lo, hi = min(start_positions), min(end_positions)
                nodes = [n for n in nodes if lo < children.index(n) < hi]
            else:
                nodes = []
        if wrap_map:
            return [_resolve_multi_node(n, builder, rule, wrap_map) for n in nodes]
        return [_resolve_node(n, builder, rule) for n in nodes]

    # --- alternatives (alt_token / alt_rule / alt_token_or_rule /
    # alt_token_presence): several field names separated by '|', keep the
    # first present one. The mapping key (rule_map) is the field NAME that
    # matched (not its text), or "absent" if none did. ----------
    if kind in ALT_KINDS or "|" in field:
        names = [n.strip() for n in field.split("|")]
        alt_index = source.get("index")
        for name in names:
            if not ca.has_accessor(ctx, name):
                continue
            # An index (e.g. Min: index 0, Max: index 1 on the SAME
            # 'Number|PosNumber|Dec' alternative group) selects WHICH
            # occurrence of the matched name to use, not just its presence -
            # ignoring it would give the full list (or the single/first
            # element) instead of the intended operand. But an index may
            # not apply to ALL alternatives in the group (e.g.
            # cardinality.Max: 'MUL|PosNumber', index:1 - MUL/'*' is a
            # single accessor, the index only concerns the PosNumber
            # branch) - fall back to the accessor without an index rather
            # than a TypeError.
            try:
                node = ca.call(ctx, name, alt_index)
            except TypeError:
                node = ca.call(ctx, name)
            if isinstance(node, list):
                # `name` is a "multi" accessor on THIS ctx (can appear
                # several times - e.g. numeric()/enumeration() on
                # DomainDefContext, which loops over N domain declarations,
                # even though only one alternative is in play per
                # declaration) and no explicit index matched (alt_index is
                # None or doesn't apply here): `ca.call` without an index
                # then returns the FULL list, never None, even when it's
                # EMPTY - without this guard, an empty list was wrongly
                # treated as "alternative present" (found on
                # domainDef._domain_content: "numeric" always matched first
                # with an empty list before "enumeration", the actually
                # present alternative, was ever tried).
                node = node[0] if node else None
            if node is not None:
                if kind == "alt_token_presence":
                    value = name
                elif wrap_map is not None and name in wrap_map and isinstance(node, ParserRuleContext):
                    # The matched alternative is a rule-bag (Container/
                    # ValueObject, e.g. numeric()/enumeration()) to promote
                    # into a typed metamodel instance (e.g. NumType/EnumType).
                    # The instance must exist and be on top of the
                    # construction stack BEFORE the visit (not after): some
                    # children of the rule-bag self-attach via their own
                    # `parent:` DURING the visit (e.g. enumElement ->
                    # TopNode/SubNode) - visiting first and wrapping
                    # afterwards would attach them to the wrong parent (the
                    # one already on top of the stack at that point, not the
                    # new instance). See InterlisModelBuilder.visit_wrapped.
                    return builder.visit_wrapped(node, wrap_map[name], rule)
                else:
                    value = _resolve_node(node, builder, rule)
                if rule_map is not None:
                    return rule_map.get(name, value)
                return value
        if rule_map is not None and "absent" in rule_map:
            return rule_map["absent"]
        # No alternative present and no "absent" key for an explicit
        # default: treated as optional by default, whether `optional: true`
        # is set or not. Rationale: every real alt_token/alt_rule case
        # examined either has an "absent" entry in rule_map (a default
        # value exists), or is documented as optional in its note without
        # the structural `optional: true` flag consistently following
        # (several occurrences found and fixed case by case, e.g.
        # numeric.Clockwise - but the pattern recurs more often than it's
        # convenient to fix one by one) - no real case found where a
        # missing alternative should be a blocking error.
        # Diagnostic id BUILD-SPEC-GAP-ALT-ABSENT (interlis.diagnostic_ids):
        # a class-A mapping gap, but mostly benign optional-absent noise -
        # left as a plain warning, not surfaced by the CLI diagnostics bag
        # by default.
        warnings.warn(
            f"[BUILD-SPEC-GAP-ALT-ABSENT] [{rule}] no alternative present among {names!r} - treated as absent/None",
        )
        return None

    # --- sequence_pattern: recognizes an exact sequence of consecutive
    # tokens starting at `field` (e.g. roleDef.Strongness: '--' vs '-<>' vs
    # '-<#>' -> Assoc/Aggr/Comp). The mapping key is the sequence of token
    # NAMES (e.g. "MINUS LT GT"), not the text. ----------
    if source.get("sequence_pattern"):
        if rule_map is None:
            raise BuildError("sequence_pattern with no associated rule:/mapping:", rule=rule, ctx=ctx)
        return _match_sequence_pattern(ctx, field, rule_map, builder, rule, optional)

    # --- anchor: position relative to an anchor token (not a flat index -
    # needed when several occurrences of the same field type exist and only
    # the one following a specific token matters). ----------
    if "anchor" in source:
        return _resolve_anchor(ctx, field, source["anchor"], optional=optional, builder=builder, rule=rule)

    # --- path: delegates to a sub-rule (Container/ValueObject, visited via
    # _resolve_node -> returns a bag dict), extracts one key. ----------
    if "path" in source:
        if not ca.has_accessor(ctx, field):
            raise BuildError(f"accessor {field!r} not found on {type(ctx).__name__}", rule=rule, ctx=ctx)
        node = ca.call(ctx, field)
        if node is None:
            if optional:
                return None
            raise BuildError(f"{field!r} missing (path={source['path']!r})", rule=rule, ctx=ctx)
        bag = _resolve_node(node, builder, rule)
        if not isinstance(bag, dict):
            raise BuildError(
                f"path={source['path']!r} expected on a bag (dict), got {type(bag).__name__}", rule=rule, ctx=ctx
            )
        return bag.get(source["path"])

    # --- presence: boolean (does the field appear or not). ------------------
    if source.get("presence"):
        present = ca.is_present(ctx, field, source.get("index"))
        if rule_map is not None:
            key = f"{field}_present" if present else f"{field}_absent"
            if key in rule_map:
                return rule_map[key]
        return present

    # --- composed segments + join: concatenation of several sub-values
    # named as sibling keys of `field` (value null = "presence of this
    # segment as-is"), with Python indexing (-1 = last) supported. --------
    if "join" in source:
        return _resolve_join(ctx, source, builder, rule)

    # --- standard case: field (+ index) (+ optional). -------------------
    # No `alt: <int>` filter here on purpose: `ctx.getAltNumber()`
    # unconditionally returns 0 for every rule in `InterlisParser.g4` (no
    # alternative is labelled anywhere in the vendored grammar), so any
    # such filter would always evaluate false and silently leave the
    # binding None (see formattedType.Format/Min/Max for a case this broke
    # in practice). The accessor name (`field:`) is already naturally
    # exclusive to the targeted alternative by grammar construction (e.g.
    # `formattedType` alt1 has a direct `Name` accessor that no other
    # alternative exposes; `pathEl` only exposes `Name` directly in its
    # alternatives 5/6/9, never 1-4/7/8), so no alternative-number filter
    # is needed for correct resolution.
    if not ca.has_accessor(ctx, field):
        if optional:
            return None
        raise BuildError(f"accessor {field!r} not found on {type(ctx).__name__}", rule=rule, ctx=ctx)

    node = ca.call(ctx, field, source.get("index"))
    if node is None:
        if optional:
            return None
        raise BuildError(f"{field!r} missing (not optional) on {type(ctx).__name__}", rule=rule, ctx=ctx)
    value = _resolve_node(node, builder, rule)
    if rule_map is not None:
        return rule_map.get(value, value)
    return value


def _resolve_anchor(ctx: Any, field: str, anchor: str, *, optional: bool, builder: Any, rule: str) -> Any:
    if not ca.has_accessor(ctx, anchor):
        if optional:
            return None
        raise BuildError(f"anchor {anchor!r} not found on {type(ctx).__name__}", rule=rule, ctx=ctx)
    anchor_node = ca.call(ctx, anchor)
    if anchor_node is None:
        if optional:
            return None
        raise BuildError(f"anchor {anchor!r} missing", rule=rule, ctx=ctx)
    children = list(ctx.children or [])
    try:
        anchor_index = children.index(anchor_node)
    except ValueError:
        raise BuildError(f"anchor {anchor!r} not found in ctx.children", rule=rule, ctx=ctx)
    for node in ca.call_list(ctx, field):
        try:
            idx = children.index(node)
        except ValueError:
            continue
        if idx > anchor_index:
            return _resolve_node(node, builder, rule)
    if optional:
        return None
    raise BuildError(f"{field!r} missing after anchor {anchor!r}", rule=rule, ctx=ctx)


def _resolve_join(ctx: Any, source: dict, builder: Any, rule: str) -> str:
    separator = source["join"]
    field = source.get("field")
    index = source.get("index")
    segments: list[str] = []
    if field is not None:
        if index is not None:
            nodes = ca.call_list(ctx, field)
            if not nodes:
                raise BuildError(f"{field!r} empty (join, index={index})", rule=rule, ctx=ctx)
            segments.append(_resolve_node(nodes[index], builder, rule))
        else:
            segments.append(_resolve_node(ca.call(ctx, field), builder, rule))
    for key, value in source.items():
        if key in ("field", "index", "join", "optional") or value is not None:
            continue
        if not ca.has_accessor(ctx, key):
            continue
        node = ca.call(ctx, key)
        if node is not None:
            segments.append(_resolve_node(node, builder, rule))
    return separator.join(s for s in segments if s is not None)


def _match_sequence_pattern(
    ctx: Any, field: str, rule_map: dict, builder: Any, rule: str, optional: bool = False
) -> Any:
    if not ca.has_accessor(ctx, field):
        raise BuildError(f"accessor {field!r} not found on {type(ctx).__name__}", rule=rule, ctx=ctx)
    # `field` can itself be a "multi" accessor (e.g. '--' = two MINUS
    # tokens): the anchor is ALWAYS the 1st occurrence, the sequence is
    # read from its position in ctx.children. Being absent is legitimate
    # (e.g. roleDef has a 2nd grammar form - a type restriction on an
    # EXISTING role by name - that uses no relationship-strength symbol at
    # all).
    candidates = ca.call_list(ctx, field)
    if not candidates:
        if optional:
            return None
        raise BuildError(f"{field!r} missing (sequence_pattern)", rule=rule, ctx=ctx)
    anchor_node = candidates[0]
    children = list(ctx.children or [])
    try:
        start = children.index(anchor_node)
    except ValueError:
        raise BuildError(f"{field!r} not found in ctx.children", rule=rule, ctx=ctx)

    max_len = max(len(pattern.split()) for pattern in rule_map)
    window = children[start : start + max_len]
    names = [_token_name(n, builder) for n in window]
    for length in range(max_len, 0, -1):
        candidate = " ".join(n for n in names[:length] if n)
        if candidate in rule_map:
            return rule_map[candidate]
    raise BuildError(
        f"no token sequence matches {sorted(rule_map)} starting at {field!r} (got {names!r})",
        rule=rule,
        ctx=ctx,
    )
