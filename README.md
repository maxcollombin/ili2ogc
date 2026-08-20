# interlis-runtime

A pure-Python INTERLIS 2 runtime: parses `.ili` models into a graph of typed
Python objects conforming to the IlisMeta16 metamodel, and validates `.xtf`
data transfers against that model.

## Installation

```sh
uv sync
```

or, with pip, from a checkout:

```sh
pip install .
```

The generated ANTLR lexer/parser are already committed under
`src/interlis/antlr/`, so no separate grammar build step is needed.

## Validate/build a model (`.ili`)

```sh
uv run interlis build path/to/model.ili
```

Pass `--repo` (repeatable) with directories of `.ili` files to resolve
`IMPORTS` into real objects instead of unresolved placeholders. Each
directory is indexed by the `MODEL`/`REFSYSTEM` name declared inside each
file (not its filename), and an imported model is loaded on demand, the
first time a reference into it is actually needed.

```sh
uv run interlis build path/to/model.ili --repo path/to/model-directory
```

Options:

- `-q`/`--quiet`: hide warnings (known, non-fatal spec gaps flagged during
  construction).
- `--repo DIR`: directory of `.ili` files to resolve `IMPORTS` against
  (repeatable).

## Validate a data transfer (`.xtf`)

```sh
uv run interlis validate path/to/transfer.xtf --repo path/to/model-directory
```

Validates a `.xtf` transfer against the schema it declares (structure,
`MANDATORY`, base types, TID/REF resolution, embedded association roles,
geometry/coordinate ranges). `--repo` resolves the `.ili` schema(s); the
root model is auto-detected from the transfer's `HEADERSECTION`/
`DATASECTION` unless `--model FILE.ili` is given explicitly.

```sh
uv run interlis validate transfer.xtf --repo models/ --catalog codes.xtf -v
```

Options:

- `--model FILE.ili`: explicit schema file instead of auto-detecting it.
- `--repo DIR`: directory of `.ili` files to resolve the schema's `IMPORTS`
  against (repeatable).
- `--catalog FILE.xtf`: additional transfer (repeatable) whose objects also
  count as resolvable targets - typically a catalogue/code-list basket
  distributed separately from the main transfer.
- `-v`/`--verbose`: also show `info`-level issues (e.g. `FORMAT`ted values
  still unhandled by the validator).
- `-q`/`--quiet`: only print the final summary.

Exit code is `1` if any `error`-severity issue was found, `0` otherwise
(warnings/info never fail the run). When `--repo` is given, every model
declared in the transfer's `HEADERSECTION/MODELS` is checked for
resolvability up front, not just the ones touched by the data.

## Python API - the executable metamodel

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

Every returned object is a Pydantic model class generated dynamically from
the IlisMeta16 metamodel - navigate it by attribute (`.Name`, `.Element`,
`.ClassAttribute`, ...), matching the metamodel's own official names.

Pass a `ModelRepository` to resolve `IMPORTS` against other local models:

```python
from interlis.builder.repository import ModelRepository

repository = ModelRepository([Path("path/to/model-directory")])
builder = InterlisModelBuilder(Path("mappings"), Path("spec/grammar/mapping"), repository=repository)
model = builder.build(tree)
```

To validate an already-parsed `.xtf` transfer directly from Python:

```python
from interlis.xtf.parse import parse_xtf
from interlis.xtf.validate import validate_transfer

transfer = parse_xtf(Path("transfer.xtf"))
issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
```

Each `issue` carries a `severity` (`error`/`warning`/`info`), a `message`,
and enough context (class, attribute, object TID) to locate the problem in
the source data.

## Architecture

```text
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
(IlisMeta16 metamodel, source of truth)
        │
        ▼
   graph of typed Python objects
```

`spec/grammar/mapping/*.yml` and `mappings/ilismeta16-*.yml` are the single
executable source of truth: `InterlisModelBuilder` is a generic engine
driven by this data, not hand-written Python methods per grammar rule.

## Development

```sh
uv run python3 scripts/validate_spec.py   # validates spec/grammar/mapping/ against the metamodel
uv run python3 -m pytest tests/           # ModelBuilder + XTF validator tests
```

Regenerating the vendored ANTLR grammar (only needed when
`vendor/interlis-antlr4` changes) is a maintainer-only step, not required
to use or contribute to the runtime - see `docs/grammar-regeneration.md`.
