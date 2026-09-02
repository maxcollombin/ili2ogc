"""IlisMeta16 `Class` -> SQL `Table`: columns, `BAG`/`LIST OF` child tables, ABSTRACT structure subclass tables,
UNIQUE/CHECK/FOREIGN KEY constraints.

`build_tables` is the public entry point - see its own docstring and
`mappings/ilismeta16-to-sql-rules.yml` for the full per-concept scope.
"""

from __future__ import annotations

from interlis.builder.forward_refs import SymbolTable
from interlis.convert.jsonschema import _is_structure
from interlis.diagnostic_ids import note as _diag
from interlis.metamodel.instance import MetaInstance
from interlis.xtf.schema import (
    ResolvedAttribute,
    attributes_of,
    concrete_structure_subclasses,
    reference_target_class,
    resolve_attribute,
    schema_members_of,
)

from .expressions import _MAX_STRUCT_FLATTEN_DEPTH, _expression_to_sql, _UnsupportedCheckExpression
from .identifiers import OID_COLUMN, _dedup_name, _sql_identifier, _truncate_identifier
from .model import CheckConstraint, Column, ForeignKey, Table, UniqueConstraint
from .types import _GEOMETRY_KINDS, _geometry_column_info, _scalar_sql_type


def _avoid_identity_collision(columns: list[Column]) -> dict[str, str]:
    """Rename any column literally named `OID_COLUMN` ("id") to `"id_attr"` (or `"id_attr_2"`, ... on a further
    collision), IN PLACE - returns the `{old_name: new_name}` rename map.

    Real corpus case: a genuine INTERLIS attribute literally named
    `Id`/`ID` collides with the reserved identity column. The caller must
    also apply the rename map to any `UniqueConstraint` built from the
    same attributes.
    """
    used = {c.name for c in columns}
    renamed: dict[str, str] = {}
    for column in columns:
        if column.name != OID_COLUMN:
            continue
        base_name = f"{OID_COLUMN}_attr"
        new_name = base_name
        suffix = 2
        while new_name in used:
            new_name = f"{base_name}_{suffix}"
            suffix += 1
        used.discard(OID_COLUMN)
        used.add(new_name)
        column.name = new_name
        renamed[OID_COLUMN] = new_name
    return renamed


def _columns_for_class(
    cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    prefix: str = "",
    depth: int = 0,
) -> tuple[
    list[Column],
    list[ForeignKey],
    list[str],
    list[tuple[str, MetaInstance]],
    dict[str, list[list[str]]],
    list[tuple[str, MetaInstance, bool, bool]],
]:
    """Return `(columns, foreign_keys, notes, child_specs, local_unique, abstract_specs)` for `cls`'s own+inherited
    members, flattening up to `_MAX_STRUCT_FLATTEN_DEPTH` levels of STRUCTURE nesting inline.

    `prefix`/`depth` track the recursive STRUCTURE-flattening call.
    `child_specs` are `BAG`/`LIST OF` members for `_build_child_table`;
    `local_unique` merges up nested `UNIQUE (LOCAL)`; `abstract_specs` are
    ABSTRACT-structure attributes, one child table per concrete subclass.
    """
    columns: list[Column] = []
    foreign_keys: list[ForeignKey] = []
    notes: list[str] = []
    child_specs: list[tuple[str, MetaInstance]] = []
    local_unique: dict[str, list[list[str]]] = {}
    abstract_specs: list[tuple[str, MetaInstance, bool, bool]] = []
    members = schema_members_of(cls, symbol_table) if symbol_table is not None else attributes_of(cls)
    for name, attr in members.items():
        resolved = resolve_attribute(attr)
        label = f"{prefix}{name}"
        col_name = _sql_identifier(label)

        if resolved.type_kind == "MultiValue":
            multi_value = resolved.type_instance
            if not isinstance(multi_value, MetaInstance):
                notes.append(
                    _diag(
                        "SQL-BAGLIST-ELEMENT-UNRESOLVED",
                        f"{label}: BAG/LIST OF element type not resolved - provide its model via --repo",
                    )
                )
                continue
            element = getattr(multi_value, "BaseType", None)
            is_abstract_struct = (
                isinstance(element, MetaInstance)
                and _is_structure(element)
                and bool(getattr(element, "Abstract", False))
            )
            if is_abstract_struct:
                abstract_specs.append((label, element, bool(getattr(multi_value, "Ordered", False)), True))
                continue
            child_specs.append((label, multi_value))
            continue

        if resolved.type_kind == "Class" and _is_structure(resolved.type_instance):
            if depth >= _MAX_STRUCT_FLATTEN_DEPTH:
                notes.append(
                    _diag(
                        "SQL-STRUCT-NESTED-DEEP",
                        f"{label}: STRUCTURE nested more than {_MAX_STRUCT_FLATTEN_DEPTH} levels deep - not flattened",
                    )
                )
                continue
            if bool(getattr(resolved.type_instance, "Abstract", False)):
                abstract_specs.append((label, resolved.type_instance, False, False))
                continue
            sub_columns, sub_fks, sub_notes, sub_child_specs, sub_local_unique, sub_abstract_specs = _columns_for_class(
                resolved.type_instance,
                symbol_table,
                prefix=f"{label}_",
                depth=depth + 1,
            )
            columns.extend(sub_columns)
            foreign_keys.extend(sub_fks)
            notes.extend(sub_notes)
            child_specs.extend(sub_child_specs)
            abstract_specs.extend(sub_abstract_specs)
            for nested_key, nested_groups in sub_local_unique.items():
                local_unique.setdefault(nested_key, []).extend(nested_groups)
            struct_local_unique, struct_local_unique_notes = _local_unique_constraints_for_class(resolved.type_instance)
            for role_attr, groups in struct_local_unique.items():
                local_unique.setdefault(f"{label}_{role_attr}", []).extend(groups)
            notes.extend(f"{label}: {note}" for note in struct_local_unique_notes)
            continue

        if resolved.type_kind in ("Class", "ReferenceType") and resolved.type_instance is not None:
            target = reference_target_class(resolved)
            if target is None:
                notes.append(
                    _diag(
                        "SQL-REF-TARGET-UNRESOLVED",
                        f"{label}: reference target not resolved - pass its model's directory "
                        f"to --repo, or the model file to --catalog",
                    )
                )
                continue
            target_table = _sql_identifier(getattr(target, "Name", None) or "")
            columns.append(Column(col_name, "text", nullable=not resolved.mandatory))
            fk_name = _truncate_identifier(_sql_identifier(f"fk_{getattr(cls, 'Name', '')}_{label}"))
            foreign_keys.append(ForeignKey(fk_name, [col_name], target_table, [OID_COLUMN]))
            continue

        if resolved.type_kind in _GEOMETRY_KINDS:
            sfa_type, srid, reason = _geometry_column_info(resolved)
            if sfa_type is None:
                notes.append(
                    _diag("SQL-GEOM-NO-CRS", f"{label}: {reason} - provide the geometry base model via --repo")
                )
                continue
            columns.append(
                Column(
                    col_name,
                    sql_type="",
                    nullable=not resolved.mandatory,
                    geometry_type=sfa_type,
                    srid=srid,
                )
            )
            continue

        scalar_type = _scalar_sql_type(resolved)
        if scalar_type is not None:
            columns.append(Column(col_name, scalar_type, nullable=not resolved.mandatory))
            continue

        if resolved.type_kind is None:
            notes.append(
                _diag(
                    "BUILD-TYPE-UNRESOLVED",
                    f"{label}: attribute type not resolved by the model builder - "
                    f"provide the imported model via --repo",
                )
            )
        else:
            notes.append(_diag("SQL-ATTR-TYPE-UNMAPPED", f"{label}: unsupported type {resolved.type_kind!r}"))
    return columns, foreign_keys, notes, child_specs, local_unique, abstract_specs


def _build_child_table(
    parent_table: str,
    attr_name: str,
    multi_value: MetaInstance,
    symbol_table: SymbolTable | None,
) -> tuple[Table | None, dict[str, str], str | None, list[tuple[str, MetaInstance]]]:
    """Return `(child_table, renamed, None, nested_child_specs)` on success or `(None, {}, reason, [])` on failure,
    for one `BAG`/`LIST OF` attribute.

    Companion to `convert/jsonfg.py`'s `include_child_rows` synthetic
    Features (same `"featureType"` routing). Schema: a `<parent>_fk`
    `FOREIGN KEY` + `seq` (if `Ordered`) + the element's own value
    column(s). A `BAG`/`LIST OF` nested inside is returned as
    `nested_child_specs`, not built here - `build_tables` makes it its
    own `<this table>_<subattr>` child table, one level deeper.
    """
    base_type = getattr(multi_value, "BaseType", None)
    if not isinstance(base_type, MetaInstance):
        return None, {}, "BaseType not resolved", []
    base_kind = base_type._qualified_class.rsplit(".", 1)[-1]

    child_table_name = _sql_identifier(f"{parent_table}_{attr_name}")
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    notes: list[str] = []

    if bool(getattr(multi_value, "Ordered", False)):
        columns.append(Column("seq", "integer", nullable=False))

    nested_child_specs: list[tuple[str, MetaInstance]] = []
    if base_kind == "Class" and _is_structure(base_type):
        if bool(getattr(base_type, "Abstract", False)):
            # An abstract element is routed to `abstract_specs` by `_columns_for_class`
            # (one child table per concrete subclass) - never reaches here.
            return None, {}, "BAG/LIST OF an ABSTRACT structure - subclass polymorphism not mapped to a table", []
        sub_columns, sub_fks, sub_notes, sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
            base_type, symbol_table
        )
        columns.extend(sub_columns)
        foreign_keys.extend(sub_fks)
        notes.extend(sub_notes)
        nested_child_specs = sub_child_specs
    elif base_kind in ("Class", "ReferenceType"):
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        target = reference_target_class(synthetic)
        if target is None:
            return None, {}, "reference target not resolved - pass its model to --repo or --catalog", []
        target_table = _sql_identifier(getattr(target, "Name", None) or "")
        columns.append(Column("value", "text", nullable=True))
        foreign_keys.append(
            ForeignKey(
                _truncate_identifier(_sql_identifier(f"fk_{child_table_name}_value")),
                ["value"],
                target_table,
                [OID_COLUMN],
            )
        )
    elif base_kind in _GEOMETRY_KINDS:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        sfa_type, srid, reason = _geometry_column_info(synthetic)
        if sfa_type is None:
            return None, {}, reason, []
        columns.append(Column("value", sql_type="", nullable=False, geometry_type=sfa_type, srid=srid))
    else:
        synthetic = ResolvedAttribute(attr=base_type, type_instance=base_type, type_kind=base_kind, mandatory=True)
        scalar_type = _scalar_sql_type(synthetic)
        if scalar_type is None:
            return None, {}, f"unsupported element type {base_kind!r}", []
        columns.append(Column("value", scalar_type, nullable=False))

    renamed = _avoid_identity_collision(columns)
    table = Table(name=child_table_name, columns=columns, foreign_keys=foreign_keys, notes=notes)
    return table, renamed, None, nested_child_specs


def _build_nested_child_tables(
    parent_child_table: Table,
    child_specs: list[tuple[str, MetaInstance]],
    symbol_table: SymbolTable | None,
    used_table_names: set[str],
) -> list[Table]:
    """Build one `<parent_child_table>_<attr>` table per `BAG`/`LIST OF` attribute found one level inside it.

    `INSPECTION OF <class> -> a -> b` needs exactly this chain of tables
    to exist. Bounded to ONE level (no real corpus evidence of a third
    nesting level) - a further-nested `BAG`/`LIST OF` is noted, not built.
    """
    out: list[Table] = []
    for attr_name, multi_value in child_specs:
        child_table, _renamed, reason, deeper_specs = _build_child_table(
            parent_child_table.name, attr_name, multi_value, symbol_table
        )
        if child_table is None:
            rule = (
                "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                if reason and "not resolved" in reason
                else "SQL-BAGLIST-ELEMENT-UNMAPPED"
            )
            parent_child_table.notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
            continue
        base_name = child_table.name
        name = base_name
        suffix = 2
        while name in used_table_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(name)
        child_table.name = name
        if deeper_specs:
            child_table.notes.append(
                _diag(
                    "SQL-BAGLIST-ELEMENT-UNMAPPED",
                    "a further nested BAG/LIST OF attribute is not built as a table (2 levels of nesting is the bound)",
                )
            )
        out.append(child_table)
    return out


def _structure_child_table(
    parent_table: str,
    table_name: str,
    struct_cls: MetaInstance,
    symbol_table: SymbolTable | None,
    *,
    ordered: bool,
) -> tuple[Table, dict[str, str]]:
    """One child table holding instances of a concrete STRUCTURE `struct_cls`.

    Used for an ABSTRACT structure attribute's concrete subclasses (one
    table per subclass, `concrete_structure_subclasses`) - same shape as
    `_build_child_table`'s own STRUCTURE branch (a `<parent>_fk` back to the
    parent, `seq` when `ordered`, then the structure's own flattened
    columns), factored out so `build_tables` can call it per subclass.
    """
    fk_column = _sql_identifier(f"{parent_table}_fk")
    columns: list[Column] = [Column(fk_column, "text", nullable=False)]
    foreign_keys: list[ForeignKey] = [
        ForeignKey(
            _truncate_identifier(_sql_identifier(f"fk_{table_name}_{fk_column}")),
            [fk_column],
            parent_table,
            [OID_COLUMN],
        )
    ]
    if ordered:
        columns.append(Column("seq", "integer", nullable=False))
    sub_columns, sub_fks, sub_notes, _sub_child_specs, _sub_local_unique, _sub_abstract = _columns_for_class(
        struct_cls, symbol_table
    )
    columns.extend(sub_columns)
    foreign_keys.extend(sub_fks)
    renamed = _avoid_identity_collision(columns)
    return Table(name=table_name, columns=columns, foreign_keys=foreign_keys, notes=list(sub_notes)), renamed


def _check_constraints_for_class(
    cls: MetaInstance,
    table_name: str,
    column_names: set[str],
    renamed: dict[str, str],
) -> tuple[list[CheckConstraint], list[str]]:
    """Return `(constraints, notes)` for `cls`'s own row-local `MANDATORY CONSTRAINT`s - same scope as
    `constraint_eval.py`'s `check_feature_constraints`.

    `UniqueConstraint` is handled separately. `SetConstraint`/
    `ExistenceConstraint`/percentage-based plausibility are
    population/basket-level checks no single-row `CHECK` can express -
    surfaced as a `-- NOTE`, never dropped silently (RULE #5).
    """
    result: list[CheckConstraint] = []
    notes: list[str] = []
    counter = 0
    for constraint in getattr(cls, "Constraint", None) or []:
        qname = constraint._qualified_class.rsplit(".", 1)[-1]
        label = repr(getattr(constraint, "Name", None)) if getattr(constraint, "Name", None) else "<unnamed>"
        if qname == "UniqueConstraint":
            continue  # handled by _unique_constraints_for_class / _local_unique_constraints_for_class
        if qname == "ExistenceConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-EXISTENCE",
                    f"EXISTENCE CONSTRAINT {label}: a check that a value also occurs in another class - "
                    "no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname == "SetConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-SET",
                    f"SET CONSTRAINT {label}: a whole-population check - no single-row SQL CHECK can express it",
                )
            )
            continue
        if qname != "SimpleConstraint":
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-NOT-ROWLOCAL",
                    f"CONSTRAINT {label} ({qname}): not a row-local MANDATORY CONSTRAINT - no CHECK generated",
                )
            )
            continue
        if (
            getattr(constraint, "Kind", None) not in (None, "MandC")
            or getattr(constraint, "Percentage", None) is not None
        ):
            notes.append(
                _diag(
                    "SQL-CONSTRAINT-PLAUSIBILITY",
                    f"CONSTRAINT {label}: percentage-based plausibility form (Kind="
                    f"{getattr(constraint, 'Kind', None)!r}) - a population ratio no single-row SQL CHECK can express",
                )
            )
            continue
        expr = getattr(constraint, "LogicalExpression", None)
        if expr is None:
            continue
        counter += 1
        name = getattr(constraint, "Name", None)
        try:
            sql_expr = _expression_to_sql(expr, column_names, renamed)
        except _UnsupportedCheckExpression as exc:
            notes.append(
                _diag(
                    "SQL-CHECK-EXPR-UNSUPPORTED",
                    f"MANDATORY CONSTRAINT {name or f'#{counter}'!r}: {exc} - CHECK not generated",
                )
            )
            continue
        constraint_name = _truncate_identifier(
            _sql_identifier(f"chk_{table_name}_{name}" if name else f"chk_{table_name}_{counter}")
        )
        result.append(CheckConstraint(constraint_name, sql_expr))
    return result, notes


def _unique_constraints_for_class(
    cls: MetaInstance, table_name: str
) -> tuple[list[UniqueConstraint], list[str], dict[str, list[list[str]]]]:
    """Return `(constraints, notes, struct_global_unique)` for `cls`'s own `Kind=GlobalU` `UniqueConstraint`s.

    `constraints`: plain `UNIQUE (...)` over own columns. `struct_global_unique`:
    `{struct attr: [[sub-attr column, ...], ...]}` for a `UNIQUE X->Y`
    where `X` is a `BAG`/`LIST OF STRUCTURE` attribute - `build_tables`
    turns each group into a `UNIQUE` on the `<parent>_<attr>` child table.
    `Kind=LocalU` is handled separately (`_local_unique_constraints_for_class`).
    """
    result: list[UniqueConstraint] = []
    notes: list[str] = []
    struct_global_unique: dict[str, list[list[str]]] = {}
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        kind = getattr(constraint, "Kind", None)
        if kind == "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if kind != "GlobalU" or not path_defs:
            notes.append(_diag("SQL-UNIQUE-BASKET", f"UNIQUE ({kind}): basket-scoped UNIQUE not supported yet"))
            continue
        own_columns: list[str] = []
        two_hop: list[tuple[str, str]] = []  # (raw first-hop attr name, sql sub-column)
        bad = False
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if any(getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els):
                bad = True
                break
            refs = [getattr(pe, "Ref", None) or "" for pe in path_els]
            if len(refs) == 1:
                own_columns.append(_sql_identifier(refs[0]))
            elif len(refs) == 2:
                two_hop.append((refs[0], _sql_identifier(refs[1])))
            else:
                bad = True
                break
        if bad or (own_columns and two_hop):
            # 3+ hops, an unexpected PathEl kind, or a mix of an own column
            # and a navigated one in the SAME constraint - not one plain
            # UNIQUE and not one child-table UNIQUE either.
            notes.append(
                _diag(
                    "SQL-UNIQUE-CROSS-REF",
                    "UNIQUE across a '->' reference - not expressible as a plain SQL table constraint",
                )
            )
            continue
        if two_hop:
            first_hops = {h[0] for h in two_hop}
            if len(first_hops) != 1:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        "UNIQUE mixing several '->' navigations in one constraint - not expressible in SQL",
                    )
                )
                continue
            struct_global_unique.setdefault(two_hop[0][0], []).append([h[1] for h in two_hop])
            continue
        if own_columns:
            name = _truncate_identifier(_sql_identifier(f"uq_{table_name}_{'_'.join(own_columns)}"))
            result.append(UniqueConstraint(name, own_columns))
    return result, notes, struct_global_unique


def _local_unique_constraints_for_class(cls: MetaInstance) -> tuple[dict[str, list[list[str]]], list[str]]:
    """Return `({BAG/LIST attr name: [[sub-attr column, ...], ...]}, notes)` for `cls`'s own `Kind=LocalU`
    `UniqueConstraint`s.

    E.g. `UNIQUE (LOCAL) Entries: Code;` -> `PathEls=[Entries, Code]`: the
    role path must be exactly ONE hop naming the `BAG`/`LIST OF` attribute,
    shared by every entry of the same constraint (RULE #7: a multi-hop or
    mixed role path is grammatically possible but unseen in the corpus).
    """
    result: dict[str, list[list[str]]] = {}
    notes: list[str] = []
    for constraint in getattr(cls, "Constraint", None) or []:
        if not constraint._qualified_class.endswith("UniqueConstraint"):
            continue
        if getattr(constraint, "Kind", None) != "LocalU":
            continue
        path_defs = getattr(constraint, "UniqueDef", None) or []
        if not path_defs:
            notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): role path could not be resolved - not supported")
            )
            continue
        role_attr: str | None = None
        columns: list[str] = []
        supported = True
        for path in path_defs:
            path_els = getattr(path, "PathEls", None) or []
            if len(path_els) < 2 or any(
                getattr(pe, "Kind", None) not in ("ReferenceAttr", "Attribute") for pe in path_els
            ):
                notes.append(_diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL): path shape not supported"))
                supported = False
                break
            *role_hops, sub_attr = path_els
            if len(role_hops) != 1:
                notes.append(
                    _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", "UNIQUE (LOCAL) across a multi-hop role path - not supported")
                )
                supported = False
                break
            hop_name = getattr(role_hops[0], "Ref", None) or ""
            if role_attr is None:
                role_attr = hop_name
            elif hop_name != role_attr:
                notes.append(
                    _diag(
                        "SQL-UNIQUE-LOCAL-UNSUPPORTED",
                        "UNIQUE (LOCAL) mixing several BAG/LIST attributes in one constraint - not supported",
                    )
                )
                supported = False
                break
            columns.append(_sql_identifier(getattr(sub_attr, "Ref", None) or ""))
        if supported and role_attr and columns:
            result.setdefault(role_attr, []).append(columns)
    return result, notes


def build_tables(
    classes: list[MetaInstance],
    symbol_table: SymbolTable | None = None,
    *,
    class_symbol_tables: dict[int, SymbolTable] | None = None,
    class_table_names: dict[int, str] | None = None,
) -> list[Table]:
    """Convert every `Class(Kind=Class)` in `classes` into a `Table` - the dialect-neutral IR every renderer consumes.

    No reachability discovery beyond `classes` itself: a STRUCTURE-typed
    attribute flattens INLINE, never becomes a separate `Table`.
    `class_table_names` (optional out-param) lets `build_views` map a
    View's base classes to their tables by identity. `class_symbol_tables`
    overrides `symbol_table` per-class for a `--catalog` class whose
    embedding association lives in a different model's own table.
    """
    tables = []
    used_table_names: set[str] = set()
    # For abstract-STRUCTURE subclass discovery: the root table plus every
    # distinct per-class table (--catalog / folded-in models) - a concrete
    # subclass can be registered in a different model's table than the
    # abstract base it extends.
    _distinct_catalog_tables = {id(t): t for t in (class_symbol_tables or {}).values()}.values()
    scan_symbol_tables: list[SymbolTable] = [st for st in [symbol_table, *_distinct_catalog_tables] if st is not None]
    for cls in classes:
        if getattr(cls, "Kind", None) != "Class":
            continue
        base_name = _sql_identifier(getattr(cls, "Name", None) or "")
        table_name = base_name
        suffix = 2
        while table_name in used_table_names:
            table_name = f"{base_name}_{suffix}"
            suffix += 1
        used_table_names.add(table_name)
        if class_table_names is not None:
            class_table_names[id(cls)] = table_name

        home_table = (class_symbol_tables or {}).get(id(cls), symbol_table)
        columns, foreign_keys, notes, child_specs, nested_local_unique, abstract_specs = _columns_for_class(
            cls, home_table
        )
        renamed = _avoid_identity_collision(columns)
        unique_constraints, unique_notes, struct_global_unique = _unique_constraints_for_class(cls, table_name)
        for unique in unique_constraints:
            unique.columns = [renamed.get(c, c) for c in unique.columns]
        column_names = {c.name for c in columns}
        valid_unique_constraints = []
        for unique in unique_constraints:
            missing = [c for c in unique.columns if c not in column_names]
            if missing:
                unique_notes.append(
                    _diag(
                        "SQL-UNIQUE-COL-UNMAPPED",
                        f"UNIQUE ({', '.join(unique.columns)}): column(s) {missing} have no mapped SQL type",
                    )
                )
                continue
            valid_unique_constraints.append(unique)
        check_constraints, check_notes = _check_constraints_for_class(cls, table_name, column_names, renamed)
        local_unique, local_unique_notes = _local_unique_constraints_for_class(cls)
        for attr_name, groups in nested_local_unique.items():
            local_unique.setdefault(attr_name, []).extend(groups)

        parent_table = Table(
            name=table_name,
            columns=columns,
            unique_constraints=valid_unique_constraints,
            foreign_keys=foreign_keys,
            check_constraints=check_constraints,
            notes=notes + unique_notes + check_notes + local_unique_notes,
        )
        tables.append(parent_table)

        for attr_name, multi_value in child_specs:
            child_table, child_renamed, reason, nested_specs = _build_child_table(
                table_name, attr_name, multi_value, home_table
            )
            if child_table is None:
                rule = (
                    "SQL-BAGLIST-ELEMENT-UNRESOLVED"
                    if reason and "not resolved" in reason
                    else "SQL-BAGLIST-ELEMENT-UNMAPPED"
                )
                tables[-1].notes.append(_diag(rule, f"{attr_name}: BAG/LIST OF - {reason}"))
                continue
            child_base_name = child_table.name
            child_name = child_base_name
            suffix = 2
            while child_name in used_table_names:
                child_name = f"{child_base_name}_{suffix}"
                suffix += 1
            used_table_names.add(child_name)
            child_table.name = child_name

            fk_column = _sql_identifier(f"{table_name}_fk")
            child_column_names = {c.name for c in child_table.columns}
            for group_columns in local_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                full_columns = [fk_column, *remapped]
                missing = [c for c in full_columns if c not in child_column_names]
                if missing:
                    child_table.notes.append(
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, full_columns))

            # `UNIQUE <this attr>->Sub` (global, no per-parent scoping): a
            # plain UNIQUE over the child table's own sub-attribute columns.
            for group_columns in struct_global_unique.pop(attr_name, []):
                remapped = [child_renamed.get(c, c) for c in group_columns]
                missing = [c for c in remapped if c not in child_column_names]
                if missing:
                    cols = ", ".join(group_columns)
                    child_table.notes.append(
                        _diag(
                            "SQL-UNIQUE-COL-UNMAPPED",
                            f"UNIQUE {attr_name}->({cols}): column(s) {missing} have no mapped SQL type",
                        )
                    )
                    continue
                name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(remapped)}"))
                child_table.unique_constraints.append(UniqueConstraint(name, remapped))

            tables.append(child_table)
            if nested_specs:
                tables.extend(_build_nested_child_tables(child_table, nested_specs, home_table, used_table_names))

        # An ABSTRACT structure attribute (single-valued or BAG/LIST OF):
        # one child table per concrete subclass reachable in the symbol
        # table, mirroring the JSON Schema pipeline's `anyOf`. The table
        # name (`<parent>_<attr>_<subclass>`) is the discriminant - no
        # extra `kind` column. No subclass in the conversion -> a `-- NOTE`.
        for attr_name, abstract_cls, ordered, from_multivalue in abstract_specs:
            subclasses = concrete_structure_subclasses(abstract_cls, home_table, *scan_symbol_tables)
            if not subclasses:
                if from_multivalue:
                    rule, kind = "SQL-BAGLIST-ELEMENT-UNMAPPED", "BAG/LIST OF an ABSTRACT structure"
                else:
                    rule, kind = "SQL-STRUCT-ABSTRACT", "ABSTRACT structure"
                parent_table.notes.append(
                    _diag(
                        rule,
                        f"{attr_name}: {kind} - no concrete subclass in this conversion to build a "
                        f"table per subtype (provide the model that defines them via --repo/--catalog)",
                    )
                )
                continue
            groups_for_attr = local_unique.pop(attr_name, [])
            fk_column = _sql_identifier(f"{table_name}_fk")
            for sub in subclasses:
                sub_name = _sql_identifier(getattr(sub, "Name", None) or "")
                child_name = _dedup_name(_sql_identifier(f"{table_name}_{attr_name}_{sub_name}"), used_table_names)
                child_table, child_renamed = _structure_child_table(
                    table_name, child_name, sub, home_table, ordered=ordered
                )
                child_column_names = {c.name for c in child_table.columns}
                for group_columns in groups_for_attr:
                    full_columns = [fk_column, *(child_renamed.get(c, c) for c in group_columns)]
                    missing = [c for c in full_columns if c not in child_column_names]
                    if missing:
                        child_table.notes.append(
                            _diag(
                                "SQL-UNIQUE-COL-UNMAPPED",
                                f"UNIQUE (LOCAL) {attr_name}: column(s) {missing} have no mapped SQL type",
                            )
                        )
                        continue
                    name = _truncate_identifier(_sql_identifier(f"uq_{child_table.name}_{'_'.join(full_columns)}"))
                    child_table.unique_constraints.append(UniqueConstraint(name, full_columns))
                tables.append(child_table)

        # Any UNIQUE (LOCAL) whose role hop never matched a real BAG/LIST OF
        # attribute on this class (typo, or a role path this project's
        # grammar mapping doesn't reach) - never silently dropped (RULE #5).
        for attr_name in local_unique:
            parent_table.notes.append(
                _diag("SQL-UNIQUE-LOCAL-UNSUPPORTED", f"UNIQUE (LOCAL) {attr_name}: no matching BAG/LIST OF attribute")
            )

        # `UNIQUE X->Y` whose first hop `X` is not a `BAG`/`LIST OF STRUCTURE`
        # attribute of this class: a real `REFERENCE TO`/role navigation to
        # another table's column (or an abstract structure split across
        # subtype tables). No single table/index constraint expresses it -
        # a BEFORE INSERT/UPDATE trigger would.
        for attr_name, groups in struct_global_unique.items():
            for group_columns in groups:
                parent_table.notes.append(
                    _diag(
                        "SQL-UNIQUE-CROSS-REF",
                        f"UNIQUE {attr_name}->({', '.join(group_columns)}): navigates a '->' reference to another "
                        "table - needs a trigger, not a table constraint",
                    )
                )

    # A real bug found the same way (PostgreSQL, live `psycopg`-free
    # verification against a real `postgis/postgis` container):
    # a `REFERENCE TO`/Role target belonging to a DIFFERENT model (real
    # corpus case, `ili_corpus/LWB_Bewirtschaftungseinheiten_V3_0.ili`'s
    # `Zone_Ausland` -> `LWB_Landwirtschaftliche_Zonengrenzen_Kataloge_V2_0.
    # LZ_Kataloge.LZ_Katalog_TypRef`) resolves to a REAL Class via
    # `reference_target_class` (repository-loaded, so not caught by the
    # "unresolved reference" check in `_columns_for_class`) but that class
    # is NOT among `classes` - this function deliberately converts ONE
    # model's OWN classes only (no cross-model reachability discovery,
    # unlike `convert/jsonschema.py`'s `_discover_classes`). The FK column itself
    # (a valid OID string either way) is kept; only the now-dangling
    # `FOREIGN KEY` constraint - which would `ALTER TABLE ... REFERENCES` a
    # table this conversion never creates - is dropped.
    final_table_names = {t.name for t in tables}
    for table in tables:
        kept_fks = []
        for fk in table.foreign_keys:
            if fk.ref_table not in final_table_names:
                table.notes.append(
                    _diag(
                        "SQL-FK-CROSS-MODEL-DROPPED",
                        f"FOREIGN KEY ({', '.join(fk.columns)}): target table {fk.ref_table!r} belongs to a "
                        "different model, not created by this conversion - pass that model via --catalog "
                        "(constraint dropped, column kept)",
                    )
                )
                continue
            kept_fks.append(fk)
        table.foreign_keys = kept_fks
    return tables
