"""Filters: narrow results by file type, location and modification date.

Semantic ranking answers "what is this document about"; a filter answers
"which of those". People usually remember roughly *when* a document arrived or
*where* it lives even when they cannot remember a word of its contents, so the
two are complementary rather than competing.

Filters come from two places, and both end up here:

    pdfind --ext pdf --since 2024 "warehouse lease"
    pdfind "ext:pdf since:2024 warehouse lease"

The inline `key:value` form exists because the search bar and the dmenu picker
have a text entry and nothing else. It is parsed leniently — an unknown key or
an unreadable date is left in the query as ordinary text rather than raising,
because the bar re-parses on every keystroke and half-typed input must not be
an error. The command line flags are strict instead, and report what went
wrong.

Deliberately free of numpy and the model stack, for the same reason as
`hit.py`: the launcher path imports this before it knows whether it can talk to
the daemon.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

# Only these keys are consumed. Anything else ("http://…", "note: buy milk")
# stays part of the description, which is why the value may not start with a
# space: "in: Anhang" is prose, "in:Work" is a filter.
KEYS = ("ext", "type", "in", "path", "since", "after", "until", "before")

_TOKEN_RE = re.compile(
    r"\b(?P<key>" + "|".join(KEYS) + r"):(?P<value>\"[^\"]+\"|[^\s]+)",
    re.IGNORECASE,
)

_RELATIVE_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}


class FilterError(ValueError):
    """A filter value that cannot be interpreted."""


# ------------------------------------------------------------------ dates


def parse_when(value: str, *, end: bool = False) -> float:
    """Turn a date expression into a Unix timestamp.

    Accepts `2024`, `2024-03`, `2024-03-05` and relative spans like `7d`, `6w`,
    `3m`, `2y`. With `end=True` a partial date resolves to the *end* of the
    period it names, so `until:2024` includes all of 2024 rather than stopping
    on New Year's Day.
    """
    text = value.strip().strip('"').lower()
    if not text:
        raise FilterError("empty date")

    if text == "today":
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return (base + timedelta(days=1) if end else base).timestamp()
    if text == "yesterday":
        base = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=1))
        return (base + timedelta(days=1) if end else base).timestamp()

    rel = _RELATIVE_RE.match(text)
    if rel:
        days = int(rel.group(1)) * _UNIT_DAYS[rel.group(2).lower()]
        return time.time() - days * 86400

    for fmt, step in (("%Y-%m-%d", "day"), ("%Y-%m", "month"), ("%Y", "year")):
        try:
            when = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if end:
            when = _next_period(when, step)
        return when.timestamp()

    raise FilterError(
        f"{value!r} is not a date — try 2024-03-01, 2024-03, 2024, 7d, 6m")


def _next_period(when: datetime, step: str) -> datetime:
    if step == "day":
        return when + timedelta(days=1)
    if step == "month":
        return (datetime(when.year + 1, 1, 1) if when.month == 12
                else datetime(when.year, when.month + 1, 1))
    return datetime(when.year + 1, 1, 1)


def _date(stamp: float | None) -> str:
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d") if stamp else ""


# ------------------------------------------------------------------ paths


@lru_cache(maxsize=256)
def _needle(fragment: str) -> str:
    """Normalise an `in:` value into the substring to look for in a path.

    A fragment without a slash is anchored to a path component, so `in:work`
    matches `~/Work/lease.pdf` but not `~/Homework/lease.pdf`.
    """
    text = fragment.strip().strip('"')
    if text.startswith("~"):
        text = str(Path(text).expanduser())
    text = text.rstrip("/").lower()
    return text if "/" in text else "/" + text


def _like(fragment: str) -> str:
    """Escape LIKE wildcards. Underscores are common in filenames."""
    for char in ("\\", "%", "_"):
        fragment = fragment.replace(char, "\\" + char)
    return fragment


# ------------------------------------------------------------------ filter


@dataclass
class Filter:
    exts: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    since: float | None = None
    until: float | None = None

    def __bool__(self) -> bool:
        return bool(self.exts or self.paths
                    or self.since is not None or self.until is not None)

    def merge(self, other: Filter | None) -> Filter:
        """Combine flag-derived and inline filters. Both must be satisfied."""
        if other is None:
            return self
        return Filter(
            exts=list(dict.fromkeys(self.exts + other.exts)),
            paths=list(dict.fromkeys(self.paths + other.paths)),
            since=max([s for s in (self.since, other.since) if s is not None],
                      default=None),
            until=min([u for u in (self.until, other.until) if u is not None],
                      default=None),
        )

    # -- evaluation -------------------------------------------------------
    #
    # The same predicate exists twice: in Python for the vector side, which
    # scores an in-memory array, and as SQL for the keyword side, which must
    # narrow *before* SQLite applies its LIMIT or a restrictive filter would
    # return nothing at all.

    def matches(self, path: str, mtime: float) -> bool:
        if self.since is not None and mtime < self.since:
            return False
        if self.until is not None and mtime > self.until:
            return False
        if self.exts or self.paths:
            low = path.lower()
            if self.exts and not any(low.endswith(f".{e}") for e in self.exts):
                return False
            if self.paths and not any(_needle(p) in low for p in self.paths):
                return False
        return True

    def sql(self, table: str = "f") -> tuple[str, list]:
        """A WHERE fragment over the `files` table, and its parameters.

        SQLite's LOWER() is ASCII-only, so a path with an umlaut in it is
        matched case-sensitively here and case-insensitively by `matches`.
        Filter fragments are typed by hand and almost always ASCII, and the
        mismatch can only make the keyword half of a hybrid query slightly
        stricter — never wrong.
        """
        clauses: list[str] = []
        params: list = []
        if self.exts:
            clauses.append("(" + " OR ".join(
                f"LOWER({table}.path) LIKE ? ESCAPE '\\'" for _ in self.exts) + ")")
            params += [f"%.{_like(e)}" for e in self.exts]
        if self.paths:
            clauses.append("(" + " OR ".join(
                f"LOWER({table}.path) LIKE ? ESCAPE '\\'" for _ in self.paths) + ")")
            params += [f"%{_like(_needle(p))}%" for p in self.paths]
        if self.since is not None:
            clauses.append(f"{table}.mtime >= ?")
            params.append(self.since)
        if self.until is not None:
            clauses.append(f"{table}.mtime <= ?")
            params.append(self.until)
        return " AND ".join(clauses), params

    # -- presentation and transport ---------------------------------------

    def describe(self) -> str:
        parts = []
        if self.exts:
            parts.append("/".join(self.exts))
        if self.paths:
            parts.append("in " + ", ".join(p.strip('"') for p in self.paths))
        if self.since is not None:
            parts.append(f"since {_date(self.since)}")
        if self.until is not None:
            parts.append(f"until {_date(self.until)}")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {"exts": self.exts, "paths": self.paths,
                "since": self.since, "until": self.until}

    @classmethod
    def from_dict(cls, raw: dict | None) -> Filter:
        if not raw:
            return cls()
        return cls(exts=list(raw.get("exts") or []),
                   paths=list(raw.get("paths") or []),
                   since=raw.get("since"), until=raw.get("until"))


def parse(query: str) -> tuple[Filter, str]:
    """Split `key:value` filters out of a query. Returns (filter, remaining).

    Never raises: a value that makes no sense is left in the query text, so
    typing `since:letz` in the search bar searches for those characters instead
    of erroring halfway through the word.
    """
    flt = Filter()
    kept: list[str] = []
    last = 0
    for match in _TOKEN_RE.finditer(query):
        key = match.group("key").lower()
        value = match.group("value").strip('"')
        try:
            if key in ("ext", "type"):
                exts = [e.lower().lstrip(".") for e in value.split(",") if e.strip(".")]
                if not exts:
                    raise FilterError("no extension")
                flt.exts += exts
            elif key in ("in", "path"):
                flt.paths.append(value)
            elif key in ("since", "after"):
                flt.since = parse_when(value)
            else:
                flt.until = parse_when(value, end=True)
        except FilterError:
            continue  # leave the token in the query as ordinary words
        kept.append(query[last:match.start()])
        last = match.end()
    kept.append(query[last:])
    return flt, " ".join("".join(kept).split())
