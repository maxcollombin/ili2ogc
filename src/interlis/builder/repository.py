"""Multi-file resolution (IMPORTS).

Loads an imported model on demand from a set of local directories, so
qualified references (e.g. `GeometryCHLV95_V2.Coord2`) resolve to the real
instance rather than an `UnresolvedNamedReference`.

Deliberately local-only scope (never network during a build - the corpus
must be downloaded beforehand, see README) and never merged into a global
symbol table (see ForwardRefResolver/SymbolTable): each loaded model keeps
its OWN isolated SymbolTable, to never introduce a short-name ambiguity
BETWEEN unrelated models (only full-qualified-name resolution, in the
explicitly targeted model's table, crosses files).
"""
import re
from pathlib import Path
from typing import Any

from interlis.builder.errors import BuildError
from interlis.runtime.parse import meta_attribute_comments, meta_attribute_comments_in_file, parse_file, parse_text

# Matches the real grammar (`modeldef`, vendor/interlis-antlr4/InterlisParser.g4):
# `CONTRACTED? (TYPE | REFSYSTEM | SYMBOLOGY)? MODEL Name ...` - MODEL is
# ALWAYS directly followed by the real Name regardless of the optional
# prefix keyword in front (or its absence), so a single keyword is enough
# to search for. (An earlier version matched `(?:MODEL|REFSYSTEM)` as if
# either keyword could directly precede the Name, which silently broke
# indexing for e.g. `REFSYSTEM MODEL CoordSys`.)
_MODEL_NAME_RE = re.compile(r"\bMODEL\s+([A-Za-z_][A-Za-z0-9_]*)")

# The predefined "INTERLIS" namespace model, built via the real parse+build
# pipeline rather than hand-crafted Python instances. Design rationale
# (why ANYOID/UUIDOID/BOOLEAN are deliberately absent, why GregorianYear/
# XMLDate/XMLTime/XMLDateTime were added): docs/dev-notes/predefined-interlis-namespace.md.
_PREDEFINED_MODEL_INTERNAL_NAME = "PredefinedInterlisNamespace"
_PREDEFINED_INTERLIS_SOURCE = f"""\
INTERLIS 2.4;

MODEL {_PREDEFINED_MODEL_INTERNAL_NAME} AT "http://www.interlis.ch" VERSION "2024-04-24" =

  DOMAIN
    NOOID = OID ANY;
    I32OID = OID 0..2147483647;
    STANDARDOID = OID TEXT*16;
    GregorianYear = 1582..2999;
    XMLDate = FORMAT INTERLIS.XMLDate "0001-01-01" .. "9999-12-31";
    XMLTime = FORMAT INTERLIS.XMLTime "00:00:00" .. "23:59:59";
    XMLDateTime = FORMAT INTERLIS.XMLDateTime "0001-01-01T00:00:00" .. "9999-12-31T23:59:59";

END {_PREDEFINED_MODEL_INTERNAL_NAME}.
"""
_BUILTIN_SOURCES = {"INTERLIS": _PREDEFINED_INTERLIS_SOURCE}


class ModelRepository:
    """Index a set of directories by declared MODEL/REFSYSTEM name.

    A lightweight text scan, not a full ANTLR parse - a file's name or the
    <Name> in an external index like ilimodels.xml doesn't necessarily
    match the MODEL name actually declared (confirmed on the real
    models.geo.admin.ch corpus: an "obsolete/..." file can declare a MODEL
    with a name entirely different from its file name).
    """

    def __init__(self, search_dirs: list[Path]):
        self._index: dict[str, Path] = {}
        for directory in search_dirs:
            for path in sorted(Path(directory).glob("*.ili")):
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for match in _MODEL_NAME_RE.finditer(text):
                    # first found wins (heuristic: a text-scan false
                    # positive, e.g. inside a comment, must never overwrite
                    # a name already indexed correctly).
                    self._index.setdefault(match.group(1), path)
        # model name -> SymbolTable (already built, possibly partially if
        # in progress - see anti-cycle guard below), or None if attempted
        # and not found/failed - never retried.
        self._cache: dict[str, Any] = {}
        self._make_sub_builder = None  # injected by InterlisModelBuilder

    def path_for(self, model_name: str) -> Path | None:
        """Return the indexed `.ili` file path for this MODEL/REFSYSTEM name.

        Resolution driven by the HEADERSECTION/MODELS of an XTF being
        validated (see xtf/model_resolution.py). `None` for a predefined
        model (`_BUILTIN_SOURCES`, never a real file on disk) or one
        absent from the given `--repo` directories.
        """
        return self._index.get(model_name)

    def register_prebuilt(self, model_name: str, symbol_table) -> None:
        """Register an already-built model in the resolution cache.

        Same cache as `resolve_external`/`availability`. For a model built
        elsewhere (the root model of `interlis validate`, built directly by
        the main builder via its own `parse_file`+`build()`, not via
        `_get_table`) - avoids re-parsing/rebuilding it a second time
        during the header completeness check (`availability` below).
        """
        self._cache[model_name] = symbol_table

    def availability(self, model_name: str) -> str:
        """Return a named model's resolvability status.

        One of 'builtin' | 'available' | 'indexed_but_failed' | 'missing' -
        for the PROACTIVE header-vs-resolved completeness check used by
        `interlis validate` (see
        xtf/model_resolution.py:header_completeness), independent of what
        a DATASECTION actually exercises (`resolve_external` only loads
        what's actually REFERENCED). Reuses `_get_table` (same cache as
        `resolve_external`): no extra cost if this model gets touched later
        anyway by a real reference, and `register_prebuilt` avoids the
        double cost for the root model itself.
        """
        if model_name in _BUILTIN_SOURCES:
            return "builtin"
        if model_name not in self._index and model_name not in self._cache:
            return "missing"
        table = self._get_table(model_name)
        return "available" if table is not None else "indexed_but_failed"

    def bind_builder_factory(self, factory) -> None:
        """Inject the sub-builder factory used to build imported files.

        Provided by the root builder, which owns the shared components
        (schema/registry/spec/attachment) to reuse for each imported file
        instead of reloading them from disk. `factory() ->
        InterlisModelBuilder`, already configured with `repository=self`
        so its OWN imports resolve recursively the same way.
        """
        self._make_sub_builder = factory

    def resolve_external(self, model_name: str, full_dotted_name: str, kind_hint) -> Any | None:
        table = self._get_table(model_name)
        if table is None:
            return None
        return table.resolve(full_dotted_name, kind_hint=kind_hint)

    def symbol_table_for(self, model_name: str):
        """Return a loaded model's complete symbol table.

        Same cache as `resolve_external`/`availability` - exposed
        separately so a caller can reuse the ENTIRE table (e.g.
        `schema.home_symbol_table`: `embedded_roles_of` must look up
        associations where they're REALLY declared, not just resolve one
        name at a time like `resolve_external`).
        """
        return self._get_table(model_name)

    def _get_table(self, model_name: str):
        """Build (or return the cached) SymbolTable for `model_name`.

        Passes `meta_attributes` (eCH-0117 `!!@Name=Value` comments, see
        `runtime.parse.meta_attribute_comments`) into the sub-build - an
        imported model's own MetaAttribute instances (e.g. `!!@CRS=...`
        on a CoordType domain) are otherwise never captured, since only
        the file passed directly to `InterlisModelBuilder.build()` used
        to get this treatment. A real-corpus gap surfaced while building
        `.xtf` -> JSON-FG geometry conversion (backlog item 5): every
        Swiss geometry domain in practice is imported (from
        `CHBase_Part1_GEOMETRY_V1`), never declared locally, so without
        this the CRS meta-attribute mechanism had zero real coverage.
        """
        if model_name in self._cache:
            return self._cache[model_name]
        if model_name in _BUILTIN_SOURCES:
            source = _BUILTIN_SOURCES[model_name]
            tree, syntax_errors = parse_text(source)
            meta_attributes = meta_attribute_comments(source)
        else:
            path = self._index.get(model_name)
            if path is None:
                self._cache[model_name] = None
                return None
            tree, syntax_errors = parse_file(path)
            meta_attributes = meta_attribute_comments_in_file(path)
        if syntax_errors or self._make_sub_builder is None:
            self._cache[model_name] = None
            return None
        builder = self._make_sub_builder()
        # Anti-cycle guard: the placeholder (the sub-builder's SymbolTable,
        # still empty) is registered in the cache BEFORE `build()` runs, not
        # after - a reentrant reference to this same model (a circular
        # import, e.g. A imports B which references A) finds its
        # partially-populated table instead of endlessly restarting the
        # load of the same file.
        self._cache[model_name] = builder.symbol_table
        try:
            result = builder.build(tree, meta_attributes=meta_attributes)
        except BuildError:
            # An EXTERNAL model indexed successfully can still genuinely
            # fail to build (a binding/mapping error on this specific
            # file). Same policy as the SYNTAX-error case just above
            # (`if syntax_errors: ... return None`): an external model that
            # doesn't build degrades to `None` (as if absent) rather than
            # crashing the whole root `validate`/`build` - the placeholder
            # already cached (anti-cycle guard above) stays empty, never
            # retried.
            self._cache[model_name] = None
            return None
        if model_name in _BUILTIN_SOURCES:
            # The Model actually declared carries an internal name
            # different from the real name documented by the manual (see
            # _PREDEFINED_INTERLIS_SOURCE, a lexer constraint) - corrected
            # here: the instance's own Name (not a fabrication, just fixing
            # the syntax workaround back to the real value), every
            # qualified entry in its table, and an alias under the expected
            # bare name so that `Import.ImportedP` (`always_external`
            # resolution, targeting the model's own name) finds the real
            # Model instance instead of an UnresolvedNamedReference.
            result.Name = model_name
            builder.symbol_table.rekey_model_prefix(_PREDEFINED_MODEL_INTERNAL_NAME, model_name)
            builder.symbol_table.register(model_name, result)
        return builder.symbol_table
