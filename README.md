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

Every returned object is a Pydantic model class generated dynamically from
the IlisMeta16 metamodel (see Architecture below) - navigate it by
attribute (`.Name`, `.Element`, `.ClassAttribute`, ...), matching the
metamodel's own official names.

## Known limitations

- **One `.ili` file at a time**: an `IMPORTS` clause referencing another
  model produces an unresolved named reference (`UnresolvedNamedReference`)
  rather than an error - multi-file resolution is out of scope for this
  first version.
- **Formal constraints are not evaluated**: `CONSTRAINT`/`MANDATORY
  CONSTRAINT` clauses are built as data (an expression tree), never
  executed against real data.
- **Topic-level default OID** (`OID AS <domain>;` without `BASKET`,
  applied to every class in the topic that doesn't declare its own OID
  clause) is not yet wired - it needs a post-processing pass over all
  classes in a topic, which the current engine doesn't perform yet.

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
