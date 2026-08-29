"""`--lang` output overlay: rename converter output through a `TRANSLATION OF` model.

An INTERLIS translation model (`MODEL X_fr (fr) TRANSLATION OF X ["ver"]`)
re-declares the base's whole structure with translated identifiers. The
transfer format does NOT change (refman SS4.3.3: "die Tagnamen [bleiben]
in der Ursprungs-Sprache"), so a translation is a presentation overlay:
parse and evaluate against the base model, then rename the OUTPUT
(`convert` JSON Schema, `convert-jsonfg` FeatureCollection) through the
name map the builder aligned positionally (`InterlisModelBuilder._apply_pending_translations`).
`convert-sql` identifier translation is a separate increment (a `CREATE
VIEW` body bakes table/column names into text).
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import Any

from interlis.builder.repository import ModelRepository
from interlis.metamodel.instance import MetaInstance

_MODEL_LANG_RE = re.compile(r"\bMODEL\s+[A-Za-z_]\w*\s*\(\s*([a-z]{2}(?:_[A-Za-z]{2})?)\s*\)")
_TRANSLATION_OF_RE = re.compile(r"\bTRANSLATION\s+OF\s+([A-Za-z_]\w*)")


@dataclass(frozen=True)
class Translation:
    """The positional name map from a `TRANSLATION OF` model, ready to rename converter output."""

    language: str
    of: str
    elements: dict[str, str]  # {base class/view/topic/domain short name: translated}
    attributes: dict[tuple[str, str], str]  # {(owner short name, attribute short name): translated}

    def element(self, name: str) -> str:
        return self.elements.get(name, name)

    def attribute(self, owner: str | None, name: str) -> str:
        if owner is not None and (owner, name) in self.attributes:
            return self.attributes[(owner, name)]
        # fall back to any owner - a plain rename is almost always
        # consistent across classes (`Bemerkungen` -> `Remarques`)
        for (owner_name, attr_name), translated in self.attributes.items():
            if attr_name == name:
                return translated
        return name


def load_translation(base_model: str, language: str, repository: ModelRepository | None) -> Translation | None:
    """Find and build the `TRANSLATION OF <base_model>` model for `language` in `repository`, or `None`.

    Cheap header scan first (`TRANSLATION OF <base>` + `(<lang>)`), then a
    real build of just that one model so its aligned name map
    (`Model._translation`) can be reused.
    """
    if repository is None:
        return None
    for name, path in getattr(repository, "_index", {}).items():
        try:
            header = path.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
        of = _TRANSLATION_OF_RE.search(header)
        lang = _MODEL_LANG_RE.search(header)
        if of is None or of.group(1) != base_model or lang is None or lang.group(1) != language:
            continue
        table = repository.symbol_table_for(name)
        for inst in table.all_registered() if table is not None else ():
            tr = getattr(inst, "_translation", None)
            if isinstance(inst, MetaInstance) and tr and tr["of"] == base_model and tr["language"] == language:
                return Translation(language, base_model, tr["elements"], tr["attributes"])
    return None


# --------------------------------------------------------------------------- #
# apply to converter output                                                    #
# --------------------------------------------------------------------------- #
def rename_json_schema(schema: dict[str, Any], tr: Translation) -> dict[str, Any]:
    """Rename `$defs` keys, `$ref` targets, `properties` keys, `required`, and `title` in a `model_to_json_schema` result."""
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        schema["$defs"] = {tr.element(k): _rename_schema_node(v, tr, owner=k) for k, v in defs.items()}
    _rename_refs(schema, tr)
    return schema


def _rename_schema_node(node: Any, tr: Translation, owner: str | None) -> Any:
    if not isinstance(node, dict):
        return node
    props = node.get("properties")
    if isinstance(props, dict):
        node["properties"] = {tr.attribute(owner, k): _rename_schema_node(v, tr, owner) for k, v in props.items()}
    if isinstance(node.get("required"), list):
        node["required"] = [tr.attribute(owner, k) for k in node["required"]]
    if isinstance(node.get("title"), str) and owner is not None:
        node["title"] = tr.element(node["title"])
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), dict):
            node[key] = _rename_schema_node(node[key], tr, owner)
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            node[key] = [_rename_schema_node(x, tr, owner) for x in node[key]]
    return node


def _rename_refs(node: Any, tr: Translation) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            node["$ref"] = "#/$defs/" + tr.element(ref[len("#/$defs/") :])
        for value in node.values():
            _rename_refs(value, tr)
    elif isinstance(node, list):
        for item in node:
            _rename_refs(item, tr)


def rename_feature_collection(collection: dict[str, Any], tr: Translation) -> dict[str, Any]:
    """Rename `featureType` and every `properties` key in a `transfer_to_feature_collection` result."""
    for feature in collection.get("features", []):
        if not isinstance(feature, dict):
            continue
        feature_type = feature.get("featureType")
        owner = feature_type if isinstance(feature_type, str) else None
        if isinstance(feature_type, str):
            feature["featureType"] = tr.element(feature_type)
        props = feature.get("properties")
        if isinstance(props, dict):
            feature["properties"] = {tr.attribute(owner, k): v for k, v in props.items()}
    return collection


def rename_sql_ddl(ddl: str, tr: Translation) -> str:
    """Rename every quoted identifier in a `render_postgresql` / `render_gpkg` DDL through the translation.

    A `CREATE VIEW` body bakes table/column names into text and FK
    constraints reference them by name, so the rename is a single pass
    over the whole DDL rather than a per-object step. SQL identifiers here
    are always lowercase and always `"..."`-quoted, and a positional
    translation maps one base name to exactly one translated name
    everywhere it occurs - a name that would translate two different ways
    across classes (vanishingly rare) is left untranslated with a warning
    rather than renamed ambiguously.
    """
    candidates: dict[str, set[str]] = {}
    for name, translated in tr.elements.items():
        candidates.setdefault(name.lower(), set()).add(translated.lower())
    for (_owner, name), translated in tr.attributes.items():
        candidates.setdefault(name.lower(), set()).add(translated.lower())
    renames: dict[str, str] = {}
    for original, translations in candidates.items():
        if len(translations) == 1:
            renames[original] = next(iter(translations))
        else:
            warnings.warn(
                f"--lang: {original!r} translates inconsistently {sorted(translations)} - left untranslated in the SQL",
                stacklevel=2,
            )
    if not renames:
        return ddl
    pattern = re.compile('"(' + "|".join(re.escape(k) for k in renames) + ')"')
    return pattern.sub(lambda m: '"' + renames[m.group(1)] + '"', ddl)
