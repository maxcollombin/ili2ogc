# interlis-runtime

A pure-Python INTERLIS 2 runtime: parses `.ili` models into a graph of typed
Python objects (the IlisMeta16 metamodel), validates `.xtf` data transfers
against them, and converts both to JSON Schema and OGC JSON-FG.

## Installation

```sh
uv sync          # or: pip install .
```

The generated ANTLR lexer/parser are committed under `src/interlis/antlr/`,
so no grammar build step is needed.

## Quickstart

```sh
interlis build model.ili --repo models/
interlis validate transfer.xtf --repo models/
interlis convert model.ili -o model.schema.json                        # .ili -> JSON Schema
interlis convert-jsonfg transfer.xtf --repo models/ -o out.jsonfg.json  # .xtf -> JSON-FG
```

`--repo DIR` (repeatable) resolves `IMPORTS`/schema references against a
directory of `.ili` files. Run `interlis <command> --help` for every option.

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

`interlis.convert.jsonschema`/`interlis.convert.jsonfg` expose the same
conversions as the CLI's `convert`/`convert-jsonfg`. Pass a
`ModelRepository` (`interlis.builder.repository`) to the builder to resolve
`IMPORTS` against other local `.ili` models.

## Development

```sh
uv run pytest                 # test suite
uv run ruff check src/ tests/ # lint (also enforced in CI)
uv run black src/ tests/      # format
```

CI (`.github/workflows/ci.yml`) runs `ruff`, `black --check` and the test
suite on Python 3.10/3.12/3.14.

`spec/grammar/mapping/*.yml` and `mappings/ilismeta16-*.yml` (the
IlisMeta16 metamodel) are the executable source of truth: `InterlisModelBuilder`
is a generic engine driven by this data, not hand-written parsing code per
grammar rule. Regenerating the vendored grammar under
`vendor/interlis-antlr4` is a maintainer-only step, needed only when that
submodule changes.
