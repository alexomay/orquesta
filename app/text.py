"""Normalización y tokenización de texto en español (sin dependencias externas)."""

import re
import unicodedata

_STOP = (
    "a al algo ante antes como con contra cual cuando de del desde donde durante e el ella "
    "ellos en entre era es esa ese eso esta este esto estoy fue ha hace hacia han hasta hay "
    "la las le les lo los me mi mis mucho muy nada ni no nos o otra otro para pero poco por "
    "porque que quien se sea segun ser si sin sobre son su sus tambien tan te tengo ti tu tus "
    "un una uno unos y ya yo hola favor ayuda buen buenos dia dias hoy puedo quiero necesito "
    "algun alguna alguien me mio mia"
)
STOPWORDS = frozenset(_STOP.split())

# Sufijos comunes; un stemmer ligero basta para unir plural/singular y conjugaciones simples.
# Orden de más largo a más corto: se aplica el primer sufijo que coincida.
_SUFFIX_TEXT = (
    "amientos imientos amiento imiento aciones iciones acion icion amente mente ando iendo "
    "adas ados idas idos ada ado ida ido ar er ir es as os a o e s"
)
_SUFFIXES = tuple(_SUFFIX_TEXT.split())


def norm(s: str) -> str:
    """Minúsculas y sin acentos: 'Contraseña' -> 'contrasena'."""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", norm(text))
    return [stem(w) for w in words if w not in STOPWORDS and len(w) > 1]
