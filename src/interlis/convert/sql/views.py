"""IlisMeta16 `View` -> SQL `CREATE VIEW`: PROJECTION/JOIN/UNION/AGGREGATION/INSPECTION formation kinds, `WHERE`
translation, VIEW-level `UNIQUE` -> `BEFORE INSERT`/`UPDATE` trigger.

`build_views` is the public entry point - see its own docstring for the
translatable subset.
"""

from __future__ import annotations

from interlis.builder.forward_refs import SymbolTable
from interlis.convert.jsonschema import _is_structure
from interlis.diagnostic_ids import note as _diag
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    _class_related_base_class,
    _role_is_multi,
    attributes_of,
    is_class_compatible,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

from .expressions import _SQL_RELATIONAL_OPERATORS, _numeric_sql_literal, _text_sql_literal
from .identifiers import OID_COLUMN, _quote, _sql_identifier, _truncate_identifier
from .model import SqlView, Table, UniqueViewTrigger
from .tables import _columns_for_class


class _UnsupportedView(Exception):
    """A View shape this module cannot faithfully turn into a `CREATE VIEW` - caught per-View, surfaced as a `-- NOTE`
    (RULE #5), never a crash.

    `rule` is the stable diagnostic id - defaults to the "expression
    outside the translatable subset" family; a "base/target table
    missing" site passes `SQL-VIEW-BASE-MISSING` explicitly.
    """

    def __init__(self, message: str, rule: str = "SQL-VIEW-EXPR-UNTRANSLATABLE") -> None:
        super().__init__(message)
        self.rule = rule


class _ViewResolver:
    """Resolves a View's `RenamedBaseView`/`ClassAttribute`/`Where` paths against the already-built `Table`s.

    Base `Table`s must be part of the same conversion (`--catalog
    <base>.ili`) - a missing one raises `_UnsupportedView` rather than
    emitting a `CREATE VIEW` that would not compile. `assoc_near_roles`
    (from `_resolve_view_bases`) marks a base that is really an embedded
    2-role `ASSOCIATION`'s "near" role - navigating it is a self-reference
    no-op, not a real member lookup.
    """

    def __init__(
        self,
        bases: list[tuple[str, MetaInstance, str]],
        tables_by_name: dict[str, Table],
        symbol_for,
        assoc_near_roles: dict[str, str] | None = None,
    ) -> None:
        self.by_alias = {alias: (cls, table) for alias, cls, table in bases}
        self.tables_by_name = tables_by_name
        self.symbol_for = symbol_for
        self.assoc_near_roles = assoc_near_roles or {}
        self.extra_joins: list[tuple[str, str, str]] = []  # (table, alias, ON-condition SQL)
        self._counter = 0

    def _members(self, cls: MetaInstance) -> dict[str, MetaInstance]:
        st = self.symbol_for(cls)
        return schema_members_of(cls, st) if st is not None else attributes_of(cls)

    def _columns(self, table_name: str) -> set[str]:
        return {c.name for c in self.tables_by_name[table_name].columns}

    def _flattened_struct_column(self, resolved: ResolvedAttribute, col: str, table: str) -> str | None:
        """Resolve a VIEW attribute naming a single-valued STRUCTURE to its ONE flattened column, or `None`.

        `_columns_for_class` flattens a single-valued STRUCTURE inline
        (`"<attr>_<subattr>"`) - a VIEW attribute naming the STRUCTURE
        itself, not a sub-field, only resolves when it flattens to
        EXACTLY one column (else ambiguous which one it means).
        """
        if resolved.type_kind != "Class" or not _is_structure(resolved.type_instance):
            return None
        if bool(getattr(resolved.type_instance, "Abstract", False)):
            return None
        sub_columns, *_ = _columns_for_class(
            resolved.type_instance, self.symbol_for(resolved.type_instance), prefix=f"{col}_", depth=1
        )
        if len(sub_columns) != 1:
            return None
        flattened = sub_columns[0].name
        return flattened if flattened in self._columns(table) else None

    def scalar_ref(self, factor: MetaInstance) -> str:
        """Return `"alias"."column"` for a `PathOrInspFactor`, registering any JOINs its intermediate reference hops
        need.
        """
        if factor._qualified_class.endswith("Constant"):
            return _view_constant_literal(factor)
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView("an expression is not a plain attribute path or constant")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if not refs or refs[0] is None:
            raise _UnsupportedView("empty attribute path")
        alias = refs[0].lower()
        if alias not in self.by_alias:
            raise _UnsupportedView(f"path root {refs[0]!r} is not a base of this view")
        cls, table = self.by_alias[alias]
        cur_alias = alias
        if len(refs) == 1:
            # a bare base reference denotes the object itself -> its identity column
            return f'"{cur_alias}"."{OID_COLUMN}"'
        for i, hop in enumerate(refs[1:], start=1):
            if hop is None:
                raise _UnsupportedView("path element with no name")
            is_last = i == len(refs) - 1
            if hop == self.assoc_near_roles.get(cur_alias):
                # The association is embedded ON this very row - navigating
                # its "near" role is a self-reference, not a real hop.
                if is_last:
                    return f'"{cur_alias}"."{OID_COLUMN}"'
                continue
            attr = self._members(cls).get(hop)
            if attr is None:
                raise _UnsupportedView(f"{hop!r} is not an attribute/role of {getattr(cls, 'Name', None)!r}")
            resolved = resolve_attribute(attr)
            col = _sql_identifier(hop)
            if is_last:
                if col not in self._columns(table):
                    flattened = self._flattened_struct_column(resolved, col, table)
                    if flattened is None:
                        raise _UnsupportedView(f"{hop!r} has no mapped column on table {table!r}")
                    col = flattened
                return f'"{cur_alias}"."{col}"'
            target = reference_target_class(resolved) if resolved.type_kind in ("Class", "ReferenceType") else None
            if target is None:
                raise _UnsupportedView(f"cannot navigate through {hop!r} - not a resolvable reference/role")
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            if target_table not in self.tables_by_name:
                raise _UnsupportedView(
                    f"join target table {target_table!r} not built - pass its model via --repo or --catalog",
                    "SQL-VIEW-BASE-MISSING",
                )
            if col not in self._columns(table):
                raise _UnsupportedView(f"reference {hop!r} has no FK column on table {table!r}")
            self._counter += 1
            new_alias = _truncate_identifier(f"j{self._counter}_{target_table}")
            self.extra_joins.append(
                (target_table, new_alias, f'"{cur_alias}"."{col}" = "{new_alias}"."{OID_COLUMN}"'),
            )
            cls, table, cur_alias = target, target_table, new_alias
        raise _UnsupportedView("unreachable")  # pragma: no cover

    def defined_sql(self, factor: MetaInstance) -> str:
        """Return a SQL boolean for `DEFINED(<base-alias> -> role -> role ...)` - an association-navigation existence
        test.

        The DMAV `*_Gueltig` VIEW idiom: a `WHERE` built only from nested
        `DEFINED()` over association-role paths. Each hop becomes an
        `EXISTS (...)` reading the FK from whichever side carries it. A
        scalar attribute or many-to-many hop demotes the whole VIEW.
        """
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView("DEFINED(...) argument is not a plain association path")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if len(refs) < 2 or refs[0] is None or refs[0].lower() not in self.by_alias:
            raise _UnsupportedView("DEFINED(...) path root is not a base of this view")
        cls, table = self.by_alias[refs[0].lower()]
        return self._defined_step(cls, f'"{refs[0].lower()}"', table, refs[1:])

    def _defined_step(self, cls: MetaInstance, cur_alias: str, cur_table: str, hops: list[str]) -> str:
        hop, rest = hops[0], hops[1:]
        if hop is None:
            raise _UnsupportedView("DEFINED(...) path element has no name")
        if not rest:
            # A final scalar attribute: DEFINED(...->attr) == <attr> IS NOT NULL.
            # The DMAV `*_Gueltig` idiom ends on `GSNachfuehrung.Grundbucheintrag`,
            # a plain XMLDateTime, not another association hop.
            col = _sql_identifier(hop)
            if col in self._columns(cur_table):
                return f'{cur_alias}."{col}" IS NOT NULL'
        target_cls, target_table, fk_on_current, fk_col = self._resolve_association_hop(cls, hop)
        self._counter += 1
        v_quoted = f'"v{self._counter}"'
        join = (
            f'{v_quoted}."{OID_COLUMN}" = {cur_alias}."{fk_col}"'
            if fk_on_current
            else f'{v_quoted}."{fk_col}" = {cur_alias}."{OID_COLUMN}"'
        )
        tail = f" AND {self._defined_step(target_cls, v_quoted, target_table, rest)}" if rest else ""
        return f'EXISTS (SELECT 1 FROM "{target_table}" {v_quoted} WHERE {join}{tail})'

    def _resolve_association_hop(self, cls: MetaInstance, hop: str) -> tuple[MetaInstance, str, bool, str]:
        """Find the 2-role association connecting `cls` to `hop`; return `(target class, target table, fk_on_current,
        fk_column)`.

        FK placement mirrors `xtf.schema.embedded_roles_of` exactly.
        """
        st = self.symbol_for(cls)
        candidates = st.all_registered() if st is not None else []
        for cand in candidates:
            if not isinstance(cand, MetaInstance) or cand._qualified_class.rsplit(".", 1)[-1] != "Class":
                continue
            if getattr(cand, "Kind", None) != "Association":
                continue
            roles = [r for r in (getattr(cand, "Role", None) or []) if isinstance(r, MetaInstance)]
            if len(roles) != 2:
                continue
            role_a, role_b = roles
            tgt_a, tgt_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
            if tgt_a is None or tgt_b is None:
                continue

            def matches_hop(role: MetaInstance, tgt: MetaInstance) -> bool:
                return getattr(role, "Name", None) == hop or getattr(tgt, "Name", None) == hop

            if is_class_compatible(cls, tgt_a) and matches_hop(role_b, tgt_b):
                near_role, far_role, far_tgt = role_a, role_b, tgt_b
            elif is_class_compatible(cls, tgt_b) and matches_hop(role_a, tgt_a):
                near_role, far_role, far_tgt = role_b, role_a, tgt_a
            else:
                continue

            multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
            if multi_a and multi_b:
                raise _UnsupportedView(f"{hop!r}: a many-to-many association has no embedded FK to navigate")
            if multi_a:
                embed_on = tgt_a
            elif multi_b:
                embed_on = tgt_b
            else:
                embed_on = tgt_b
            fk_on_current = is_class_compatible(cls, embed_on)
            fk_col = _sql_identifier((far_role if fk_on_current else near_role).Name or "")
            far_table = _sql_identifier(getattr(far_tgt, "Name", None) or "")
            if far_table not in self.tables_by_name:
                raise _UnsupportedView(
                    f"navigation target table {far_table!r} not built - pass its model via --repo or --catalog",
                    "SQL-VIEW-BASE-MISSING",
                )
            return far_tgt, far_table, fk_on_current, fk_col
        raise _UnsupportedView(
            f"cannot navigate {hop!r} from {getattr(cls, 'Name', None)!r} - no 2-role association found"
        )


def _view_constant_literal(node: MetaInstance) -> str:
    value, type_ = getattr(node, "Value", None), getattr(node, "Type", None)
    if type_ == "Numeric":
        return _numeric_sql_literal(value)
    if type_ == "Text":
        return _text_sql_literal(value)
    if type_ == "Enumeration":
        return "'" + value.replace("'", "''") + "'"
    raise _UnsupportedView(f"constant of type {type_!r} is not supported in a view expression")


def _view_where_conjuncts(expr: MetaInstance | None, resolver: _ViewResolver) -> list[str]:
    """Flatten a View's `Where` `Expression` tree into a list of SQL boolean strings (implicitly AND-ed)."""
    if expr is None:
        return []
    qname = expr._qualified_class.rsplit(".", 1)[-1]
    op = getattr(expr, "Operation", None)
    if qname == "CompoundExpr" and op == "And":
        out: list[str] = []
        for sub in getattr(expr, "SubExpressions", None) or []:
            out.extend(_view_where_conjuncts(sub, resolver))
        return out
    return [_view_where_sql(expr, resolver)]


def _view_where_sql(expr: MetaInstance, resolver: _ViewResolver) -> str:
    """One View `Where` sub-expression as a parenthesised SQL boolean.

    Same supported subset as `constraint_eval.py`/`jsonfg.py`'s
    `evaluate_view`: relational comparison, `And`/`Or`/`Not`, `DEFINED()`.
    Anything else (a function call, arithmetic) demotes the whole VIEW.
    """
    qname = expr._qualified_class.rsplit(".", 1)[-1]
    op = getattr(expr, "Operation", None)
    if qname == "CompoundExpr":
        subs = list(getattr(expr, "SubExpressions", None) or [])
        if op == "And":
            return "(" + " AND ".join(_view_where_sql(s, resolver) for s in subs) + ")"
        if op == "Or":
            return "(" + " OR ".join(_view_where_sql(s, resolver) for s in subs) + ")"
        if op in _SQL_RELATIONAL_OPERATORS and len(subs) == 2:
            return f"({resolver.scalar_ref(subs[0])} {_SQL_RELATIONAL_OPERATORS[op]} {resolver.scalar_ref(subs[1])})"
    if qname == "UnaryExpr":
        sub = getattr(expr, "SubExpression", None)
        if op == "Not" and sub is not None:
            return f"(NOT {_view_where_sql(sub, resolver)})"
        if op == "Defined" and sub is not None:
            return f"({resolver.defined_sql(sub)})"
    raise _UnsupportedView(f"WHERE operation {op!r} ({qname}) is not translatable to a SQL view predicate")


def build_views(
    views: list[MetaInstance],
    tables: list[Table],
    *,
    symbol_table: SymbolTable | None = None,
    class_symbol_tables: dict[int, SymbolTable] | None = None,
    class_table_names: dict[int, str] | None = None,
) -> list[SqlView]:
    """Translate each `View` (`FormationKind` Projection/Join only) into a `CREATE VIEW` body, or a `-- NOTE` when it
    can't be done faithfully.

    Becomes `SELECT <attr := path> ... FROM <base tables, comma-joined>
    WHERE <predicates>`. Base classes must be among `tables` (`--catalog`).
    Anything outside the translatable subset demotes the WHOLE view to
    `body=None` with a note (RULE #5), never a half-built `CREATE VIEW`.
    """
    tables_by_name = {t.name: t for t in tables}
    table_name_by_class_id = class_table_names or {}

    def symbol_for(cls: MetaInstance) -> SymbolTable | None:
        return (class_symbol_tables or {}).get(id(cls), symbol_table)

    result: list[SqlView] = []
    used_names: set[str] = {t.name for t in tables}
    for view in views:
        vname = _truncate_identifier(_sql_identifier(getattr(view, "Name", None) or ""))
        while vname in used_names:
            vname = _truncate_identifier(f"{vname}_v")
        used_names.add(vname)
        try:
            bases, assoc_near_roles = _resolve_view_bases(view, tables_by_name, table_name_by_class_id)
            formation = getattr(view, "FormationKind", None)
            if formation == "Union":
                body = _build_union_view(view, bases, tables_by_name, symbol_for, assoc_near_roles)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            if formation == "Inspection":
                body = _build_inspection_view(view, bases, tables_by_name, symbol_for)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            if formation == "Aggregation":
                body = _build_aggregation_view(view, bases, tables_by_name, symbol_for, assoc_near_roles)
                result.append(SqlView(vname, body, _view_constraint_notes(view)))
                continue
            resolver = _ViewResolver(bases, tables_by_name, symbol_for, assoc_near_roles)
            select_items: list[str] = []
            notes: list[str] = []
            attr_col: dict[str, str] = {}
            for attr in getattr(view, "ClassAttribute", None) or []:
                aname = getattr(attr, "Name", None)
                derivates = getattr(attr, "Derivates", None) or []
                if not derivates:
                    raise _UnsupportedView(f"view attribute {aname!r} has no assigned expression")
                out_col = _sql_identifier(aname or "")
                try:
                    col_ref = resolver.scalar_ref(derivates[0])
                    select_items.append(f'{col_ref} AS "{out_col}"')
                    attr_col[(aname or "").lower()] = col_ref
                except _UnsupportedView as exc:
                    # An `ALL OF` pass-through re-exports every base attribute; one it
                    # cannot project as a single column (a STRUCTURE, an unmapped type)
                    # is dropped with a note. An explicit `Name := expression` was asked
                    # for by name and still fails the whole VIEW.
                    if not getattr(derivates[0], "_all_of_identity", False):
                        raise
                    notes.append(_diag("SQL-VIEW-ATTR-DROPPED", f"attribute {aname!r} not in the CREATE VIEW: {exc}"))
            if not select_items:
                raise _UnsupportedView("view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
            where = _view_where_conjuncts(getattr(view, "Where", None), resolver)
            if len(bases) > 1 and getattr(view, "Where", None) is None:
                where += _auto_join_conditions(bases, symbol_for)
            constraint_notes, triggers = _view_unique_constraint_ddl(
                view, vname, bases, tables_by_name, attr_col, where, extra_joins_present=bool(resolver.extra_joins)
            )
            from_parts = [f'"{table}" "{alias}"' for alias, _cls, table in bases]
            from_parts += [f'"{table}" "{alias}"' for table, alias, _on in resolver.extra_joins]
            where += [on for _t, _a, on in resolver.extra_joins]
            body = "SELECT\n    " + ",\n    ".join(select_items) + "\nFROM " + ", ".join(from_parts)
            if where:
                body += "\nWHERE " + "\n  AND ".join(where)
            notes.extend(constraint_notes)
            result.append(SqlView(vname, body, notes, triggers))
        except _UnsupportedView as exc:
            result.append(SqlView(vname, None, [_diag(exc.rule, str(exc))]))
    return result


def _build_union_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
    assoc_near_roles: dict[str, str],
) -> str:
    """Return a `SELECT ... UNION ALL SELECT ...` body for a `FormationKind=Union` view.

    Each `ClassAttribute` carries one `Derivates` entry per base; branch
    `i` projects `Derivates[i]` from base `i` alone. `UNION ALL`, not
    `UNION` - INTERLIS union is a merge, not a set operation. Each branch
    resolves independently, so one branch's join never leaks into
    another's.
    """
    attrs = getattr(view, "ClassAttribute", None) or []
    if not attrs:
        raise _UnsupportedView("union view has no ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    branches: list[str] = []
    for branch_index, (alias, cls, table) in enumerate(bases):
        resolver = _ViewResolver([(alias, cls, table)], tables_by_name, symbol_for, assoc_near_roles)
        items: list[str] = []
        for attr in attrs:
            aname = getattr(attr, "Name", None)
            derivates = getattr(attr, "Derivates", None) or []
            if len(derivates) != len(bases):
                raise _UnsupportedView(
                    f"union attribute {aname!r}: {len(derivates)} assigned expression(s) for {len(bases)} bases"
                )
            items.append(f'{resolver.scalar_ref(derivates[branch_index])} AS "{_sql_identifier(aname or "")}"')
        from_parts = [f'"{table}" "{alias}"']
        from_parts += [f'"{t}" "{a}"' for t, a, _on in resolver.extra_joins]
        branch = "SELECT\n    " + ",\n    ".join(items) + "\nFROM " + ", ".join(from_parts)
        join_conditions = [on for _t, _a, on in resolver.extra_joins]
        if join_conditions:
            branch += "\nWHERE " + "\n  AND ".join(join_conditions)
        branches.append(branch)
    return "\nUNION ALL\n".join(branches)


def _build_inspection_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
) -> str:
    """Return a `SELECT ... FROM "<parent>_<attr>[_<sub-attr>]"` body for a `FormationKind=Inspection` view.

    `INSPECTION OF <base> -> attr` yields every element of the inspected
    `BAG`/`LIST OF`; `build_tables` already emits that extent as the
    child table `<base_table>_<attr>`, so this is a projection over it
    (`out := PARENT -> field` joins back on the child's own `_fk` column,
    single-hop only). A SURFACE/AREA geometry inspection has no child
    table but IS translated (`_build_geometry_inspection_view`); a
    LINE/POLYLINE geometry or a deeper path demotes.
    """
    if len(bases) != 1:
        raise _UnsupportedView("an inspection view has exactly one base", "SQL-VIEW-FORMATION-UNSUPPORTED")
    base_alias, base_cls, base_table = bases[0]
    path = list(getattr(view, "_inspection_path", None) or [])
    if not path:
        raise _UnsupportedView(
            "INSPECTION path (the '-> attribute' chain) was not built - InterlisModelBuilder gap",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    child_table = base_table
    for hop in path:
        candidate = _sql_identifier(f"{child_table}_{hop}")
        if candidate in tables_by_name:
            child_table = candidate
            continue
        if child_table == base_table and len(path) == 1:
            geom_col = _geometry_inspection_column(base_cls, hop, base_table, tables_by_name, symbol_for)
            if geom_col is not None:
                return _build_geometry_inspection_view(view, base_alias, base_table, hop, geom_col)
        raise _UnsupportedView(
            f"the inspected attribute {' -> '.join(path)!r} has no child table "
            f"(a geometry inspection over a SURFACE/AREA attribute decomposes to its own boundary "
            f"instead - see _build_geometry_inspection_view - a LINE/POLYLINE geometry or a path "
            f"nesting more than one level deep still has no child table at all)",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    columns = {c.name for c in tables_by_name[child_table].columns}
    base_columns = {c.name for c in tables_by_name[base_table].columns} if base_table in tables_by_name else set()
    fk_col = _sql_identifier(f"{base_table}_fk")
    elem_alias = "insp"
    select_items: list[str] = []
    needs_parent_join = False
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"inspection view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView(f"inspection view attribute {aname!r} is not a plain element path")
        path_els = getattr(factor, "PathEls", None) or []
        refs = [getattr(el, "Ref", None) for el in path_els]
        if len(refs) == 2 and (refs[0] or "").lower() == base_alias and refs[1] is not None:
            col = _sql_identifier(refs[1])
            if col not in columns:
                raise _UnsupportedView(f"element attribute {refs[1]!r} has no column on {child_table!r}")
            select_items.append(f'"{elem_alias}"."{col}" AS "{_sql_identifier(aname or "")}"')
        elif len(path_els) == 2 and getattr(path_els[0], "Kind", None) == "Parent" and refs[1] is not None:
            if len(path) > 1:
                raise _UnsupportedView(
                    "an inspection view attribute navigates PARENT-> on a multi-hop path - the child table's "
                    "FK points at the intermediate table, not the base table",
                    "SQL-VIEW-FORMATION-UNSUPPORTED",
                )
            if fk_col not in columns:
                raise _UnsupportedView(
                    f"the inspected element table {child_table!r} has no {fk_col!r} column to join back to "
                    f"{base_table!r}",
                    "SQL-VIEW-FORMATION-UNSUPPORTED",
                )
            col = _sql_identifier(refs[1])
            if col not in base_columns:
                raise _UnsupportedView(f"PARENT-> attribute {refs[1]!r} has no column on {base_table!r}")
            select_items.append(f'"{base_alias}"."{col}" AS "{_sql_identifier(aname or "")}"')
            needs_parent_join = True
        else:
            raise _UnsupportedView(f"inspection view attribute {aname!r}: unsupported element path {refs}")
    if not select_items:
        raise _UnsupportedView("inspection view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    from_clause = f'FROM "{child_table}" "{elem_alias}"'
    if needs_parent_join:
        from_clause += (
            f'\nJOIN "{base_table}" "{base_alias}" ON "{elem_alias}"."{fk_col}" = "{base_alias}"."{OID_COLUMN}"'
        )
    return "SELECT\n    " + ",\n    ".join(select_items) + f"\n{from_clause}"


def _geometry_inspection_column(
    base_cls: MetaInstance,
    attr_name: str,
    base_table: str,
    tables_by_name: dict[str, Table],
    symbol_for,
) -> str | None:
    """Return `attr_name`'s SQL column name on `base_table` if it's a single-valued SURFACE/AREA-`Kind` geometry
    attribute, else `None`.

    `Kind in ("Surface", "Area")` only - a `Polyline`/`DirectedPolyline`
    has a different decomposition, not covered here.
    """
    st = symbol_for(base_cls)
    members = schema_members_of(base_cls, st) if st is not None else attributes_of(base_cls)
    attr = members.get(attr_name)
    if attr is None:
        return None
    resolved = resolve_attribute(attr)
    if resolved.type_kind != "LineType" or getattr(resolved.type_instance, "Kind", None) not in ("Surface", "Area"):
        return None
    col = _sql_identifier(attr_name)
    if base_table not in tables_by_name or col not in {c.name for c in tables_by_name[base_table].columns}:
        return None
    return col


def _build_geometry_inspection_view(
    view: MetaInstance,
    base_alias: str,
    base_table: str,
    geom_attr_name: str,
    geom_col: str,
) -> str:
    """Return a `SELECT ST_Boundary(...) FROM "<base>"` body for a single-hop geometry `INSPECTION`.

    Decomposed as ONE row per base object whose geometry IS that boundary
    (`ST_Boundary`, OGC SFA/PostGIS) - the pragmatic reading a `GRAPHIC
    ... BASED ON` an inspection view needs, not the full nested
    `Lines`/`SurfaceEdge` structure (out of scope, eCH-0031 itself treats
    that as a separate conformance level). Only reading the SAME
    inspected attribute back is translatable.
    """
    select_items: list[str] = []
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"inspection view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        if not factor._qualified_class.endswith("PathOrInspFactor") or getattr(factor, "Inspection", None):
            raise _UnsupportedView(f"inspection view attribute {aname!r} is not a plain element path")
        refs = [getattr(el, "Ref", None) for el in (getattr(factor, "PathEls", None) or [])]
        if len(refs) == 2 and (refs[0] or "").lower() == base_alias and refs[1] == geom_attr_name:
            select_items.append(f'ST_Boundary("{base_alias}"."{geom_col}") AS "{_sql_identifier(aname or "")}"')
        else:
            raise _UnsupportedView(
                f"geometry inspection view attribute {aname!r}: only the inspected geometry's own "
                f"boundary ({geom_attr_name!r}) is selectable - no further sub-structure is translated"
            )
    if not select_items:
        raise _UnsupportedView("inspection view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    return "SELECT\n    " + ",\n    ".join(select_items) + f'\nFROM "{base_table}" "{base_alias}"'


def _build_aggregation_view(
    view: MetaInstance,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    symbol_for,
    assoc_near_roles: dict[str, str],
) -> str:
    """Return a `SELECT [DISTINCT] ... FROM "<base>" [GROUP BY ...]` body for a `FormationKind=Aggregation` view,
    or demote.

    `AGGREGATION OF <base> (ALL | EQUAL(key))` collapses base objects into
    one instance; a user FUNCTION over the implicit `AGGREGATES` bag
    demotes the view, EXCEPT `INTERLIS.objectCount`/`elementCount` on the
    bag itself, which become `COUNT(*)`. `EQUAL(key)` adds a real
    `GROUP BY`; `ALL` with an aggregate AND a plain column has no
    well-defined single value and demotes too.
    """
    if len(bases) != 1:
        raise _UnsupportedView("an aggregation view has exactly one base", "SQL-VIEW-FORMATION-UNSUPPORTED")
    resolver = _ViewResolver(bases, tables_by_name, symbol_for, assoc_near_roles)
    key_factor = getattr(view, "_aggregation_key", None)
    group_by: list[str] = []
    if key_factor is not None:
        group_by.append(resolver.scalar_ref(key_factor))
    select_items: list[str] = []
    has_aggregate = False
    has_plain = False
    for attr in getattr(view, "ClassAttribute", None) or []:
        aname = getattr(attr, "Name", None)
        derivates = getattr(attr, "Derivates", None) or []
        if not derivates:
            raise _UnsupportedView(f"aggregation view attribute {aname!r} has no assigned expression")
        factor = derivates[0]
        out_col = _sql_identifier(aname or "")
        if factor._qualified_class.endswith("FunctionCall"):
            select_items.append(f'{_standard_aggregate_function_sql(factor, aname)} AS "{out_col}"')
            has_aggregate = True
            continue
        if not factor._qualified_class.endswith(("PathOrInspFactor", "Constant")):
            raise _UnsupportedView(
                f"aggregation view attribute {aname!r} is a function/expression over the implicit AGGREGATES bag - "
                f"a user FUNCTION body is not translated to SQL",
                "SQL-VIEW-FORMATION-UNSUPPORTED",
            )
        expr = resolver.scalar_ref(factor)
        select_items.append(f'{expr} AS "{out_col}"')
        has_plain = True
        if key_factor is not None and expr not in group_by:
            group_by.append(expr)
    if not select_items:
        raise _UnsupportedView("aggregation view has no projectable ATTRIBUTE definitions", "SQL-VIEW-NO-ATTRS")
    if key_factor is None and has_aggregate and has_plain:
        raise _UnsupportedView(
            "an ALL aggregation combines a FUNCTION over AGGREGATES with a plain attribute - "
            "no EQUAL(...) grouping key to make that combination well-defined",
            "SQL-VIEW-FORMATION-UNSUPPORTED",
        )
    _alias, _cls, table = bases[0]
    from_parts = [f'"{table}" "{_alias}"']
    from_parts += [f'"{t}" "{a}"' for t, a, _on in resolver.extra_joins]
    verb = "SELECT" if key_factor is not None or has_aggregate else "SELECT DISTINCT"
    body = f"{verb}\n    " + ",\n    ".join(select_items) + "\nFROM " + ", ".join(from_parts)
    join_conditions = [on for _t, _a, on in resolver.extra_joins]
    if join_conditions:
        body += "\nWHERE " + "\n  AND ".join(join_conditions)
    if key_factor is not None:
        body += "\nGROUP BY " + ", ".join(group_by)
    return body


_STANDARD_AGGREGATE_COUNT_FUNCTIONS = {"INTERLIS.objectCount", "INTERLIS.elementCount"}


def _is_aggregates_marker(expr: MetaInstance) -> bool:
    """True for the bare `AGGREGATES` argument (no `Ref`, unlike a real attribute)."""
    if not expr._qualified_class.endswith("PathOrInspFactor"):
        return False
    path_els = getattr(expr, "PathEls", None) or []
    return (
        len(path_els) == 1
        and getattr(path_els[0], "Kind", None) == "Attribute"
        and getattr(path_els[0], "Ref", None) is None
    )


def _standard_aggregate_function_sql(factor: MetaInstance, aname: str | None) -> str:
    """Return `COUNT(*)` for `INTERLIS.objectCount(AGGREGATES)`/`elementCount(AGGREGATES)`, or demote.

    Refman signature ("count of objects/elements") is exactly `COUNT(*)`
    applied to the grouped bag itself - any other function, or these two
    applied to anything but the bare `AGGREGATES` argument, demotes.
    """
    func_name = getattr(factor, "Function", None)
    args = getattr(factor, "Arguments", None) or []
    arg_expr = getattr(args[0], "Expression", None) if len(args) == 1 else None
    if func_name in _STANDARD_AGGREGATE_COUNT_FUNCTIONS and arg_expr is not None and _is_aggregates_marker(arg_expr):
        return "COUNT(*)"
    raise _UnsupportedView(
        f"aggregation view attribute {aname!r} is a function/expression over the implicit AGGREGATES bag - "
        f"only INTERLIS.objectCount(AGGREGATES)/elementCount(AGGREGATES) are translated to SQL, "
        f"a user FUNCTION body is not",
        "SQL-VIEW-FORMATION-UNSUPPORTED",
    )


def _view_constraint_notes(view: MetaInstance) -> list[str]:
    """Return a `-- NOTE` per VIEW-level `UNIQUE` / `SET` / `EXISTENCE` constraint - a `CREATE VIEW` cannot carry them.

    Neither is expressible on a SQL view - surfaced here rather than
    dropped silently (RULE #5); enforce downstream.
    """
    notes: list[str] = []
    for constraint in getattr(view, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname == "UniqueConstraint":
            cols = [
                getattr(pe, "Ref", None)
                for factor in getattr(constraint, "UniqueDef", None) or []
                for pe in getattr(factor, "PathEls", None) or []
            ]
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level UNIQUE {label} ({', '.join(c for c in cols if c)}) - a CREATE VIEW cannot enforce it",
                )
            )
        elif qname in ("SetConstraint", "ExistenceConstraint"):
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level {qname} {label} - a whole-population check no CREATE VIEW can carry",
                )
            )
        else:
            notes.append(
                _diag(
                    "SQL-VIEW-CONSTRAINT-DROPPED",
                    f"VIEW-level CONSTRAINT {label} ({qname}) - not carried onto the CREATE VIEW",
                )
            )
    return notes


def _view_unique_constraint_ddl(
    view: MetaInstance,
    vname: str,
    bases: list[tuple[str, MetaInstance, str]],
    tables_by_name: dict[str, Table],
    attr_col: dict[str, str],
    where: list[str],
    *,
    extra_joins_present: bool,
) -> tuple[list[str], list[UniqueViewTrigger]]:
    """Return `(notes, triggers)` for every VIEW-level `Constraint` of a PROJECTION/JOIN view (`Union`/`Aggregation`/
    `Inspection` still go through `_view_constraint_notes` unchanged - no real corpus case combines them with a
    view-level `UNIQUE`).

    A `UniqueConstraint` whose key is a plain view attribute on a
    SINGLE-base view with no `extra_joins` becomes a real
    `UniqueViewTrigger` instead of a dropped note (real corpus case: DMAV
    `*_Gueltig`). A geometry-typed key stays a note - SQL `=` is
    bounding-box equality on PostGIS `geometry`, not exact equality.
    """
    notes: list[str] = []
    triggers: list[UniqueViewTrigger] = []
    single_base = bases[0] if len(bases) == 1 and not extra_joins_present else None
    for constraint in getattr(view, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname != "UniqueConstraint":
            reason = (
                "a whole-population check no CREATE VIEW/TRIGGER can carry"
                if qname in ("SetConstraint", "ExistenceConstraint")
                else f"not carried onto the CREATE VIEW ({qname})"
            )
            notes.append(_diag("SQL-VIEW-CONSTRAINT-DROPPED", f"VIEW-level {qname} {label} - {reason}"))
            continue
        cols = [
            getattr(pe, "Ref", None)
            for factor in getattr(constraint, "UniqueDef", None) or []
            for pe in getattr(factor, "PathEls", None) or []
        ]
        trigger: UniqueViewTrigger | None = None
        if single_base is not None:
            alias, _cls, table = single_base
            table_columns = {c.name: c for c in tables_by_name[table].columns}
            columns: list[str] = []
            ok = True
            for factor in getattr(constraint, "UniqueDef", None) or []:
                pathels = getattr(factor, "PathEls", None) or []
                col = _sql_identifier(pathels[0].Ref or "") if len(pathels) == 1 else None
                sql_expr = attr_col.get((pathels[0].Ref or "").lower()) if len(pathels) == 1 else None
                if col is None or sql_expr != f'"{alias}"."{col}"' or table_columns.get(col) is None:
                    ok = False
                    break
                if table_columns[col].geometry_type is not None:
                    ok = False
                    break
                columns.append(col)
            if ok and columns:
                trigger = UniqueViewTrigger(vname, label.strip("'"), table, alias, columns, list(where))
        if trigger is not None:
            triggers.append(trigger)
            continue
        notes.append(
            _diag(
                "SQL-VIEW-CONSTRAINT-DROPPED",
                f"VIEW-level UNIQUE {label} ({', '.join(c for c in cols if c)}) - a CREATE VIEW cannot enforce it, "
                "and it is outside the single-base/plain-column subset a BEFORE INSERT/UPDATE trigger can",
            )
        )
    return notes, triggers


def _association_embedding(assoc_cls: MetaInstance) -> tuple[MetaInstance, str, str] | None:
    """Return `(carrier_class, near_role_name, far_role_name)` for a 2-role embedded `ASSOCIATION`.

    Mirrors `xtf.schema.embedded_roles_of`, but from the association's own
    perspective. `near_role_name`'s own target class IS the carrier (a
    self-reference when navigated from the association). `None` for a
    many-to-many association (not embedded) or a non-2-role one.
    """
    roles = [r for r in getattr(assoc_cls, "Role", None) or [] if isinstance(r, MetaInstance)]
    if len(roles) != 2:
        return None
    role_a, role_b = roles
    target_a, target_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
    if target_a is None or target_b is None:
        return None
    multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
    if multi_a and multi_b:
        return None
    carrier, near_role, far_role = (target_a, role_a, role_b) if multi_a else (target_b, role_b, role_a)
    near_name, far_name = getattr(near_role, "Name", None), getattr(far_role, "Name", None)
    if not near_name or not far_name:
        return None
    return carrier, near_name, far_name


def _direct_association_join(
    cls_a: MetaInstance, cls_b: MetaInstance, symbol_table: SymbolTable | None
) -> tuple[bool, str] | None:
    """Find the (unique) 2-role association directly linking `cls_a` and `cls_b`.

    Returns `(fk_on_a, fk_col)` (True = `cls_a`'s table carries the FK).
    `None` for no such association, a many-to-many one, or 2+ candidates
    that disagree (ambiguous, left to an explicit `WHERE`).
    """
    if symbol_table is None:
        return None
    result: tuple[bool, str] | None = None
    for cand in symbol_table.all_registered():
        if not isinstance(cand, MetaInstance) or cand._qualified_class.rsplit(".", 1)[-1] != "Class":
            continue
        if getattr(cand, "Kind", None) != "Association":
            continue
        roles = [r for r in (getattr(cand, "Role", None) or []) if isinstance(r, MetaInstance)]
        if len(roles) != 2:
            continue
        role_a, role_b = roles
        tgt_a, tgt_b = _class_related_base_class(role_a), _class_related_base_class(role_b)
        if tgt_a is None or tgt_b is None:
            continue
        if is_class_compatible(cls_a, tgt_a) and is_class_compatible(cls_b, tgt_b):
            a_target = tgt_a
        elif is_class_compatible(cls_a, tgt_b) and is_class_compatible(cls_b, tgt_a):
            a_target = tgt_b
        else:
            continue
        multi_a, multi_b = _role_is_multi(role_a), _role_is_multi(role_b)
        if multi_a and multi_b:
            continue
        embed_on, fk_role = (tgt_a, role_b) if multi_a else ((tgt_b, role_a) if multi_b else (tgt_b, role_a))
        fk_col = _sql_identifier(getattr(fk_role, "Name", None) or "")
        if not fk_col:
            continue
        candidate = (embed_on is a_target, fk_col)
        if result is not None and result != candidate:
            return None
        result = candidate
    return result


def _auto_join_conditions(
    bases: list[tuple[str, MetaInstance, str]],
    symbol_for,
) -> list[str]:
    """Derive `WHERE` join predicates connecting every JOIN OF base, for a `Where`-less multi-base VIEW.

    Real corpus `JOIN OF A, B;` with no `WHERE` relies on the classes
    being linked by their OWN association (confirmed against `ili2c`).
    Builds a spanning tree over `bases`; a base with no direct association
    to the rest can't join without risking a Cartesian product and
    demotes the whole VIEW instead.
    """
    connected = {bases[0][0]}
    conditions: list[str] = []
    remaining = list(bases[1:])
    progress = True
    while remaining and progress:
        progress = False
        for alias, cls, _table in list(remaining):
            for other_alias, other_cls, _other_table in bases:
                if other_alias not in connected or other_alias == alias:
                    continue
                link = _direct_association_join(cls, other_cls, symbol_for(cls)) or _direct_association_join(
                    cls, other_cls, symbol_for(other_cls)
                )
                if link is None:
                    continue
                fk_on_current, fk_col = link
                conditions.append(
                    f'"{alias}"."{fk_col}" = "{other_alias}"."{OID_COLUMN}"'
                    if fk_on_current
                    else f'"{other_alias}"."{fk_col}" = "{alias}"."{OID_COLUMN}"'
                )
                connected.add(alias)
                remaining.remove((alias, cls, _table))
                progress = True
                break
            if progress:
                break
    if remaining:
        names = ", ".join(getattr(cls, "Name", None) or "?" for _a, cls, _t in remaining)
        raise _UnsupportedView(
            f"JOIN OF has no WHERE and {names} has no direct association linking it to the other base(s) - "
            "cannot derive a join condition without risking a Cartesian product",
            "SQL-VIEW-JOIN-UNLINKED",
        )
    return conditions


def _resolve_view_bases(
    view: MetaInstance,
    tables_by_name: dict[str, Table],
    table_name_by_class_id: dict[int, str],
) -> tuple[list[tuple[str, MetaInstance, str]], dict[str, str]]:
    """Return `(bases, assoc_near_roles)` - `assoc_near_roles` (`alias -> role name`) for `_ViewResolver`, see its
    own docstring.

    A base that is `Kind=Association` has no `CREATE TABLE` of its own -
    resolved instead to its 2-role embedding's CARRIER class/table
    (`_association_embedding`), the real corpus shape for `PROJECTION OF
    <association>`.
    """
    bases: list[tuple[str, MetaInstance, str]] = []
    assoc_near_roles: dict[str, str] = {}
    used_aliases: set[str] = set()
    for rbv in getattr(view, "RenamedBaseView", None) or []:
        base_cls = getattr(rbv, "BaseView", None)
        if not isinstance(base_cls, MetaInstance):
            raise _UnsupportedView(
                "a base class did not resolve - pass --repo for the base model's own imports", "SQL-VIEW-BASE-MISSING"
            )
        resolved_cls = base_cls
        near_role_name: str | None = None
        if getattr(base_cls, "Kind", None) == "Association":
            embedding = _association_embedding(base_cls)
            if embedding is None:
                raise _UnsupportedView(
                    f"{getattr(base_cls, 'Name', '?')!r} is an ASSOCIATION with no 2-role embedding "
                    "(many-to-many, or fewer/more than 2 roles) - not represented by a table",
                    "SQL-VIEW-BASE-MISSING",
                )
            resolved_cls, near_role_name, _far_role_name = embedding
        table = table_name_by_class_id.get(id(resolved_cls)) or _sql_identifier(
            getattr(resolved_cls, "Name", None) or ""
        )
        if table not in tables_by_name:
            raise _UnsupportedView(
                f"base table {table!r} not built - pass "
                f"{getattr(resolved_cls, 'Name', '?')}'s model via --repo or --catalog",
                "SQL-VIEW-BASE-MISSING",
            )
        alias = (getattr(rbv, "Name", None) or getattr(base_cls, "Name", None) or "").lower()
        base_alias = alias
        suffix = 2
        while alias in used_aliases:
            alias = f"{base_alias}_{suffix}"
            suffix += 1
        used_aliases.add(alias)
        bases.append((alias, resolved_cls, table))
        if near_role_name is not None:
            assoc_near_roles[alias] = near_role_name
    if not bases:
        raise _UnsupportedView(
            "no resolved base classes - pass the base model via --repo or --catalog", "SQL-VIEW-BASE-MISSING"
        )
    return bases, assoc_near_roles


def _render_views(views: tuple[SqlView, ...]) -> list[str]:
    statements: list[str] = []
    for view in views:
        for note in view.notes:
            statements.append(f"-- NOTE (view {view.name}): {note}")
        if view.body is None:
            continue
        indented = view.body.replace("\n", "\n    ")
        statements.append(f'CREATE VIEW "{view.name}" AS\n    {indented};')
    return statements


def _view_unique_trigger_predicate(trig: UniqueViewTrigger, new_ref: str) -> tuple[str, str]:
    """Return `(new_is_in_the_view, a_duplicate_exists)` SQL booleans for `trig` - `new_ref` is the trigger row
    reference (`"NEW"` in both dialects).

    `new_is_in_the_view` reapplies the view's own `WHERE` to the row being
    written - without it, a not-yet-valid row would be wrongly rejected
    just for sharing a key with an already-valid one. `a_duplicate_exists`
    re-queries the base table (simpler than the `CREATE VIEW` itself).
    """
    substituted = [w.replace(f'"{trig.alias}".', f"{new_ref}.") for w in trig.where]
    not_null = " AND ".join(f'{new_ref}."{c}" IS NOT NULL' for c in trig.columns)
    new_is_in_the_view = " AND ".join([f"({not_null})", *substituted])
    key_match = " AND ".join(f'"{trig.alias}"."{c}" = {new_ref}."{c}"' for c in trig.columns)
    conditions = [f'"{trig.alias}"."{OID_COLUMN}" <> {new_ref}."{OID_COLUMN}"', key_match, *trig.where]
    duplicate_exists = (
        f'EXISTS (SELECT 1 FROM "{trig.base_table}" "{trig.alias}" WHERE ' + " AND ".join(conditions) + ")"
    )
    return new_is_in_the_view, duplicate_exists


def _view_unique_trigger_message(trig: UniqueViewTrigger) -> str:
    return f'view "{trig.view_name}": UNIQUE {trig.label} ({", ".join(trig.columns)}) violated'.replace("'", "''")


def _render_view_unique_triggers_postgresql(views: tuple[SqlView, ...]) -> list[str]:
    """Render each `SqlView.triggers` entry as a PL/pgSQL trigger function + `CREATE TRIGGER`.

    A single `BEFORE INSERT OR UPDATE` trigger covers both events -
    PostgreSQL, unlike SQLite, allows combining them in one `CREATE
    TRIGGER`.
    """
    statements: list[str] = []
    for view in views:
        for trig in view.triggers:
            new_ok, duplicate_exists = _view_unique_trigger_predicate(trig, "NEW")
            base = _truncate_identifier(_sql_identifier(f"uq_{trig.view_name}_{trig.label}"))
            fn_name, trg_name = f"{base}_check", f"{base}_trg"
            statements.append(
                f"CREATE OR REPLACE FUNCTION {_quote(fn_name)}() RETURNS trigger AS $$\n"
                "BEGIN\n"
                f"    IF ({new_ok}) AND {duplicate_exists} THEN\n"
                f"        RAISE EXCEPTION '{_view_unique_trigger_message(trig)}';\n"
                "    END IF;\n"
                "    RETURN NEW;\n"
                "END;\n"
                "$$ LANGUAGE plpgsql;"
            )
            statements.append(
                f"CREATE TRIGGER {_quote(trg_name)} BEFORE INSERT OR UPDATE ON {_quote(trig.base_table)}\n"
                f"    FOR EACH ROW EXECUTE FUNCTION {_quote(fn_name)}();"
            )
    return statements


def _render_view_unique_triggers_gpkg(views: tuple[SqlView, ...]) -> list[str]:
    """Render each `SqlView.triggers` entry as two SQLite `CREATE TRIGGER` statements (INSERT + UPDATE).

    SQLite's trigger event is singular (`INSERT`/`UPDATE`/`DELETE`) -
    unlike PostgreSQL's `BEFORE INSERT OR UPDATE`, it cannot be combined
    into one `CREATE TRIGGER`.
    """
    statements: list[str] = []
    for view in views:
        for trig in view.triggers:
            new_ok, duplicate_exists = _view_unique_trigger_predicate(trig, "NEW")
            base = _truncate_identifier(_sql_identifier(f"uq_{trig.view_name}_{trig.label}"))
            message = _view_unique_trigger_message(trig)
            for event in ("INSERT", "UPDATE"):
                trg_name = _truncate_identifier(f"{base}_{event.lower()}")
                statements.append(
                    f"CREATE TRIGGER {_quote(trg_name)}\n"
                    f"BEFORE {event} ON {_quote(trig.base_table)}\n"
                    f"WHEN ({new_ok}) AND {duplicate_exists}\n"
                    "BEGIN\n"
                    f"    SELECT RAISE(ABORT, '{message}');\n"
                    "END;"
                )
    return statements
