# interlis-runtime

A pure-Python INTERLIS runtime: parses an `.ili` file (INTERLIS 2 model)
and builds a graph of typed Python objects conforming to the IlisMeta16
metamodel - without depending on ili2c (the reference Java compiler) at
runtime.

## Installation

```sh
git submodule update --init --recursive   # INTERLIS grammar (vendor/interlis-antlr4)
uv sync
```

## Usage - CLI

```sh
uv run interlis build path/to/model.ili
```

If the model has `IMPORTS` clauses, pass `--repo` (repeatable) with one or
more directories of `.ili` files to resolve references into the imported
models for real, instead of leaving them as unresolved placeholders:

```sh
uv run interlis build path/to/model.ili --repo path/to/model-directory
```

`--repo` indexes every `.ili` file in the given directories by the `MODEL`/
`REFSYSTEM` name it declares (not its filename) and loads an imported model
on demand, the first time a reference into it is actually needed.

Example with a minimal model:

```interlis
INTERLIS 2.4;
MODEL Example AT "https://example.org/example" VERSION "2026-08-03" =
  TOPIC MainTopic =
    CLASS Person =
      Name: MANDATORY TEXT;
    END Person;
  END MainTopic;
END Example.
```

```sh
$ uv run interlis build example.ili --quiet
Model Name='Example' Kind='NormalM'
  At = '"https://example.org/example"'
  Version = '"2026-08-03"'
  iliVersion = '2.4'
  Element:
    SubModel Name='MainTopic'
      Element:
        Class Name='Person' Kind='Class'
          ClassAttribute:
            AttrOrParam Name='Name'
              Type:
                TextType Kind='Text'
    DataUnit Name='BASKET'
```

Options:
- `-q`/`--quiet`: hide warnings (known, non-fatal spec gaps flagged during
  construction - see [Known limitations](#known-limitations)).
- `--repo DIR`: directory of `.ili` files to resolve `IMPORTS` references
  against (repeatable). Omit for the previous single-file behavior.

## Usage - XTF validation

```sh
uv run interlis validate path/to/transfer.xtf --repo path/to/model-directory
```

Validates a `.xtf` data transfer against the schema it declares using
(structure, `MANDATORY`, base types, TID/REF resolution, embedded
association roles). `--repo` (repeatable) resolves the `.ili` schema(s):
the root model is auto-detected from the transfer's own `HEADERSECTION`/
`DATASECTION` (see `docs/model-resolution-strategy.md` for why this is
never a live Model Repository lookup) - pass `--model FILE.ili` to
override with an explicit file instead.

```sh
uv run interlis validate transfer.xtf --repo models/ --catalog codes.xtf -v
```

`--catalog FILE.xtf` (repeatable) supplies additional transfers whose
objects should also count as resolvable targets - typically a
catalogue/code-list basket distributed separately from the main data
transfer (see [Known limitations](#known-limitations) and
`docs/xtf-catalogue-references.md`). `-v`/`--verbose` also shows `info`-
level issues (unresolved-type attributes, e.g. geometry); `-q`/`--quiet`
shows only the final summary. Exit code is `1` if any `error`-severity
issue was found, `0` otherwise (warnings/info never fail the run).

When `--repo` is given, every model declared in the transfer's
`HEADERSECTION/MODELS` is proactively checked for resolvability against
it (Lot 39) - not just the ones actually touched by the data - and any
gap (model missing from `--repo`, or indexed but failing to build) is
reported up front, with a resolved-count folded into the final summary
line. This can surface issues invisible to earlier, purely lazy
validation, where a `--repo` directory that looked complete could still
be missing a model that no attribute in *this particular* transfer
happened to reference.

## Usage - Python API

```python
from pathlib import Path
from interlis.runtime.parse import parse_file
from interlis.builder.model_builder import InterlisModelBuilder

tree, syntax_errors = parse_file(Path("example.ili"))
assert not syntax_errors

builder = InterlisModelBuilder(Path("mappings"), Path("spec/grammar/mapping"))
model = builder.build(tree)

model.Name                     # "Example"
model.Element[0].Element[0]    # the "Person" Class
```

To resolve `IMPORTS` against other local models, pass a `ModelRepository`:

```python
from interlis.builder.repository import ModelRepository

repository = ModelRepository([Path("path/to/model-directory")])
builder = InterlisModelBuilder(Path("mappings"), Path("spec/grammar/mapping"), repository=repository)
model = builder.build(tree)
```

Every returned object is a Pydantic model class generated dynamically from
the IlisMeta16 metamodel (see Architecture below) - navigate it by
attribute (`.Name`, `.Element`, `.ClassAttribute`, ...), matching the
metamodel's own official names.

## Known limitations

- **`IMPORTS` resolution is local-only, opt-in, and best-effort**: without
  `--repo`/`repository=...`, a reference into an imported model stays an
  unresolved named reference (`UnresolvedNamedReference`) rather than an
  error. With it, resolution only ever looks at the given local
  directories - never the network - so any model not present there stays
  unresolved too. This includes the handful of "core" INTERLIS models
  (`Units`, `CoordSys`, ...) that real-world data commonly imports but
  that are NOT hosted on models.geo.admin.ch - they live at
  `https://models.interlis.ch/ilimodels.xml` instead (same
  `IliRepository20.RepositoryIndex.ModelMetadata` index format, confirmed
  Lot 26) - pass a directory populated from there as an additional
  `--repo`/`ModelRepository([...])` entry to resolve them too. Exception:
  the predefined `INTERLIS` namespace (see next bullet) always resolves,
  `--repo` or not - it needs no directory lookup.
- **Formal constraints are not evaluated**: `CONSTRAINT`/`MANDATORY
  CONSTRAINT` clauses are built as data (an expression tree), never
  executed against real data.
- **Topic-level default OID** (`OID AS <domain>;` without `BASKET`,
  applied to every class in the topic that doesn't declare its own OID
  clause) is not yet wired - it needs a post-processing pass over all
  classes in a topic, which the current engine doesn't perform yet.
- **INTERLIS predefined namespace domains** (Reference Manual Annex A):
  `NOOID`/`I32OID`/`STANDARDOID` are modeled and resolve both qualified
  (`INTERLIS.I32OID`) and unqualified (`I32OID` after `IMPORTS UNQUALIFIED
  INTERLIS;`). `ANYOID`/`UUIDOID` are NOT modeled: unlike the other three,
  the grammar treats them as reserved lexer tokens usable only through the
  special `INTERLIS.ANYOID`/`INTERLIS.UUIDOID` qualified forms (not through
  ordinary `domainRef`), which would need a dedicated binding - out of
  scope so far (no corpus file has needed it yet).
- **XTF catalogue/code-list references** (`REFERENCE TO (EXTERNAL) X`,
  common Swiss pattern via `CatalogueObjects_V1.Catalogues.
  MandatoryCatalogueReference`) resolve only if their target basket is
  supplied via `--catalog` - `interlis validate` cannot locate it on its
  own (neither `.ili` nor `.xtf` encode a physical location for it, and no
  public catalogue file was found for this project's real-world
  inventory). See `docs/xtf-catalogue-references.md` for the full
  investigation and how unresolved references are classified either way.
- **Attributes inherited via `EXTENDS`** are now seen by the XTF
  validator's schema lookup (`xtf/schema.py:attributes_of`, Lot 38) -
  resolved transitively up the `Super` chain, own attributes winning over
  inherited ones of the same name. A `TOPIC B EXTENDS TOPIC A` (common
  Swiss pattern, e.g. CHBase) also makes `A`'s own short names resolvable
  unqualified inside `B`, per eCH-0031 V2.1.0 §3.5.4 - this only degrades
  gracefully (`UnresolvedNamedReference`, never a crash) when the base
  model itself cannot be built, which can currently happen for reasons
  unrelated to `EXTENDS` (see the grammar-coverage bullet below).
- **Two possible grammar-coverage gaps found while testing `EXTENDS`
  resolution against the real CHBase corpus, not yet confirmed as real
  bugs or fixed**: a `MANDATORY CONSTRAINT` clause combining two
  conditions with `OR` (`CHBase_Part3_CATALOGUEOBJECTS_V1.ili`), and a
  `TOPIC` with two separate `DEPENDS ON X;` statements instead of one
  comma-separated clause (`CHBase_Part4_ADMINISTRATIVEUNITS_V1.ili`) both
  currently fail to parse, which blocks the *entire* `.ili` file
  (ANTLR rejects the whole tree on any rule failure) - not just the
  offending clause. Needs checking against the Reference Manual before
  deciding whether these are valid INTERLIS 2 forms worth supporting.

## Architecture

```
ANTLR4 grammar (vendor/interlis-antlr4)
        │  generates
        ▼
src/interlis/antlr/ (lexer + parser + visitor)
        │  parsed by
        ▼
src/interlis/runtime/parse.py  ──►  syntax tree
        │
        ▼
spec/grammar/mapping/*.yml  ──►  InterlisModelBuilder (src/interlis/builder/)
(grammar rule → metamodel class)          │  builds, guided by
        ▲                                 ▼
        │                        src/interlis/metamodel/ (Pydantic classes
mappings/ilismeta16-*.yml               generated from the metamodel)
(IlisMeta16 metamodel, source of
 truth, extracted from the ili2c XMI)
        │
        ▼
   graph of typed Python objects
```

`spec/grammar/mapping/*.yml` and `mappings/ilismeta16-*.yml` are the single
executable source of truth: `InterlisModelBuilder` is a generic engine
driven by this data, not 121 hand-written Python methods.

## Development

```sh
uv run python3 scripts/validate_spec.py   # validates spec/grammar/mapping/ against the metamodel
uv run python3 -m pytest tests/           # ModelBuilder tests
```

## ANTLR lexer/parser generation

Only needs re-running if `vendor/interlis-antlr4` changes version.

```sh
uv run --env-file .env antlr4 \
    -Dlanguage=Python3 \
    -visitor \
    -no-listener \
    -Xexact-output-dir \
    -o src/interlis/antlr \
    vendor/interlis-antlr4/InterlisLexer.g4

uv run --env-file .env antlr4 \
    -Dlanguage=Python3 \
    -visitor \
    -no-listener \
    -Xexact-output-dir \
    -lib src/interlis/antlr \
    -o src/interlis/antlr \
    vendor/interlis-antlr4/InterlisParser.g4
```
