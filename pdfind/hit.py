"""The result type and snippet rendering.

Deliberately free of numpy and the model stack: the CLI and the launcher path
import only this, so talking to an already-running daemon costs no import time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Hit:
    path: str
    score: float
    page: int
    snippet: str
    n_matches: int
    relevance: float = 0.0
    """Standard deviations above the mean chunk score for this query.

    Reported for diagnostics only. It separates signal from noise well under
    an English-only model and poorly under the multilingual one, so it is not
    what `confident` is based on.
    """

    copies: int = 1
    """How many byte-identical files this result stands for."""

    via: str = "vector"
    """What put this result here.

    `vector` — the meaning of the text matched the description.
    `exact` — the document literally contains words from the query, found in
    the keyword index. Worth showing: it is the one kind of match a reader can
    verify at a glance, and it stays trustworthy at a cosine that would
    otherwise read as weak.
    `filter` — there was no description to rank by, only filters.
    """

    mtime: float = 0.0
    """The file's modification time, shown when the result came from a filter."""

    confident: bool = True
    """Whether the raw score clears the configured confidence threshold.

    Which metric is trustworthy depends on the embedding model, so the
    decision is made where the config is available rather than here.
    """

    @property
    def name(self) -> str:
        return Path(self.path).name


def snippet(text: str, query: str, width: int = 200) -> str:
    """Show the part of the chunk nearest a query word, else the opening."""
    words = re.findall(r"\w{4,}", query.lower())
    lowered = text.lower()
    pos = next((lowered.find(w) for w in words if lowered.find(w) >= 0), -1)
    if pos < 0:
        out = text[:width]
        return out.replace("\n", " ").strip() + ("…" if len(text) > width else "")
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    out = text[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + out + ("…" if end < len(text) else "")
