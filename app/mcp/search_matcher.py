"""
Matching determinista de ``search_schema``: sin LLM, sin embeddings, sin dependencias.

Es una función PURA sobre texto en memoria. Nada de lo que se calcula acá llega al motor: la
consulta del agente jamás se concatena en SQL (ni siquiera parametrizada) porque no hay SQL — el
catálogo se lee entero (acotado) por los métodos del façade de solo lectura y se filtra en
proceso. Eso es lo que hace que ``%``, ``_`` o comillas en la consulta sean texto inerte.

NORMALIZACIÓN
-------------
Se parte en tokens ANTES de plegar mayúsculas: ``customerID2`` → ``customer``, ``id``, ``2``;
``HTTPServer`` → ``http``, ``server``; ``fecha_de_alta`` → ``fecha``, ``de``, ``alta``. Después se
quitan los acentos (NFKD sin marcas combinantes) y se aplica ``casefold``: ``Dirección`` y
``direccion`` son el mismo token, en nombres y en comentarios.

Un "stem" mínimo (quitar una ``s`` final en tokens de más de 3 letras) cubre el plural de español
e inglés sin diccionario: ``clientes``/``cliente``, ``orders``/``order``. Se aplica a ambos lados,
así que la coincidencia es simétrica. No es lematización y no pretende serlo: lo que no cubre
(``mujer``/``mujeres``) simplemente no matchea.

QUÉ ES UN MATCH
---------------
AND: todos los tokens de la consulta (sin las palabras vacías) tienen que aparecer. Un token de
la consulta coincide con uno del candidato si son iguales o —desde 3 letras— si es PREFIJO
(``cli`` encuentra ``cliente``). Los tokens pueden repartirse entre el nombre del objeto, el
nombre de su tabla y su comentario, pero al menos uno tiene que caer en el nombre PROPIO o en el
comentario PROPIO: una columna no matchea solo porque su tabla se llama como la consulta.

RANKING (el puntaje es un entero, a mayor mejor)
------------------------------------------------
Tablas, vistas, rutinas y triggers:  nombre exacto 1000 > prefijo del nombre 800 > todos los
tokens en el nombre 600. Columnas, siempre por DEBAJO de cualquier nombre de tabla:  exacto 460 >
prefijo 440 > tokens en el nombre 420 > tokens repartidos entre columna y tabla 410. Comentarios,
por debajo de todo: 200 + 10 por token que aporta el comentario (+20 si el comentario los trae
a todos). El desempate es estable (ver ``sort_key``): nombre más corto, tipo, tabla, columna.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Cota de texto que se tokeniza por campo. Un comentario de 64 KiB no tiene por qué costar más
#: CPU que uno de 2 KiB: lo que sigue después del tope no participa del match.
_MAX_TEXT_CHARS = 4000

#: Palabras vacías de español e inglés. Solo se descartan de la CONSULTA, y solo si queda al menos
#: un token con contenido: ``fecha de nacimiento del cliente`` busca ``fecha nacimiento cliente``,
#: pero ``de`` solo se busca tal cual.
_STOPWORDS = frozenset(
    {
        "a", "al", "con", "de", "del", "el", "en", "la", "las", "lo", "los", "para", "por",
        "un", "una", "uno", "y", "o", "que", "se", "su", "sus",
        "an", "and", "as", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the",
        "to", "with",
    }
)  # fmt: skip

_SCORE_NAME = {"exact": 1000, "prefix": 800, "tokens": 600}
_SCORE_COLUMN = {"exact": 460, "prefix": 440, "tokens": 420, "with_table": 410}
_SCORE_COMMENT_BASE = 200
_KIND_RANK = {"table": 0, "view": 1, "column": 2, "routine": 3, "trigger": 4}


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokenize(text: str | None) -> list[str]:
    """Tokens plegados (minúsculas, sin acentos) de un nombre, una frase o un comentario."""
    if not text:
        return []
    s = _strip_accents(str(text)[:_MAX_TEXT_CHARS])
    tokens: list[str] = []
    cur: list[str] = []
    prev = ""
    for i, ch in enumerate(s):
        if not ch.isalnum():
            if cur:
                tokens.append("".join(cur))
                cur = []
            prev = ""
            continue
        if cur:
            nxt = s[i + 1] if i + 1 < len(s) else ""
            boundary = (
                (prev.islower() and ch.isupper())
                or (prev.isdigit() != ch.isdigit())
                or (prev.isupper() and ch.isupper() and nxt.islower())
            )
            if boundary:
                tokens.append("".join(cur))
                cur = []
        cur.append(ch)
        prev = ch
    if cur:
        tokens.append("".join(cur))
    return [t.casefold() for t in tokens]


def stem(token: str) -> str:
    """Quita la ``s`` final de un plural. ``ss`` y las palabras de hasta 3 letras no se tocan."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _token_hits(q: str, candidates: frozenset[str]) -> bool:
    if q in candidates:
        return True
    return len(q) >= 3 and any(c.startswith(q) for c in candidates)


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    #: Tokens plegados y sin repetir, sin palabras vacías (salvo que todas lo sean).
    tokens: tuple[str, ...]
    stems: tuple[str, ...]
    #: Todos los tokens (con vacías) con stem, unidos: es lo que se compara con el nombre completo
    #: para decidir "exacto" y "prefijo".
    phrase: str


def parse_query(query: str) -> ParsedQuery | None:
    """``None`` si la consulta no produce ningún token (solo signos, por ejemplo)."""
    todos = tokenize(query)
    if not todos:
        return None
    unicos: list[str] = []
    for t in todos:
        if t not in unicos:
            unicos.append(t)
    utiles = [t for t in unicos if t not in _STOPWORDS] or unicos
    return ParsedQuery(
        tokens=tuple(utiles),
        stems=tuple(stem(t) for t in utiles),
        phrase=" ".join(stem(t) for t in todos),
    )


@dataclass(frozen=True, slots=True)
class Match:
    score: int
    matched_on: str  # name | name_and_table | comment | name_and_comment
    tokens: tuple[str, ...]


def score_entry(
    q: ParsedQuery,
    *,
    name: str,
    parent: str | None = None,
    comment: str | None = None,
    is_column: bool = False,
) -> Match | None:
    """
    Puntúa un candidato contra la consulta, o ``None`` si no matchea (AND sobre todos los tokens).

    ``parent`` es el nombre de la tabla de una columna; ``comment`` es el del propio objeto.
    """
    name_stems = [stem(t) for t in tokenize(name)]
    name_set = frozenset(name_stems)
    parent_set = frozenset(stem(t) for t in tokenize(parent))
    comment_set = frozenset(stem(t) for t in tokenize(comment))

    in_name = [_token_hits(s, name_set) for s in q.stems]
    in_parent = [_token_hits(s, parent_set) for s in q.stems]
    in_comment = [_token_hits(s, comment_set) for s in q.stems]

    if not all(n or p or c for n, p, c in zip(in_name, in_parent, in_comment, strict=True)):
        return None

    if all(in_name):
        phrase = " ".join(name_stems)
        if phrase == q.phrase:
            nivel = "exact"
        elif phrase.startswith(q.phrase):
            nivel = "prefix"
        else:
            nivel = "tokens"
        tabla = _SCORE_COLUMN if is_column else _SCORE_NAME
        return Match(tabla[nivel], "name", q.tokens)

    if parent is not None and any(in_name):
        if all(n or p for n, p in zip(in_name, in_parent, strict=True)):
            return Match(_SCORE_COLUMN["with_table"], "name_and_table", q.tokens)

    # El comentario aporta lo que el nombre no cubre. Si ningún token lo aporta el comentario,
    # el único vínculo sería la tabla: una columna no matchea por llamarse su tabla como la
    # consulta.
    n_aporta = sum(1 for n, c in zip(in_name, in_comment, strict=True) if c and not n)
    if n_aporta == 0:
        return None
    score = _SCORE_COMMENT_BASE + 10 * n_aporta + (20 if all(in_comment) else 0)
    return Match(score, "name_and_comment" if any(in_name) else "comment", q.tokens)


def sort_key(score: int, name: str, kind: str, table: str | None, column: str | None):
    """Orden total y estable: mejor puntaje, nombre más corto, tipo, tabla, columna, nombre."""
    return (-score, len(name), _KIND_RANK.get(kind, 9), table or "", column or "", name)
