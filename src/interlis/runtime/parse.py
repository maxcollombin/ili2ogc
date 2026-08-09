"""Facade : chemin d'un fichier .ili (ou texte source brut) -> arbre ANTLR
(Lexer+Parser)."""
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
    de syntaxe). N'appelle PAS le ModelBuilder - separation lecture/construction.

    Lot 52 : repli ISO-8859-1 si le fichier n'est pas de l'UTF-8 valide -
    confirme reel (RULE #1, inspection directe des octets) sur des fichiers
    plus anciens du corpus large models.geo.admin.ch (ex. variantes
    "obsolete/*_o0.ili") : `file` les identifie "ISO-8859 text", et l'octet
    fautif (0xDC en position du mot allemand "FÜR") decode correctement en
    latin-1 mais pas en UTF-8. Le manuel de reference (eCH-0031 V2.1.0
    §3.5.1, clause CHARSET) ne fixe QUE le jeu de caracteres autorise dans
    les VALEURS transferees, jamais l'encodage sur DISQUE du fichier .ili
    source lui-meme - aucune regle a violer en repliant sur latin-1, qui ne
    peut JAMAIS lever UnicodeDecodeError (mapping 1 octet <-> 1 caractere
    sur les 256 valeurs) - pas de 3e repli necessaire."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="iso-8859-1")
    return _parse_stream(InputStream(text))


def parse_text(text: str):
    """Meme chose que `parse_file`, mais depuis du texte source en memoire -
    utilise pour le modele INTERLIS predefini (voir ModelRepository), qui
    n'existe sous forme de fichier .ili nulle part sur disque."""
    return _parse_stream(InputStream(text))
