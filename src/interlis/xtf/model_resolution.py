"""Resolve the root model(s) required to validate an XTF transfer.

Driven EXCLUSIVELY by the file's own content: never a Model Repository
queried live the way iliValidator does it, always the transfer's explicit
references plus a `.ili` corpus resolved locally beforehand - the same
reproducibility principle already applied to IMPORTS: "never network
during a build()".

Two distinct sources of information - don't guess, verify:
- `HEADERSECTION/MODELS` (`XtfTransfer.models`, parse.py): declares which
  models AND WHICH VERSIONS were used to produce THIS transfer - the
  canonical source for knowing WHAT to download (exact name+version+URI).
- `DATASECTION` (the baskets actually present, `XtfBasket.qualified_topic`):
  the canonical source for knowing which model(s) are ACTUALLY the
  "business" (root) model(s) of THIS specific transfer - the header ALWAYS
  also lists "core" models (Units/CoordSys/geometry/etc.) that never
  produce a basket of their own (used only as TYPES), so they can't serve
  as an entry point for `InterlisModelBuilder.build()`.
"""

from dataclasses import dataclass

from interlis.xtf.parse import XtfTransfer


def root_model_names(transfer: XtfTransfer) -> list[str]:
    """Return model names (first qualified segment) with a DATASECTION basket.

    Ordered by first appearance, deduplicated. Just one of these models is
    enough as `InterlisModelBuilder.build()`'s root: the OTHER required
    models' classes (remaining roots, or an imported core model) already
    resolve via `ModelRepository.resolve_external` (see
    xtf/schema.py:resolve_class) as long as their file is indexed by the
    same `--repo`.
    """
    seen: dict[str, None] = {}
    for basket in transfer.baskets:
        name = basket.qualified_topic.split(".", 1)[0]
        seen.setdefault(name, None)
    return list(seen)


def header_model_lookup(transfer: XtfTransfer) -> dict[str, "list[str]"]:
    """Map model name -> [version, uri] as declared in HEADERSECTION/MODELS.

    Used to produce an actionable error message - never degrade silently -
    when a root model required by the DATASECTION is missing from the
    given `--repo` directories: tells the user EXACTLY what to fetch (and
    from where), without having to re-parse the file by hand.
    """
    return {m.name: [m.version or "?", m.uri or "?"] for m in transfer.models}


@dataclass
class HeaderModelStatus:
    name: str
    version: str | None
    uri: str | None
    status: str  # "builtin" | "available" | "indexed_but_failed" | "missing" | "no_repo"


def header_completeness(transfer: XtfTransfer, repository) -> list[HeaderModelStatus]:
    """Return the resolvability status of every HEADERSECTION/MODELS entry.

    A PROACTIVE check answering "are XTFs valid against ALL their models?":
    previously, only models actually referenced by an attribute/role
    genuinely encountered during `validate_transfer` were loaded - a
    "core" model from the header never exercised by the data stayed
    invisible, whether available or not. This check inspects EVERY model
    in the header, exercised or not, independent of the DATASECTION.

    `repository=None` (no `--repo` given): every entry becomes "no_repo"
    rather than attempting anything - distinct from "missing" (which
    positively asserts that a `--repo` WAS searched and found nothing).
    """
    if repository is None:
        return [HeaderModelStatus(name=m.name, version=m.version, uri=m.uri, status="no_repo") for m in transfer.models]
    return [
        HeaderModelStatus(name=m.name, version=m.version, uri=m.uri, status=repository.availability(m.name))
        for m in transfer.models
    ]
