"""Configuration loading. Config lives at ~/.config/pdfind/config.toml."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "pdfind"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "pdfind"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Bare names match a whole path segment; fragments with "/" match as substrings.
# These are dependency/build caches — thousands of vendored READMEs that would
# drown out the documents you actually wrote or received.
DEFAULT_EXCLUDES = [
    ".git", ".cache", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".npm", ".cargo", ".rustup", "site-packages", "vendor",
    "target", "dist", "build", ".next", ".tox", ".gradle", ".m2",
    "go/pkg", "Android/Sdk", ".local/share/Trash", ".local/share/pdfind",
    ".mozilla", ".steam", "snap",
]

# ~/Library alone holds tens of thousands of files no one wants to search,
# and app bundles are directories full of resources.
MACOS_EXCLUDES = [
    "Library", "Applications", ".Trash", "Pictures/Photos Library.photoslibrary",
    "Music/Music", ".vscode", "OrbStack",
]

if sys.platform == "darwin":
    DEFAULT_EXCLUDES += MACOS_EXCLUDES

DEFAULT_CONFIG = """\
# pdfind configuration

# Directories to index. "~" means your whole home directory.
roots = ["~"]

# Path fragments to skip. Matched against any part of the path.
exclude = [".git", ".cache", "node_modules", ".venv", "__pycache__", "Trash"]

# File types to index.
extensions = ["pdf", "md", "txt", "docx", "epub"]

# Embedding model. This one handles 50+ languages, so an English description
# finds a German document and vice versa.
#
# "BAAI/bge-small-en-v1.5" is a little stronger on pure English, but it is
# English-only in a way that fails quietly: it scored unrelated German text
# *higher* than a correct German translation, so German documents effectively
# become unrankable rather than merely worse.
#
# Changing this requires a full rebuild: pdfind reset && pdfind index
model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Skip files larger than this (megabytes).
max_file_mb = 50

# Run OCR (tesseract) on PDFs that yield almost no extractable text.
# Off by default: it is slow. Turn on if you have scanned documents.
ocr = false
ocr_lang = "eng"

# Characters per chunk, and overlap between consecutive chunks.
chunk_chars = 1200
chunk_overlap = 200

# Stop after this many chunks per file. Embedding runs at roughly 12 chunks/s
# on CPU, so one enormous changelog can otherwise dominate a whole re-index.
# Only the beginning of a very long file stays searchable.
max_chunks_per_file = 200

# Raw cosine score at or above which a result is treated as a real match
# rather than "the closest available text". Model-specific: measured for the
# multilingual model on this corpus. Raise it if you see confident-looking junk.
confidence_threshold = 0.53

# Combine the meaning-based ranking with a literal keyword index (SQLite
# FTS5), so that order numbers, surnames and case references find their
# document even though an embedding cannot represent them. Costs a few
# milliseconds per query. Set false to rank on the embedding alone.
hybrid = true

# Collapse byte-identical copies of the same document into one result,
# keeping the best-ranked path. Set false to see every copy separately.
deduplicate = true

# How to open a result. "auto" picks a page-aware PDF viewer if one is
# installed (zathura/okular/evince on Linux, Skim on macOS), else the system
# default. Override with a command line; {file} and {page} are substituted.
open_command = "auto"
"""


@dataclass
class Config:
    roots: list[Path] = field(default_factory=lambda: [Path.home()])
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    extensions: list[str] = field(default_factory=lambda: ["pdf", "md", "txt", "docx", "epub"])
    model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    max_file_mb: int = 50
    ocr: bool = False
    ocr_lang: str = "eng"
    chunk_chars: int = 1200
    chunk_overlap: int = 200
    max_chunks_per_file: int = 200
    confidence_threshold: float = 0.53
    hybrid: bool = True
    deduplicate: bool = True
    open_command: str = "auto"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def is_excluded(self, path: Path) -> bool:
        parts = set(path.parts)
        text = str(path)
        for frag in self.exclude:
            if not frag:
                continue
            if "/" in frag:
                if frag in text:
                    return True
            elif frag in parts:
                return True
        return False


def ensure_config() -> Path:
    """Write a default config file if none exists. Returns its path."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
    return CONFIG_PATH


def load_config() -> Config:
    ensure_config()
    try:
        with CONFIG_PATH.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{CONFIG_PATH} is not valid TOML:\n  {exc}") from None
    except OSError as exc:
        raise SystemExit(f"Cannot read {CONFIG_PATH}: {exc}") from None

    cfg = Config()
    if "roots" in raw:
        cfg.roots = [Path(r).expanduser().resolve() for r in raw["roots"]]
    if "exclude" in raw:
        # User excludes extend the built-in list rather than replacing it, so
        # nobody accidentally indexes their entire ~/.cache.
        cfg.exclude = sorted(set(DEFAULT_EXCLUDES) | set(raw["exclude"]))
    for key in ("extensions", "model", "max_file_mb", "ocr", "ocr_lang",
                "chunk_chars", "chunk_overlap", "max_chunks_per_file",
                "confidence_threshold", "hybrid", "deduplicate",
                "open_command"):
        if key in raw:
            setattr(cfg, key, raw[key])
    cfg.extensions = [e.lower().lstrip(".") for e in cfg.extensions]
    return cfg
