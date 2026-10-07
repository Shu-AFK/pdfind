"""Query the index: embed the description, score chunks, group by file.

Two rankings are produced for every query and combined into one. The vector side
knows what the text *means* — it is the reason "warehouse lease" finds a
document that only says "rental contract for storage premises". The keyword
side knows what the text *says* — it is the reason an order number, a surname
or a case reference still finds its document, which a 384-dimension embedding
of that string never reliably does.

The obvious way to combine them is reciprocal rank fusion, and it was tried
first: each side contributes 1/(K+rank), positions only, no incomparable
numbers to weight. On this corpus it ranks worse than either half alone,
because it throws away the one thing the cosine carries — magnitude. A 0.605
match and a 0.503 match sit at ranks 0 and 1, which RRF prices 0.2% apart; the
0.503 document then wins on nothing but having more mediocre chunks. That
magnitude is also what `confidence_threshold` and every score in the output
mean, so discarding it here would leave the ranking and the displayed numbers
telling different stories.

So cosine scoring stays exactly as it was, and the keyword index contributes a
bounded boost on top of it. How large that boost is depends on the query, which
is the one judgement call here. Measured on this corpus: given "41-L0EL0085",
the vector side confidently returns a page of `Aa0Aa1Aa2` at cosine 0.79 — the
nearest neighbours of a string the model cannot represent are just other
strings that look alphanumeric — and only the keyword index knows anything, so
it decides. Given "Reisenotizen Australien", the keyword side confidently
returns the one file that mentions the word in passing while the vector side
has the right answer at rank 1, so a literal match is worth a nudge and no
more. What separates the two is the query itself: a code is not a concept, and
`_is_literal` says which one you typed.

With no literal match, or with `hybrid = false`, the ranking is bit-for-bit
what it was before any of this existed.
"""

from __future__ import annotations

import numpy as np

from .config import Config
from .embed import Embedder
from .filters import Filter
from .filters import parse as parse_filters
from .hit import Hit, snippet
from .store import Store

# How much a second/third strong chunk in the same file adds to its score.
# Documents that match in several places should outrank a one-line fluke.
SUPPORT_WEIGHT = 0.15
MAX_SUPPORT = 3

# Chunks pulled from the keyword index per query. It only has to be deep enough
# that the per-file fold has something to work with.
KEYWORD_CANDIDATES = 200

# The literal-match boost, in cosine points, for the best-matching chunk of a
# file. KEYWORD_LEAD is deliberately larger than the whole cosine range: when
# the query names a code, a document containing it beats one that merely reads
# like it, whatever the embedding thinks.
#
# KEYWORD_SUPPORT is the value for an ordinary description, and it is small on
# purpose. Swept over this corpus, 0.03 pulls literal matches *into* the
# results without once changing which document comes first; from 0.05 it starts
# reordering documents a hundredth apart, and by 0.15 it puts a passing mention
# of "Reisenotizen" above the travel notes themselves. Corroboration is worth a
# place in the list, not the top of it.
KEYWORD_LEAD = 1.0
KEYWORD_SUPPORT = 0.03

# Keyword rank at which the boost has halved, so that the tenth-best literal
# match is not worth what the best one is.
KEYWORD_DECAY = 5.0

# Stands in for "no literal match", and sorts behind every real keyword rank.
NO_KEYWORD = 1 << 30


def _is_literal(word: str) -> bool:
    """Whether a word is a code rather than a concept.

    Order numbers, case references and scan filenames mix letters and digits,
    or are long runs of digits. Those are the queries an embedding answers with
    confident nonsense and a keyword index answers exactly. A bare year does
    not count: "invoice 2024" is still a description.
    """
    alpha = any(c.isalpha() for c in word)
    digit = any(c.isdigit() for c in word)
    return (alpha and digit) or (digit and len(word) >= 6)


class Searcher:
    """Holds the model and index in memory; reusable across queries."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.embedder = Embedder(cfg.model)
        self.store = Store(self.embedder.dim)
        self._vecs: np.ndarray | None = None
        self._lookup: dict[int, tuple[str, int, str]] | None = None
        self._shas: dict[str, str] | None = None
        self._mtimes: dict[str, float] = {}
        self._paths: list[str] = []
        self._path_ids: np.ndarray | None = None
        self._rows: np.ndarray | None = None

    def load(self) -> None:
        self._vecs = self.store.vectors()
        self._lookup = self.store.chunk_lookup()
        self._shas = self.store.path_shas()
        self._mtimes = self.store.file_mtimes()

        # Filtering is decided per file (thousands) and then fanned out to
        # chunks (hundreds of thousands) with a single gather through
        # `_path_ids`, so a filtered query costs about what an unfiltered one
        # does. Both arrays are parallel to the iteration order of `_lookup`.
        index: dict[str, int] = {}
        paths: list[str] = []
        n = len(self._lookup)
        rows = np.empty(n, dtype=np.int64)
        path_ids = np.empty(n, dtype=np.int32)
        for i, (row, (path, _, _)) in enumerate(self._lookup.items()):
            pid = index.get(path)
            if pid is None:
                pid = index[path] = len(paths)
                paths.append(path)
            rows[i] = row
            path_ids[i] = pid
        self._paths, self._path_ids, self._rows = paths, path_ids, rows

    def search(self, query: str, limit: int = 10, min_score: float = 0.0,
               filters: Filter | None = None) -> list[Hit]:
        if self._vecs is None or self._lookup is None:
            self.load()
        assert self._vecs is not None and self._lookup is not None
        assert self._rows is not None and self._path_ids is not None

        # Inline `ext:pdf since:2024` filters are parsed here rather than in the
        # CLI so that every entry point gets them: the search bar and the dmenu
        # picker have a text entry and no flags.
        flt, query = parse_filters(query)
        flt = flt.merge(filters)

        if not query:
            return self._listing(flt, limit) if flt else []
        if not self._lookup:
            return []

        rows = self._rows
        if flt:
            keep = np.array([flt.matches(p, self._mtimes.get(p, 0.0))
                             for p in self._paths], dtype=bool)
            if not keep.any():
                return []
            rows = rows[keep[self._path_ids]]

        qvec = self.embedder.embed_query(query)
        scores = self._vecs @ qvec
        live_scores = scores[rows]

        take = min(len(rows), max(limit * 20, 200))
        top = np.argpartition(-live_scores, take - 1)[:take]
        top = top[np.argsort(-live_scores[top])]

        # The corpus-wide spread for this query is the yardstick that makes
        # relevance comparable between different queries.
        mean = float(live_scores.mean())
        std = max(float(live_scores.std()), 1e-6)

        where, params = flt.sql() if flt else ("", [])
        keyword: list[int] = []
        if self.cfg.hybrid:
            keyword = self.store.keyword_rows(query, KEYWORD_CANDIDATES,
                                              where, params)
        by_code = any(_is_literal(word) for word in query.split())
        boost = KEYWORD_LEAD if by_code else KEYWORD_SUPPORT
        literal = {row: rank for rank, row in enumerate(keyword)
                   if row in self._lookup}  # rows indexed since the last load

        # Candidates from both sides, each carrying its cosine and, where it has
        # one, its position in the keyword ranking.
        candidates: dict[int, int] = {int(rows[i]): NO_KEYWORD for i in top}
        for row, rank in literal.items():
            candidates[row] = rank

        per_file: dict[str, list[tuple[float, int, str, int]]] = {}
        for row, kw_rank in candidates.items():
            path, page, text = self._lookup[row]
            per_file.setdefault(path, []).append(
                (float(scores[row]), page, text, kw_rank))

        # `Hit.score` is the plain cosine of the chunk shown, so the number, the
        # confidence threshold and --min-score all refer to the same quantity.
        # The boost only moves results around.
        threshold = self.cfg.confidence_threshold
        ranked: list[tuple[float, Hit]] = []
        for path, chunks in per_file.items():
            chunks.sort(key=lambda c: -c[0])
            best_kw = min(c[3] for c in chunks)
            # For a code query the page that literally contains the code is the
            # one worth showing, even if another page of the same file reads
            # more like the query. Ties fall to the higher cosine, the list
            # being sorted by it.
            if by_code and best_kw < NO_KEYWORD:
                shown = min(range(len(chunks)), key=lambda i: chunks[i][3])
            else:
                shown = 0
            best, page, text, _ = chunks[shown]
            # The badge is claimed only for a query that named a code. Under an
            # ordinary description nearly every result contains some word of it
            # literally, so saying so would be noise — and it is exactly the
            # code case where a low cosine is still the right answer.
            exact = by_code and best_kw < NO_KEYWORD
            # Which is also why --min-score does not apply to those: it is a
            # cutoff on how much to trust a number that, here, is not what the
            # result rests on. The same reasoning exempts them from `(weak)`.
            if best < min_score and not exact:
                continue

            support = [c[0] for i, c in enumerate(chunks)
                       if i != shown][:MAX_SUPPORT]
            rank = best + SUPPORT_WEIGHT * sum(support) / MAX_SUPPORT
            if best_kw < NO_KEYWORD:
                rank += boost / (1 + best_kw / KEYWORD_DECAY)
            ranked.append((rank, Hit(
                path=path, score=best, page=page,
                snippet=snippet(text, query), n_matches=len(chunks),
                relevance=(best - mean) / std, mtime=self._mtimes.get(path, 0.0),
                via="exact" if exact else "vector",
                confident=best >= threshold or exact,
            )))

        ranked.sort(key=lambda pair: -pair[0])
        if self.cfg.deduplicate:
            ranked = self._collapse_duplicates(ranked)
        return [hit for _, hit in ranked[:limit]]

    def _listing(self, flt: Filter, limit: int) -> list[Hit]:
        """Answer a query that is only filters, newest file first.

        "ext:pdf since:7d" is a reasonable thing to ask on its own, and there is
        nothing to rank it by, so recency stands in for relevance and the score
        column gives way to the date.
        """
        where, params = flt.sql()
        rows = self.store.list_files(where, params, limit * 4 if self.cfg.deduplicate
                                     else limit)
        ranked = [(-mtime, Hit(path=path, score=0.0, page=0,
                               snippet=snippet(text, ""), n_matches=1,
                               mtime=mtime, via="filter", confident=True))
                  for path, mtime, text in rows]
        if self.cfg.deduplicate:
            ranked = self._collapse_duplicates(ranked)
        return [hit for _, hit in ranked[:limit]]

    def _collapse_duplicates(
        self, ranked: list[tuple[float, Hit]]
    ) -> list[tuple[float, Hit]]:
        """Keep one entry per distinct file content.

        The same document often sits in several places (a copy in Downloads, a
        synced folder, a second drive). Those are one document to a person, so
        the best-ranked path represents the group and the rest only bump the
        `copies` count. Files with no recorded hash are never merged.
        """
        if self._shas is None:
            self._shas = self.store.path_shas()
        if not self._shas:
            return ranked

        kept: list[tuple[float, Hit]] = []
        seen: dict[str, Hit] = {}
        for rank, hit in ranked:
            sha = self._shas.get(hit.path)
            if sha is None:
                kept.append((rank, hit))
                continue
            first = seen.get(sha)
            if first is None:
                seen[sha] = hit
                kept.append((rank, hit))
            else:
                first.copies += 1
        return kept
