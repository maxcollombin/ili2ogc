# interlis-runtime

A pure-Python INTERLIS runtime: parses an `.ili` file (INTERLIS 2 model)
and builds a graph of typed Python objects conforming to the IlisMeta16
metamodel, and validates `.xtf` data transfers against that model - without
depending on ili2c (the reference Java compiler) at runtime.

## Installation

```sh
uv sync
```

or, with pip, from a checkout:

```sh
pip install .
```

The generated ANTLR lexer/parser are already committed under
`src/interlis/antlr/`, so no separate grammar build step is needed to use
the runtime.

## Validate/build an INTERLIS model (`.ili`)

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
  construction).
- `--repo DIR`: directory of `.ili` files to resolve `IMPORTS` references
  against (repeatable). Omit for single-file resolution only.

## Validate a data transfer (`.xtf`)

```sh
uv run interlis validate path/to/transfer.xtf --repo path/to/model-directory
```

Validates a `.xtf` data transfer against the schema it declares (structure,
`MANDATORY`, base types, TID/REF resolution, embedded association roles).
`--repo` (repeatable) resolves the `.ili` schema(s): the root model is
auto-detected from the transfer's own `HEADERSECTION`/`DATASECTION` - pass
`--model FILE.ili` to override with an explicit file instead.

```sh
uv run interlis validate transfer.xtf --repo models/ --catalog codes.xtf -v
```

Options:
- `--model FILE.ili`: explicit schema file, instead of auto-detecting it
  from the transfer's header.
- `--repo DIR`: directory of `.ili` files to resolve the schema's `IMPORTS`
  against (repeatable).
- `--catalog FILE.xtf`: additional transfer (repeatable) whose objects
  should also count as resolvable targets - typically a catalogue/code-list
  basket distributed separately from the main data transfer.
- `-v`/`--verbose`: also show `info`-level issues (types still unhandled by
  the validator, e.g. `FORMAT`ted values).
- `-q`/`--quiet`: only print the final summary.

Exit code is `1` if any `error`-severity issue was found, `0` otherwise
(warnings/info never fail the run).

Geometry/coordinate attributes (`COORD`/`MULTICOORD`,
`POLYLINE`/`SURFACE`/`AREA`/`MULTI*`) are validated structurally and, where
the coordinate domain's axis ranges are resolvable, against their declared
`Min`/`Max` per axis.

When `--repo` is given, every model declared in the transfer's
`HEADERSECTION/MODELS` is proactively checked for resolvability against it
- not just the ones actually touched by the data - and any gap (model
missing from `--repo`, or indexed but failing to build) is reported up
front, with a resolved-count folded into the final summary line.

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

To resolve `IMPORTS` against other local models, pass a `ModelRepository`:

```python
from interlis.builder.repository import ModelRepository

repository = ModelRepository([Path("path/to/model-directory")])
builder = InterlisModelBuilder(Path("mappings"), Path("spec/grammar/mapping"), repository=repository)
model = builder.build(tree)
```

Every returned object is a Pydantic model class generated dynamically from
the IlisMeta16 metamodel (see [Architecture](#architecture) below) -
navigate it by attribute (`.Name`, `.Element`, `.ClassAttribute`, ...),
matching the metamodel's own official names.

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
uv run python3 -m pytest tests/           # ModelBuilder + XTF validator tests
```

Regenerating the vendored ANTLR grammar (only needed when
`vendor/interlis-antlr4` changes) is a maintainer-only step, not required
to use or contribute to the runtime itself - see `docs/grammar-regeneration.md`.
