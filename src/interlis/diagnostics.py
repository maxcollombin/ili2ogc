"""Shared diagnostics: one in-memory model, three renderings.

Every converter and the validator produce `Diagnostic`s into a
`DiagnosticBag`; the CLI renders that bag as Ruff-style text (stderr), as
SARIF 2.1.0 (`--output-format sarif` / `--report FILE`), and - for
`convert-sql` - the `-- NOTE` comments stay in the `.sql` as a third
rendering of the same objects.

`severity` is SARIF's own vocabulary (`error` / `warning` / `note`); the
XTF validator's historical `info` maps to `note`. `rule` is a stable id
from `interlis.diagnostic_ids.REGISTRY`. `help` names the fix, GDAL
RFC 104 style ("... - pass its model to --repo or --catalog"); it is
required in spirit for a class-C (missing-input) diagnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from interlis.diagnostic_ids import REGISTRY

_SEVERITY_ORDER = {"note": 0, "warning": 1, "error": 2}

# Rule ids that are NOT converter limitations (so not in
# `diagnostic_ids.REGISTRY`, and not A/B/C-classed) but still flow through
# the same Diagnostic/SARIF machinery - `interlis validate` data findings.
EXTRA_RULES: dict[str, str] = {
    "XTF-VALIDATION": "an .xtf value does not conform to its schema",
}


@dataclass(frozen=True)
class Location:
    """Where a diagnostic points. Every field optional - a converter often knows only the model/element."""

    file: str | None = None
    model: str | None = None
    element_path: str | None = None
    tid: str | None = None

    def render(self) -> str:
        head = self.file or ""
        tail = ".".join(p for p in (self.model, self.element_path) if p)
        joined = ":".join(p for p in (head, tail) if p)
        if self.tid:
            joined = f"{joined} [{self.tid}]" if joined else f"[{self.tid}]"
        return joined


@dataclass(frozen=True)
class Diagnostic:
    severity: str  # "error" | "warning" | "note"
    rule: str
    message: str
    location: Location = field(default_factory=Location)
    help: str | None = None
    related: tuple[Location, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_ORDER:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.rule not in REGISTRY and self.rule not in EXTRA_RULES:
            raise ValueError(f"unknown diagnostic rule {self.rule!r} - register it in interlis.diagnostic_ids")

    @property
    def klass(self) -> str:
        """A / B / C for a converter limitation; '' for a data-validation finding."""
        return REGISTRY[self.rule][0] if self.rule in REGISTRY else ""


def severity_for_class(klass: str) -> str:
    """Default severity for a limitation class: C (missing input) is actionable → warning; A/B → note."""
    return "warning" if klass == "C" else "note"


class DiagnosticBag:
    """Collect diagnostics, count them, decide the process exit code."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(self, diagnostic: Diagnostic) -> None:
        self._items.append(diagnostic)

    def extend(self, diagnostics) -> None:
        for d in diagnostics:
            self.add(d)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "note": 0}
        for d in self._items:
            out[d.severity] += 1
        return out

    def highest_severity(self) -> str | None:
        if not self._items:
            return None
        return max((d.severity for d in self._items), key=_SEVERITY_ORDER.__getitem__)

    def exit_code(self, *, strict: bool = False) -> int:
        """0 clean · 2 completed-with-degradations · 1 failed (an error, or --strict with any diagnostic)."""
        top = self.highest_severity()
        if top is None:
            return 0
        if top == "error" or strict:
            return 1
        return 2


def render_text(bag: DiagnosticBag, *, color: bool = False) -> str:
    """Ruff-style: one `SEVERITY RULE-ID  location  message` line, then an indented `help:` line."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    palette = {"error": "31", "warning": "33", "note": "36"}
    lines: list[str] = []
    for d in bag:
        loc = d.location.render()
        loc_part = f"  {loc}" if loc else ""
        lines.append(f"{paint(d.severity, palette[d.severity])} {paint(d.rule, '1')}{loc_part}  {d.message}")
        if d.help:
            lines.append(f"    help: {d.help}")
        for rel in d.related:
            rendered = rel.render()
            if rendered:
                lines.append(f"    related: {rendered}")
    counts = bag.counts()
    summary = ", ".join(f"{counts[s]} {s}{'s' if counts[s] != 1 else ''}" for s in ("error", "warning", "note"))
    lines.append(f"{len(bag)} diagnostic(s): {summary}")
    return "\n".join(lines)


_SARIF_LEVEL = {"error": "error", "warning": "warning", "note": "note"}


def render_sarif(bag: DiagnosticBag, *, tool_version: str = "0") -> dict:
    """Build a hand-rolled SARIF 2.1.0 log - no dependency; validated against the schema in tests."""
    rules_seen: dict[str, dict] = {}
    results: list[dict] = []
    for d in bag:
        if d.rule not in rules_seen:
            if d.rule in REGISTRY:
                klass, summary = REGISTRY[d.rule]
                rules_seen[d.rule] = {
                    "id": d.rule,
                    "shortDescription": {"text": summary},
                    "properties": {"limitationClass": klass},
                }
            else:
                rules_seen[d.rule] = {"id": d.rule, "shortDescription": {"text": EXTRA_RULES[d.rule]}}
        result: dict = {
            "ruleId": d.rule,
            "level": _SARIF_LEVEL[d.severity],
            "message": {"text": d.message},
        }
        locations = _sarif_locations(d.location)
        if locations:
            result["locations"] = locations
        related = [loc for loc in (_sarif_physical(r) for r in d.related) if loc]
        if related:
            result["relatedLocations"] = related
        if d.help:
            result["properties"] = {"help": d.help}
        results.append(result)
    return {
        "version": "2.1.0",
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "interlis",
                        "informationUri": "https://github.com/geostandards-ch/interlis-runtime",
                        "version": tool_version,
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": results,
            }
        ],
    }


_BUILDER_WARNING_RE = re.compile(r"^\[([A-Z0-9-]+)\]\s*(.*)$", re.DOTALL)


def builder_warnings_to_diagnostics(caught, *, file: str | None = None, include_spec_gaps: bool = False):
    """Classify `warnings.catch_warnings(record=True)` output from a builder run into `Diagnostic`s.

    Only warnings whose text carries a stable id (`[RULE-ID] ...`) are
    picked up. `BUILD-SPEC-GAP-ALT-ABSENT` is mostly benign
    optional-absent noise (7+ per ordinary model) - excluded unless
    `include_spec_gaps` is set, so an ordinary `convert` stays quiet.
    """
    out: list[Diagnostic] = []
    for warning in caught:
        text = str(warning.message)
        m = _BUILDER_WARNING_RE.match(text)
        if not m or m.group(1) not in REGISTRY:
            continue
        rule, message = m.group(1), m.group(2)
        if rule == "BUILD-SPEC-GAP-ALT-ABSENT" and not include_spec_gaps:
            continue
        out.append(Diagnostic(severity_for_class(REGISTRY[rule][0]), rule, message, Location(file=file)))
    return out


def _sarif_physical(loc: Location) -> dict | None:
    if not loc.file:
        return None
    return {"physicalLocation": {"artifactLocation": {"uri": loc.file}}}


def _sarif_locations(loc: Location) -> list[dict]:
    logical_name = ".".join(p for p in (loc.model, loc.element_path) if p)
    entry: dict = {}
    physical = _sarif_physical(loc)
    if physical:
        entry.update(physical)
    if logical_name:
        entry["logicalLocations"] = [{"fullyQualifiedName": logical_name}]
    if loc.tid:
        entry.setdefault("properties", {})["tid"] = loc.tid
    return [entry] if entry else []
