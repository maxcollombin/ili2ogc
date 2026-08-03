"""Facade : chemin d'un fichier .ili -> arbre ANTLR (Lexer+Parser)."""
from pathlib import Path

from antlr4 import CommonTokenStream, FileStream
from antlr4.error.ErrorListener import ErrorListener

from interlis.antlr.InterlisLexer import InterlisLexer
from interlis.antlr.InterlisParser import InterlisParser


class SyntaxErrorCollector(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"ligne {line}:{column} {msg}")


def parse_file(path: Path):
    """Parse un fichier .ili, retourne (arbre interlis2def, liste d'erreurs
    de syntaxe). N'appelle PAS le ModelBuilder - separation lecture/construction."""
    stream = FileStream(str(path), encoding="utf-8")
    lexer = InterlisLexer(stream)
    tokens = CommonTokenStream(lexer)
    parser = InterlisParser(tokens)

    collector = SyntaxErrorCollector()
    parser.removeErrorListeners()
    parser.addErrorListener(collector)

    tree = parser.interlis2def()
    return tree, collector.errors
