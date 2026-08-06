"""Facade : chemin d'un fichier .ili (ou texte source brut) -> arbre ANTLR
(Lexer+Parser)."""
from pathlib import Path

from antlr4 import CommonTokenStream, FileStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from interlis.antlr.InterlisLexer import InterlisLexer
from interlis.antlr.InterlisParser import InterlisParser


class SyntaxErrorCollector(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: list[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"ligne {line}:{column} {msg}")


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
    """Parse un fichier .ili, retourne (arbre interlis2def, liste d'erreurs
    de syntaxe). N'appelle PAS le ModelBuilder - separation lecture/construction."""
    return _parse_stream(FileStream(str(path), encoding="utf-8"))


def parse_text(text: str):
    """Meme chose que `parse_file`, mais depuis du texte source en memoire -
    utilise pour le modele INTERLIS predefini (voir ModelRepository), qui
    n'existe sous forme de fichier .ili nulle part sur disque."""
    return _parse_stream(InputStream(text))
