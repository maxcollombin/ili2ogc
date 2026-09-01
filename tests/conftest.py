"""Shared model-construction helpers for the test suite.

Every test that needs a built `InterlisModelBuilder` used to redefine its own
near-identical `_build` (parse -> `InterlisModelBuilder` -> `build()`, known
spec-gap warnings suppressed) locally - see `docs/testing-strategy.md`
("Existing technical debt" #1/#2) for why that duplication existed and what
it cost. These four functions are the canonical versions; a test file whose
own `_build` needs a different signature or return shape (an extra
parameter, `(builder, model)` instead of just `builder`) keeps a thin local
wrapper that delegates to one of these rather than reimplementing the
parse/build/warnings boilerplate itself.

pytest makes this module importable as `from conftest import ...` from any
sibling test file (no `tests/__init__.py`, default "prepend" import mode) -
this is not a fixture module, these are plain functions.
"""

import warnings
from pathlib import Path
from typing import Any

from interlis.builder.model_builder import InterlisModelBuilder
from interlis.runtime.parse import parse_file, parse_text

ROOT = Path(__file__).resolve().parent.parent
MAPPINGS_DIR = ROOT / "mappings"
SPEC_DIR = ROOT / "spec/grammar/mapping"


def build_from_text(
    src: str,
    *,
    repository: Any = None,
    meta_attributes: list[tuple[int, str, str]] | None = None,
) -> InterlisModelBuilder:
    """Parse `src` as a `.ili` snippet and build it, suppressing known spec-gap warnings."""
    tree, errors = parse_text(src)
    assert not errors, f"unexpected syntax errors: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attributes)
    return builder


def build_from_text_with_model(
    src: str,
    *,
    repository: Any = None,
    meta_attributes: list[tuple[int, str, str]] | None = None,
) -> tuple[InterlisModelBuilder, Any]:
    """Same as `build_from_text`, also returning `builder.build()`'s own return value."""
    tree, errors = parse_text(src)
    assert not errors, f"unexpected syntax errors: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = builder.build(tree, meta_attributes=meta_attributes)
    return builder, model


def build_from_file(
    path: Path,
    *,
    repository: Any = None,
    meta_attributes: list[tuple[int, str, str]] | None = None,
) -> InterlisModelBuilder:
    """Parse a `.ili` fixture file and build it, suppressing known spec-gap warnings."""
    tree, errors = parse_file(path)
    assert not errors, f"unexpected syntax errors in {path}: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        builder.build(tree, meta_attributes=meta_attributes)
    return builder


def build_from_file_with_model(
    path: Path,
    *,
    repository: Any = None,
) -> tuple[InterlisModelBuilder, Any]:
    """Same as `build_from_file`, also returning `builder.build()`'s own return value."""
    tree, errors = parse_file(path)
    assert not errors, f"unexpected syntax errors in {path}: {errors}"
    builder = InterlisModelBuilder(MAPPINGS_DIR, SPEC_DIR, repository=repository)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = builder.build(tree)
    return builder, model
