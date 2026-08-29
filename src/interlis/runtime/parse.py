"""Facade: an .ili file path (or raw source text) -> an ANTLR tree.

Runs the Lexer+Parser.
"""

import re
from pathlib import Path

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from interlis.antlr.InterlisLexer import InterlisLexer
from interlis.antlr.InterlisParser import InterlisParser

_META_ATTRIBUTE_PREFIX = "!!@"
# eCH-0117 SS4.2 Escape = '\' ('"' | '\' | 'u' HexDigit HexDigit HexDigit HexDigit)
_ESCAPE_RE = re.compile(r'\\(["\\]|u[0-9a-fA-F]{4})')


class SyntaxErrorCollector(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"line {line}:{column} {msg}")


def _parse_stream(stream):
    lexer = InterlisLexer(stream)
    tokens = CommonTokenStream(lexer)
    parser = InterlisParser(tokens)

    collector = SyntaxErrorCollector()
    parser.removeErrorListeners()
    parser.addErrorListener(collector)

    tree = parser.interlis2def()
    return tree, collector.errors


def parse_file(path: Path):
    """Parse an .ili file, return (interlis2def tree, syntax error list).

    Does NOT call the ModelBuilder - keeps reading and building separate.

    Falls back to ISO-8859-1 if the file isn't valid UTF-8 - some older
    files in the wild (e.g. "obsolete/*_o0.ili" variants) are ISO-8859
    text, not UTF-8. The reference manual (eCH-0031 V2.1.0 §3.5.1, CHARSET
    clause) only fixes the character set allowed in transferred VALUES,
    never the on-disk encoding of the .ili source file itself, so falling
    back to latin-1 breaks no rule. latin-1 can never raise
    UnicodeDecodeError (a 1 byte <-> 1 character mapping over all 256
    values), so no further fallback is needed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="iso-8859-1")
    return _parse_stream(InputStream(text))


def parse_text(text: str):
    """Do the same as `parse_file`, but from in-memory source text.

    Used for the predefined INTERLIS model (see ModelRepository), which
    exists as an .ili file nowhere on disk.
    """
    return _parse_stream(InputStream(text))


def _unquote_meta_attribute_value(value: str) -> str:
    r"""Strip eCH-0117 SS4.1 String quoting/escapes, if present.

    `Value = Metaattributename | String` - a bare (unquoted) value is
    returned as-is; a `"..."` value has its `\"`/`\\`/`\uXXXX` escapes
    resolved.
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value
    inner = value[1:-1]
    return _ESCAPE_RE.sub(
        lambda m: m.group(1) if m.group(1) in ('"', "\\") else chr(int(m.group(1)[1:], 16)),
        inner,
    )


def meta_attribute_comments(text: str) -> list[tuple[int, str, str]]:
    r"""Return every eCH-0117 `!!@Name=Value` meta-attribute comment in `text`.

    eCH-0117 ("Meta-attributs pour modeles INTERLIS") formalizes `!!@...`
    as an INTERLIS line comment (`SingleLineComment : '!!' ~[\r\n]* ->
    channel(HIDDEN);`, vendor/interlis-antlr4/InterlisLexer.g4) whose 3rd
    character is `@` - the grammar itself never sees these (they're on
    ANTLR's hidden channel, invisible to the 121 mapped parser rules), but
    the lexer does NOT discard them (`channel(HIDDEN)`, not `-> skip`), so
    a second, independent lex-only pass over the same source recovers them
    fully, with exact line numbers - no grammar/parser change needed.

    Returns `(line, name, value)` triples in source order (one per
    `Name=Value` pair - a single comment can carry several, separated by
    `;`: `!!@a=1;b=2`). An ordinary `!!` comment (no `@`) is not a
    meta-attribute per eCH-0117 SS3/SS4 and is excluded. Positioning
    (which built instance a given triple actually belongs to - "the first
    following language construct", eCH-0117 SS3) is NOT decided here -
    see InterlisModelBuilder._attach_pending_meta_attributes.
    """
    stream = InputStream(text)
    lexer = InterlisLexer(stream)
    tokens = CommonTokenStream(lexer)
    tokens.fill()
    results: list[tuple[int, str, str]] = []
    for token in tokens.tokens:
        if token.channel == 0 or token.text is None or not token.text.startswith(_META_ATTRIBUTE_PREFIX):
            continue
        body = token.text[len(_META_ATTRIBUTE_PREFIX) :]
        for pair in body.split(";"):
            if "=" not in pair:
                continue
            name, _, raw_value = pair.partition("=")
            name = name.strip()
            if not name:
                continue
            results.append((token.line, name, _unquote_meta_attribute_value(raw_value.strip())))
    return results


def meta_attribute_comments_in_file(path: Path) -> list[tuple[int, str, str]]:
    """Same as `meta_attribute_comments`, reading `path` (same encoding fallback as `parse_file`)."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="iso-8859-1")
    return meta_attribute_comments(text)
