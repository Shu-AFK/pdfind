"""Text extraction. Returns a list of (page_number, text) pairs.

Everything here shells out to poppler/tesseract or uses the stdlib, so the
indexer needs no PDF or Office parsing libraries.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

Pages = list[tuple[int, str]]

# Below this many characters a PDF page is treated as image-only.
OCR_TRIGGER_CHARS = 40

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def clean(text: str) -> str:
    text = text.replace("\x00", " ").replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


def extract(path: Path, *, ocr: bool = False, ocr_lang: str = "eng") -> Pages:
    ext = path.suffix.lower().lstrip(".")
    if ext == "pdf":
        return _pdf(path, ocr=ocr, ocr_lang=ocr_lang)
    if ext == "docx":
        return _docx(path)
    if ext == "epub":
        return _epub(path)
    return _plain(path)


def _plain(path: Path) -> Pages:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [(1, clean(text))] if text.strip() else []


def _pdf(path: Path, *, ocr: bool, ocr_lang: str) -> Pages:
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", "-q", str(path), "-"],
            capture_output=True, timeout=120,
        ).stdout.decode("utf-8", errors="replace")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    pages = [(i, clean(t)) for i, t in enumerate(out.split("\f"), start=1)]
    pages = [(n, t) for n, t in pages if t]

    if ocr and sum(len(t) for _, t in pages) < OCR_TRIGGER_CHARS:
        return _pdf_ocr(path, ocr_lang)
    return pages


def _pdf_ocr(path: Path, lang: str) -> Pages:
    """Rasterise each page and run tesseract. Slow; only for scanned PDFs."""
    pages: Pages = []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                ["pdftoppm", "-r", "200", "-png", str(path), f"{tmp}/p"],
                capture_output=True, timeout=600, check=True,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return []
        for n, img in enumerate(sorted(Path(tmp).glob("p*.png")), start=1):
            try:
                res = subprocess.run(
                    ["tesseract", str(img), "stdout", "-l", lang],
                    capture_output=True, timeout=300,
                )
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
            text = clean(res.stdout.decode("utf-8", errors="replace"))
            if text:
                pages.append((n, text))
    return pages


def _docx(path: Path) -> Pages:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError):
        return []
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []

    paras = []
    for p in root.iter(f"{ns}p"):
        runs = [t.text for t in p.iter(f"{ns}t") if t.text]
        if runs:
            paras.append("".join(runs))
    text = clean("\n".join(paras))
    return [(1, text)] if text else []


def _epub(path: Path) -> Pages:
    """Each XHTML document becomes one 'page', in spine order where possible."""
    try:
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist()
                     if n.lower().endswith((".xhtml", ".html", ".htm"))]
            names.sort()
            pages: Pages = []
            for n, name in enumerate(names, start=1):
                try:
                    raw = z.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
                text = clean(_TAG_RE.sub(" ", raw))
                if text:
                    pages.append((n, text))
    except (zipfile.BadZipFile, OSError):
        return []
    return pages


def chunk_pages(pages: Pages, size: int, overlap: int,
                max_chunks: int = 0) -> list[tuple[int, str]]:
    """Split page text into overlapping windows, keeping the page number.

    `max_chunks` (0 = unlimited) bounds the embedding cost of a single huge
    file; the tail of such a file is left unindexed.
    """
    step = max(1, size - overlap)
    chunks: list[tuple[int, str]] = []
    for page, text in pages:
        if max_chunks and len(chunks) >= max_chunks:
            break
        if len(text) <= size:
            chunks.append((page, text))
            continue
        for start in range(0, len(text), step):
            piece = text[start:start + size].strip()
            if len(piece) > 50:
                chunks.append((page, piece))
            if start + size >= len(text) or (max_chunks and len(chunks) >= max_chunks):
                break
    return chunks
