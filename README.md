# interlis-runtime

A pure-Python INTERLIS 2 runtime: parses `.ili` models into a graph of typed
Python objects conforming to the IlisMeta16 metamodel, validates `.xtf` data
transfers against that model, and converts both to JSON Schema and OGC
JSON-FG.

## Installation

```sh
uv sync
```

or, with pip, from a checkout: `pip install .`. The generated ANTLR
lexer/parser are already committed under `src/interlis/antlr/`, so no
separate grammar build step is needed.

## Quickstart

```sh
interlis build model.ili --repo models/                                # parse + build
interlis validate transfer.xtf --repo models/                          # validate .xtf against its schema
interlis convert model.ili -o model.schema.json                        # .ili -> JSON Schema
interlis convert-jsonfg transfer.xtf --repo models/ -o out.jsonfg.json  # .xtf -> JSON-FG
```

`--repo DIR` (repeatable) resolves `IMPORTS`/schema references against a
directory of `.ili` files, each indexed by its declared `MODEL`/
`REFSYSTEM` name rather than its filename. Run `interlis <command> --help`
for the full option reference - it is the source of truth, not duplicated
here.

## Python API

```python
from pathlib import Path
from interlis.runtime.parse import parse_file
from interlis.builder.model_builder import InterlisModelBuilder
from interlis.xtf.parse import parse_xtf
from interlis.xtf.validate import validate_transfer

tree, syntax_errors = parse_file(Path("model.ili"))
builder = InterlisModelBuilder(Path("mappings"), Path("spec/grammar/mapping"))
model = builder.build(tree)  # graph of Pydantic objects, e.g. model.Element[0].Name

transfer = parse_xtf(Path("transfer.xtf"))
issues = validate_transfer(transfer, symbol_table=builder.symbol_table)
```

Every returned model object is a Pydantic class generated dynamically from
the IlisMeta16 metamodel, navigable by its official attribute names. Pass a
`ModelRepository` (`interlis.builder.repository`) to the builder to resolve
`IMPORTS` against other local `.ili` models. Each validation `issue` carries
a `severity` (`error`/`warning`/`info`), a `message`, and enough context
(class, attribute, object TID) to locate the problem in the source data.
`interlis.convert.jsonschema`/`interlis.convert.jsonfg` expose the same
conversions as `interlis convert`/`convert-jsonfg` for use from Python -
see `cmd_convert`/`cmd_convert_jsonfg` in `src/interlis/cli.py` for how to
select the right root classes/Views before calling them.

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
(IlisMeta16 metamodel, source of truth)  │
        │                                ▼
        │                   src/interlis/convert/ (→ JSON Schema, JSON-FG)
        ▼
   graph of typed Python objects
```

`spec/grammar/mapping/*.yml` and `mappings/ilismeta16-*.yml` are the single
executable source of truth: `InterlisModelBuilder` is a generic engine
driven by this data, not hand-written Python methods per grammar rule.

## Development

```sh
uv run python3 scripts/validate_spec.py   # validates spec/grammar/mapping/ against the metamodel
uv run python3 -m pytest tests/           # ModelBuilder, XTF validator, converter tests
```

Regenerating the vendored ANTLR grammar under `vendor/interlis-antlr4` is a
maintainer-only step, needed only when that submodule changes - not required
to use or contribute to the runtime otherwise.
