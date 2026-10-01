"""Deterministic keyphrase extraction (ARCHITECTURE §12.1, pass 1).

**No model.** Term frequency over a document version's evidence text, weighted by
inverse document frequency across the corpus, with a domain stoplist. The reason
is reproducibility, stated plainly in §12.1: the same corpus must produce the
same cloud on a re-run, or a reviewer cannot tell a data change from a model
change. An embedding model that silently re-clusters between runs makes the word
cloud unfalsifiable.

The domain stoplist is the part that does the real work. In a corpus that is
*entirely* about coal production, the words ``coal``, ``production``, ``tonnes``
and ``limited`` appear in nearly every document and therefore carry no
information about any of them — IDF alone already pushes them down, but with a
few hundred documents it does not push far enough, and a cloud whose largest word
is "coal" has told the reader nothing they did not know before opening it.

Pass 2 — embedding clustering into named topics — is optional and lives in
:mod:`mrip.topics.cluster`. Absent the ``ml`` extra, phrases stand alone and the
feature degrades rather than breaking, the same rule as OCR (§10) and narrative
answers (§13).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence

__all__ = [
    "DOMAIN_STOPWORDS",
    "STOPWORDS",
    "Keyphrase",
    "extract_keyphrases",
    "tokenize",
]

#: Ordinary English function words. Kept short and explicit rather than pulled
#: from a library: a dependency that changes its stoplist between versions would
#: change every cloud in the corpus without a line of our code moving.
#:
#: Written as whitespace-delimited prose rather than a list literal (SIM905) so
#: a reviewer can scan and amend a stoplist in the shape a stoplist is normally
#: read; the literal form is one 1,200-character line nobody will proofread.
_ENGLISH_STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been
    before being below between both but by can cannot could did do does doing down
    during each few for from further had has have having he her here hers herself
    him himself his how i if in into is it its itself me more most my myself no nor
    not of off on once only or other ought our ours ourselves out over own same she
    should so some such than that the their theirs them themselves then there these
    they this those through to too under until up very was we were what when where
    which while who whom why with would you your yours yourself yourselves
    """.split()  # noqa: SIM905 — a stoplist reads as prose, not as a list literal
)

#: Words this corpus is *about*, which therefore say nothing about any single
#: document in it. ARCHITECTURE §12.1 names the first four; the rest are the
#: boilerplate that every CIL statement carries — headers, units written long,
#: and the vocabulary of a filing rather than of its subject.
DOMAIN_STOPWORDS = frozenset(
    """
    coal production limited tonnes tonne mining mine mines india indian company
    corporation ltd lt total sub grand annual monthly month year years figures
    figure table page annexure annexures statement statements report reports
    performance target targets actual achievement percentage percent growth
    cumulative quantity qty value values unit units lakh lakhs crore crores
    million tons mt te number no sl sr note notes source sources
    subsidiary subsidiaries overall provisional revised during period periods
    """.split()  # noqa: SIM905 — see above
)

#: What extraction removes. The union is taken once here rather than at every
#: call, because this runs per document over a corpus.
STOPWORDS = _ENGLISH_STOPWORDS | DOMAIN_STOPWORDS

#: A term worth indexing. Letters only, at least four characters: shorter tokens
#: in this corpus are overwhelmingly column codes and abbreviations that mean
#: nothing out of their table. Hyphens and apostrophes are kept inside a word so
#: ``over-burden`` and ``company's`` survive as one token.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]{3,}")

#: Below this many occurrences a term is noise or an OCR artefact — one mangled
#: glyph sequence appearing once should never reach the cloud.
_MIN_OCCURRENCES = 2


class Keyphrase:
    """One extracted term, with the two numbers the cloud needs.

    A plain class rather than a Pydantic model: this is an internal intermediate
    produced in bulk per document, and validation on every instance would cost
    more than it buys. The Pydantic boundary is :class:`~mrip.schemas.CloudTerm`,
    which is what leaves the API.
    """

    __slots__ = ("occurrences", "score", "term")

    def __init__(self, term: str, occurrences: int, score: float) -> None:
        self.term = term
        self.occurrences = occurrences
        self.score = score

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"Keyphrase({self.term!r}, n={self.occurrences}, s={self.score:.4f})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Keyphrase):
            return NotImplemented
        return (
            self.term == other.term
            and self.occurrences == other.occurrences
            and math.isclose(self.score, other.score, rel_tol=1e-9)
        )

    def as_row(self, document_id: str, document_version: int) -> dict[str, object]:
        """Shaped for :meth:`KeyphraseRepository.replace_for_document`."""
        return {
            "document_id": document_id,
            "document_version": document_version,
            "term": self.term,
            "occurrences": self.occurrences,
            "score": self.score,
        }


def tokenize(text: str) -> list[str]:
    """Lowercased content words, stoplisted.

    Deliberately simple: no stemmer. Stemming would merge ``mining`` into
    ``mine`` and ``despatches`` into ``despatch``, which reads like an
    improvement until a reviewer clicks a term and finds documents that never
    contain the word they clicked. The cloud promises that every term is
    literally in the documents behind it.
    """
    return [
        token
        for raw in _TOKEN.findall(text)
        if (token := raw.lower().strip("-'")) not in STOPWORDS and len(token) >= 4
    ]


def extract_keyphrases(
    texts: Iterable[str],
    *,
    document_frequencies: dict[str, int] | None = None,
    corpus_size: int = 1,
    limit: int = 60,
) -> list[Keyphrase]:
    """Rank one document version's terms by TF-IDF.

    ``document_frequencies`` maps a term to the number of documents in the corpus
    containing it, and ``corpus_size`` is how many documents that is. Both are
    passed in rather than queried here, so this function stays pure and testable
    — the same inputs always give the same ranking, which is the §12.1 property.

    With no corpus statistics supplied this degrades to term frequency alone,
    which is the correct behaviour for the first document ever ingested: there is
    nothing yet to be relatively rare *against*.
    """
    counts = Counter(token for text in texts for token in tokenize(text))
    if not counts:
        return []

    frequencies = document_frequencies or {}
    total_documents = max(corpus_size, 1)
    longest = max(counts.values())

    ranked: list[Keyphrase] = []
    for term, occurrences in counts.items():
        if occurrences < _MIN_OCCURRENCES:
            continue
        # Sublinear term frequency, normalized by the document's own most common
        # term: a 400-page annual report should not outrank a 2-page statement
        # on every term merely by being longer.
        tf = (1.0 + math.log(occurrences)) / (1.0 + math.log(longest))
        # Smoothed IDF. The +1s keep a term that appears in every document at a
        # small positive weight rather than exactly zero, so a document whose
        # every term is common still produces a cloud instead of nothing.
        idf = math.log((1.0 + total_documents) / (1.0 + frequencies.get(term, 0))) + 1.0
        ranked.append(Keyphrase(term, occurrences, tf * idf))

    # Score descending, then term ascending. The tiebreak is not cosmetic: two
    # runs over the same corpus must emit the same rows in the same order, or the
    # "deterministic" claim is only true up to dictionary iteration order.
    ranked.sort(key=lambda phrase: (-phrase.score, phrase.term))
    return ranked[:limit]


def document_frequencies(documents: Sequence[Sequence[str]]) -> dict[str, int]:
    """Count how many documents contain each term.

    Takes already-tokenized documents. Used by the corpus-wide recompute; the
    per-document job reads the same numbers from the database instead.
    """
    frequencies: Counter[str] = Counter()
    for tokens in documents:
        frequencies.update(set(tokens))
    return dict(frequencies)
