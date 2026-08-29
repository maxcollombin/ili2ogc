"""Load spec/grammar/mapping/*.yml into a single {rule_name: SpecEntry} dict.

Merges the 9 themed files.
"""

from pathlib import Path

import yaml

from interlis.spec.models import SpecEntry


def load_raw_spec(spec_dir: Path) -> tuple[dict[str, dict], list[str]]:
    """Load the 9 files into a single {rule: raw entry (dict)} dict.

    Also returns the list of cross-file duplicate-rule messages (the first
    occurrence of a duplicated rule is kept, later ones ignored) instead of
    raising on the first one - it's up to the caller to decide whether to
    stop or continue (the CLI validator wants to see every problem in a
    single pass).
    """
    merged: dict[str, dict] = {}
    origin: dict[str, str] = {}
    duplicates: list[str] = []
    for path in sorted(spec_dir.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rule, entry in data.items():
            if rule in merged:
                duplicates.append(f"{rule}: present in both {origin[rule]} and {path.name} (cross-file duplicate)")
                continue
            merged[rule] = entry
            origin[rule] = path.name
    return merged, duplicates


def load_spec(spec_dir: Path) -> dict[str, SpecEntry]:
    """Load and validate (Pydantic) the 9 files.

    Raises pydantic.ValidationError on the first schema problem,
    ValueError on a cross-file duplicate - used by the ModelBuilder, which
    needs a guaranteed-valid spec before it starts building.
    """
    raw, duplicates = load_raw_spec(spec_dir)
    if duplicates:
        raise ValueError("; ".join(duplicates))
    return {rule: SpecEntry(**entry) for rule, entry in raw.items()}
