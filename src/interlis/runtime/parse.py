"""Facade: an .ili file path (or raw source text) -> an ANTLR tree.

Runs the Lexer+Parser.
"""
from pathlib import Path

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from interlis.antlr.InterlisLexer import InterlisLexer
from interlis.antlr.InterlisParser import InterlisParser


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
