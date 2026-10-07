"""Walk the configured roots and build/refresh the index incrementally.

Files are processed in slices: each slice is extracted in parallel (poppler and
tesseract are subprocesses, so this is I/O bound and threads are enough), then
all of the slice's chunks are embedded in a few large batches. Batching matters
a lot — one `.embed()` call per file spends more time on thread-pool setup than
on the actual model.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .embed import Embedder
from .extract import chunk_pages, extract
from .store import Store

_SPLIT_RE = re.compile(r"[_\-.]+")

FILES_PER_SLICE = 64
EMBED_BATCH = 128
EXTRACT_WORKERS = min(8, (os.cpu_count() or 4))


HASH_BLOCK = 1 << 20


def file_sha(path: str | Path) -> str | None:
    """Hash of the file's bytes, used to collapse duplicate copies.

    Deliberately the raw bytes rather than the extracted text: every file that
    yields no text (scans, empty files) would otherwise hash identically and be
    merged into one result.
    """
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            while block := fh.read(HASH_BLOCK):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


@dataclass
class Extracted:
    path: Path
    mtime: float
    size: int
    n_pages: int
    chunks: list[tuple[int, str]]
    sha: str | None = None
    failed: bool = False


def walk(cfg: Config) -> Iterator[Path]:
    exts = {f".{e}" for e in cfg.extensions}
    seen: set[Path] = set()
    for root in cfg.roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            # Prune excluded and hidden directories in place.
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and not cfg.is_excluded(here / d)
            ]
            for name in filenames:
                path = here / name
                if path.suffix.lower() not in exts or cfg.is_excluded(path):
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield path


def title_text(path: Path) -> str:
    """A synthetic chunk from the path, so filenames are searchable too."""
    words = _SPLIT_RE.sub(" ", path.stem)
    folder = _SPLIT_RE.sub(" ", path.parent.name)
    return f"{words} ({folder} folder, {path.suffix.lstrip('.')} document)"


def _extract_one(path: Path, cfg: Config) -> Extracted | None:
    try:
        st = path.stat()
    except OSError:
        return None
    try:
        pages = extract(path, ocr=cfg.ocr, ocr_lang=cfg.ocr_lang)
    except Exception:
        return Extracted(path, st.st_mtime, st.st_size, 0, [], failed=True)

    chunks = chunk_pages(pages, cfg.chunk_chars, cfg.chunk_overlap,
                         cfg.max_chunks_per_file)
    chunks.insert(0, (0, title_text(path)))  # page 0 = the filename chunk
    return Extracted(path, st.st_mtime, st.st_size, len(pages), chunks,
                     file_sha(path))


def _slices(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def reindex(cfg: Config, *, force: bool = False, progress=None) -> dict[str, int]:
    embedder = Embedder(cfg.model)
    store = Store(embedder.dim)

    recorded = store.get_meta("model")
    if recorded not in (None, cfg.model):
        raise SystemExit(
            f"Index was built with model {recorded!r}, config says {cfg.model!r}.\n"
            f"Run 'pdfind reset' to rebuild from scratch."
        )
    store.set_meta("model", cfg.model)

    known = store.file_state()
    stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0, "failed": 0}
    on_disk: set[str] = set()
    todo: list[Path] = []

    for path in walk(cfg):
        key = str(path)
        on_disk.add(key)
        try:
            st = path.stat()
        except OSError:
            continue
        if st.st_size > cfg.max_file_bytes:
            stats["skipped"] += 1
            continue
        prior = known.get(key)
        if prior and not force:
            _, mtime, size = prior
            if abs(mtime - st.st_mtime) < 1e-6 and size == st.st_size:
                stats["skipped"] += 1
                continue
        todo.append(path)

    done = 0
    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        for batch in _slices(todo, FILES_PER_SLICE):
            results = [r for r in pool.map(lambda p: _extract_one(p, cfg), batch)
                       if r is not None]

            usable = []
            for res in results:
                done += 1
                if progress:
                    progress(res.path, done, len(todo))
                if res.failed:
                    stats["failed"] += 1
                elif res.chunks:
                    usable.append(res)

            texts = [t for res in usable for _, t in res.chunks]
            if not texts:
                continue
            try:
                vecs = embedder.embed_passages(texts, batch_size=EMBED_BATCH)
            except Exception:
                stats["failed"] += len(usable)
                continue

            offset = 0
            for res in usable:
                n = len(res.chunks)
                key = str(res.path)
                prior = known.get(key)
                if prior:
                    store.delete_file(prior[0])
                    stats["updated"] += 1
                else:
                    stats["added"] += 1
                store.add_file(key, res.mtime, res.size, res.n_pages,
                               res.chunks, vecs[offset:offset + n], time.time(),
                               res.sha)
                offset += n
            store.commit()

    for key, (file_id, _, _) in known.items():
        if key not in on_disk:
            store.delete_file(file_id)
            stats["removed"] += 1

    # Files indexed before the sha column existed only need hashing, not
    # re-embedding, so this stays cheap.
    store.backfill_shas(file_sha)

    store.set_meta("indexed_at", str(time.time()))
    store.commit()

    if store.stats()["dead_rows"] > 5000:
        store.compact()

    store.close()
    return stats
