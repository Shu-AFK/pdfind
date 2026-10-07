"""Index storage: SQLite for metadata, a flat float32 file for vectors.

Vectors live in vecs.f32 as an append-only array of `dim` float32 rows; each
chunk row records its offset. Deleting a file tombstones its rows, which
`pdfind compact` reclaims. At a few hundred thousand chunks a brute-force
numpy dot product is still sub-100ms, so there is no ANN index to maintain.

SQLite also carries the keyword half of the search. The chunk text is already
stored here, so an FTS5 index over it costs nothing but its own postings —
see `keyword_rows`.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import numpy as np

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    mtime     REAL NOT NULL,
    size      INTEGER NOT NULL,
    n_pages   INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL,
    sha       TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    page    INTEGER NOT NULL,
    row     INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS chunks_row  ON chunks(row);
CREATE TABLE IF NOT EXISTS free_rows (row INTEGER PRIMARY KEY);
"""

# An external-content FTS5 index: the text lives in `chunks` and is not copied,
# so this adds postings only. The triggers keep it in step with the writes the
# indexer already makes; `chunks.text` is never updated in place (a changed
# file has its chunks deleted and re-inserted), so no UPDATE trigger is needed.
#
# `remove_diacritics 2` folds umlauts, which matters for a corpus that is
# half German: without it "Grundstuck" would not find "Grundstück".
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
"""

_WORD_RE = re.compile(r"\w+", re.UNICODE)

# Beyond this many words a description is rambling, and every extra term costs
# a postings-list walk.
MAX_TERMS = 24


def fts_query(text: str) -> str:
    """Turn a description into an FTS5 MATCH expression.

    Terms are OR-ed, because a description is not a boolean query: "hazard and
    toxicity tables" should still reach a chunk that only says "toxicity".
    BM25 does the discriminating — a chunk carrying the rare words outranks one
    carrying only the common ones.

    A word the tokeniser splits further stays a phrase. "41-L0EL0085" is one
    thing, not a 41 and an L0EL0085; searched apart, the "41" alone drags in
    every page that happens to contain the number.

    Terms ending in four or more characters match as prefixes. That is what
    makes the keyword half useful in the live search bar before you finish
    typing, and it doubles as crude morphology: "miete*" reaches "Mieten",
    "hazard*" reaches "hazardous". Shorter ones stay exact, or "in*" would
    match everything.
    """
    terms = []
    for word in text.lower().split():
        parts = _WORD_RE.findall(word)
        if not parts or (len(parts) == 1 and len(parts[0]) < 2):
            continue
        phrase = '"' + " ".join(parts) + '"'
        terms.append(phrase + "*" if len(parts[-1]) >= 4 else phrase)
        if len(terms) == MAX_TERMS:
            break
    return " OR ".join(terms)


class Store:
    def __init__(self, dim: int, data_dir: Path | None = None):
        self.dir = data_dir or DATA_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.vec_path = self.dir / "vecs.f32"
        # The daemon answers queries on handler threads while the connection is
        # created on the main one. Its own lock serialises every search, so
        # SQLite never sees two callers at once.
        self.db = sqlite3.connect(self.dir / "index.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.fts = self._enable_fts()
        self.db.commit()
        self.vec_path.touch(exist_ok=True)

    def _migrate(self) -> None:
        """Add columns introduced after an index was first built.

        Runs after the schema script, so anything depending on a migrated
        column (such as the index on `sha`) belongs here, not in SCHEMA.
        """
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(files)")}
        if "sha" not in columns:
            self.db.execute("ALTER TABLE files ADD COLUMN sha TEXT")
        self.db.execute("CREATE INDEX IF NOT EXISTS files_sha ON files(sha)")

    def _enable_fts(self) -> bool:
        """Create the keyword index, and say whether it is usable.

        A few Python builds ship a SQLite without FTS5. That is not worth
        failing over — search falls back to vectors alone, and `pdfind doctor`
        reports it — so the failure is caught rather than raised.
        """
        existed = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone()
        try:
            self.db.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            return False
        if not existed:
            # An index built before hybrid search has all the chunk text but no
            # postings. Building them reads what is already in SQLite: seconds,
            # not the minutes a re-embedding would take.
            self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            self.db.commit()
        return True

    # -- metadata ---------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.db.commit()

    # -- vectors ----------------------------------------------------------

    def n_rows(self) -> int:
        return self.vec_path.stat().st_size // (self.dim * 4)

    def vectors(self) -> np.ndarray:
        """Memory-mapped view of every stored row (including tombstones)."""
        n = self.n_rows()
        if n == 0:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.memmap(self.vec_path, dtype=np.float32, mode="r", shape=(n, self.dim))

    def _take_free_rows(self, count: int) -> list[int]:
        rows = [r[0] for r in self.db.execute(
            "SELECT row FROM free_rows LIMIT ?", (count,)).fetchall()]
        if rows:
            self.db.executemany("DELETE FROM free_rows WHERE row=?",
                                [(r,) for r in rows])
        return rows

    def append_vectors(self, vecs: np.ndarray) -> list[int]:
        """Store vectors, reusing tombstoned rows first. Returns row indices."""
        vecs = np.ascontiguousarray(vecs, dtype=np.float32)
        assigned: list[int] = []
        reuse = self._take_free_rows(len(vecs))

        if reuse:
            with self.vec_path.open("r+b") as fh:
                for row, vec in zip(reuse, vecs):
                    fh.seek(row * self.dim * 4)
                    fh.write(vec.tobytes())
            assigned.extend(reuse)

        remaining = vecs[len(reuse):]
        if len(remaining):
            start = self.n_rows()
            with self.vec_path.open("ab") as fh:
                fh.write(remaining.tobytes())
            assigned.extend(range(start, start + len(remaining)))
        return assigned

    # -- files ------------------------------------------------------------

    def file_state(self) -> dict[str, tuple[int, float, int]]:
        """path -> (id, mtime, size) for every indexed file."""
        return {p: (i, m, s) for i, p, m, s in
                self.db.execute("SELECT id, path, mtime, size FROM files")}

    def delete_file(self, file_id: int) -> None:
        rows = self.db.execute("SELECT row FROM chunks WHERE file_id=?", (file_id,))
        self.db.executemany("INSERT OR IGNORE INTO free_rows(row) VALUES(?)",
                            [(r[0],) for r in rows])
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        self.db.execute("DELETE FROM files WHERE id=?", (file_id,))

    def add_file(self, path: str, mtime: float, size: int, n_pages: int,
                 chunks: list[tuple[int, str]], vecs: np.ndarray,
                 indexed_at: float, sha: str | None = None) -> None:
        cur = self.db.execute(
            "INSERT INTO files(path,mtime,size,n_pages,indexed_at,sha) "
            "VALUES(?,?,?,?,?,?)",
            (path, mtime, size, n_pages, indexed_at, sha),
        )
        file_id = cur.lastrowid
        rows = self.append_vectors(vecs)
        self.db.executemany(
            "INSERT INTO chunks(file_id,page,row,text) VALUES(?,?,?,?)",
            [(file_id, page, row, text)
             for (page, text), row in zip(chunks, rows)],
        )

    def chunk_lookup(self) -> dict[int, tuple[str, int, str]]:
        """row -> (path, page, text) for scoring search hits."""
        return {row: (path, page, text) for row, path, page, text in self.db.execute(
            "SELECT c.row, f.path, c.page, c.text FROM chunks c "
            "JOIN files f ON f.id = c.file_id")}

    def keyword_rows(self, query: str, limit: int,
                     where: str = "", params: list | None = None) -> list[int]:
        """Vector rows of the chunks matching `query` literally, best first.

        This is the half of the search an embedding cannot do. A 384-dimension
        vector has no room for an order number: "41-L0EL0085" is not a concept,
        and the nearest neighbours of its embedding are other strings that
        merely look alphanumeric. Here it is one postings lookup.

        Any filter is applied in SQL rather than to the result, or a narrow
        filter would just empty out the LIMIT.
        """
        match = fts_query(query)
        if not self.fts or not match:
            return []
        sql = ("SELECT c.row FROM chunks_fts "
               "JOIN chunks c ON c.id = chunks_fts.rowid "
               "JOIN files f ON f.id = c.file_id "
               "WHERE chunks_fts MATCH ?")
        if where:
            sql += f" AND {where}"
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        try:
            return [row[0] for row in
                    self.db.execute(sql, (match, *(params or []), limit))]
        except sqlite3.OperationalError:
            return []  # unparseable MATCH expression: vectors still answer

    def file_mtimes(self) -> dict[str, float]:
        """path -> modification time, for filtering at query time."""
        return dict(self.db.execute("SELECT path, mtime FROM files"))

    def list_files(self, where: str = "", params: list | None = None,
                   limit: int = 20) -> list[tuple[str, float, str]]:
        """Newest indexed files first, as (path, mtime, opening text).

        Serves a query that is nothing but filters — "the PDFs I got this
        week" — where there is no description to rank by.
        """
        sql = ("SELECT f.path, f.mtime, "
               "(SELECT c.text FROM chunks c WHERE c.file_id = f.id AND c.page > 0 "
               " ORDER BY c.id LIMIT 1) "
               "FROM files f")
        if where:
            sql += f" WHERE {where}"
        sql += " ORDER BY f.mtime DESC LIMIT ?"
        return [(path, mtime, text or "")
                for path, mtime, text in
                self.db.execute(sql, (*(params or []), limit))]

    def path_shas(self) -> dict[str, str]:
        """path -> content hash, for collapsing duplicates at search time."""
        return {path: sha for path, sha in self.db.execute(
            "SELECT path, sha FROM files WHERE sha IS NOT NULL")}

    def backfill_shas(self, hasher) -> int:
        """Hash files indexed before the column existed. No re-embedding."""
        rows = self.db.execute(
            "SELECT id, path FROM files WHERE sha IS NULL").fetchall()
        done = 0
        for file_id, path in rows:
            sha = hasher(path)
            if sha:
                self.db.execute("UPDATE files SET sha=? WHERE id=?", (sha, file_id))
                done += 1
        if done:
            self.db.commit()
        return done

    def stats(self) -> dict[str, int]:
        n_files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        n_chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_free = self.db.execute("SELECT COUNT(*) FROM free_rows").fetchone()[0]
        return {"files": n_files, "chunks": n_chunks,
                "rows": self.n_rows(), "dead_rows": n_free}

    def compact(self) -> int:
        """Rewrite the vector file without tombstones. Returns rows reclaimed."""
        dead = self.db.execute("SELECT COUNT(*) FROM free_rows").fetchone()[0]
        if not dead:
            return 0
        old = self.vectors()
        live = self.db.execute("SELECT id, row FROM chunks ORDER BY row").fetchall()
        tmp = self.vec_path.with_suffix(".f32.tmp")
        with tmp.open("wb") as fh:
            for _, row in live:
                fh.write(np.ascontiguousarray(old[row]).tobytes())
        del old
        self.db.executemany("UPDATE chunks SET row=? WHERE id=?",
                            [(new, cid) for new, (cid, _) in enumerate(live)])
        self.db.execute("DELETE FROM free_rows")
        tmp.replace(self.vec_path)
        self.db.commit()
        self.db.execute("VACUUM")
        return dead

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
