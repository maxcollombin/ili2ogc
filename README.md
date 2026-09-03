# ili-ogc

A pure-Python INTERLIS 2 toolkit: parses `.ili` models into a graph of typed
Python objects (the IlisMeta16 metamodel), validates `.xtf` data transfers
against them, and converts both to JSON Schema, SQL DDL (PostgreSQL/
GeoPackage), OGC JSON-FG, CQL2-JSON filters, and back to `.xtf`. `TRANSLATION
OF` models are supported throughout via `--lang` (renames identifiers/keys
in the output, the transfer wire format itself is unaffected).

## Installation

```sh
uv sync          # or: pip install .
```

The generated ANTLR lexer/parser are committed under `src/interlis/antlr/`,
so no grammar build step is needed.

## Usage

```sh
interlis build model.ili --repo models/                                 # parse + print the built model
interlis validate transfer.xtf --repo models/                           # check .xtf against its .ili schema
interlis convert model.ili -o model.schema.json                         # .ili -> JSON Schema
interlis convert-sql model.ili --dialect postgresql -o model.sql        # .ili -> SQL DDL
interlis convert-jsonfg transfer.xtf --repo models/ -o out.jsonfg.json   # .xtf -> OGC JSON-FG
interlis convert-cql2 model.ili -o model.cql2.json                      # CONSTRAINT -> CQL2-JSON filters
interlis write-xtf view-model.ili source.xtf -o view.xtf                # VIEW TOPIC data -> .xtf
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

`interlis.convert.jsonschema`/`.sql`/`.jsonfg`/`.cql2`/`.xtf_writer` expose
the same conversions as the CLI's `convert`/`convert-sql`/`convert-jsonfg`/
`convert-cql2`/`write-xtf`. Pass a `ModelRepository`
(`interlis.builder.repository`) to the builder to resolve `IMPORTS` against
other local `.ili` models.

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
