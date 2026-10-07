"""Preview rendering for the search bar.

PDFs get the matching page rasterised by poppler, which is the only preview
that actually answers "is this the right document?" at a glance. Everything
else falls back to the page's full text, fetched from the daemon.

Both are cached on disk keyed by (path, mtime, page), so moving the selection
back and forth does not re-render.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", "~/.cache")
).expanduser() / "pdfind" / "previews"

RENDER_DPI = 110
RENDER_TIMEOUT = 20


def _key(path: str, page: int) -> str:
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        mtime = 0.0
    raw = f"{path}\x00{mtime}\x00{page}\x00{RENDER_DPI}"
    return hashlib.sha1(raw.encode()).hexdigest()


def render_pdf_page(path: str, page: int) -> Path | None:
    """Rasterise one page to PNG. Returns the cached file, or None."""
    if not path.lower().endswith(".pdf"):
        return None
    page = max(1, page)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = CACHE_DIR / f"{_key(path, page)}.png"
    if out.exists():
        return out

    prefix = out.with_suffix("")
    try:
        subprocess.run(
            ["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(RENDER_DPI),
             "-png", "-singlefile", path, str(prefix)],
            capture_output=True, timeout=RENDER_TIMEOUT, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    return out if out.exists() else None


def page_text(path: str, page: int, fallback: str = "") -> str:
    """Full text of the matching page, via the daemon; snippet if unavailable."""
    from .daemon import page_context

    text = page_context(path, page)
    if text:
        return text
    if fallback:
        return fallback
    try:
        from .extract import extract
        pages = extract(Path(path))
    except Exception:
        return ""
    for number, body in pages:
        if number == page:
            return body[:8000]
    return pages[0][1][:8000] if pages else ""


def prune_cache(max_files: int = 400) -> None:
    """Drop the oldest renders so the cache cannot grow without bound."""
    if not CACHE_DIR.exists():
        return
    files = sorted(CACHE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for stale in files[:-max_files]:
        stale.unlink(missing_ok=True)
