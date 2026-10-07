"""Command line interface."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import daemon, env
from .config import CONFIG_PATH, Config, ensure_config, load_config
from .filters import Filter, FilterError, parse_when
from .hit import Hit

BOLD, DIM, CYAN, GREEN, RESET = "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[0m"


def _colour(stream=sys.stdout) -> bool:
    return stream.isatty()


def home_relative(path: str) -> str:
    try:
        rel = Path(path).relative_to(Path.home())
    except ValueError:
        return path
    return "~" if str(rel) == "." else f"~/{rel}"


# ---------------------------------------------------------------- opening


def open_hit(hit: Hit, cfg: Config) -> None:
    page = max(1, hit.page)
    if cfg.open_command and cfg.open_command != "auto":
        cmd = cfg.open_command.format(file=shlex.quote(hit.path), page=page)
        subprocess.Popen(cmd, shell=True, start_new_session=True)
        return
    env.open_file(hit.path, page)


# ---------------------------------------------------------------- querying


def run_search(query: str, limit: int, cfg: Config, min_score: float = 0.0,
               filters: Filter | None = None) -> list[Hit]:
    """Ask the daemon first; fall back to loading the model in-process."""
    hits = daemon.search_via_daemon(query, limit, min_score, filters)
    if hits is not None:
        return hits
    from .search import Searcher  # only now do we pay for numpy + the model
    return Searcher(cfg).search(query, limit=limit, min_score=min_score,
                                filters=filters)


def filters_from_args(args) -> Filter:
    """Build a filter from the command line flags.

    Unlike the inline `since:…` form, which is typed live and stays lenient, a
    flag that cannot be read is an error worth stopping for.
    """
    flt = Filter()
    for value in getattr(args, "ext", None) or []:
        flt.exts += [e.strip().lower().lstrip(".")
                     for e in value.split(",") if e.strip(". ")]
    flt.paths += list(getattr(args, "within", None) or [])
    for flag, end in (("since", False), ("until", True)):
        value = getattr(args, flag, None)
        if not value:
            continue
        try:
            setattr(flt, flag, parse_when(value, end=end))
        except FilterError as exc:
            raise SystemExit(f"--{flag}: {exc}") from None
    return flt


# ---------------------------------------------------------------- commands


def cmd_index(args, cfg: Config) -> int:
    from .indexer import reindex

    started = time.time()
    tty = _colour(sys.stderr)

    def progress(path: Path, done: int, total: int) -> None:
        if not tty:
            return
        line = f"  [{done}/{total}] {home_relative(str(path))}"
        width = shutil.get_terminal_size().columns - 1
        print(f"\r\033[K{line[:width]}", end="", file=sys.stderr, flush=True)

    print(f"Indexing {', '.join(home_relative(str(r)) for r in cfg.roots)} "
          f"({', '.join(cfg.extensions)})…", file=sys.stderr)
    stats = reindex(cfg, force=args.force, progress=progress)
    if tty:
        print("\r\033[K", end="", file=sys.stderr)

    elapsed = time.time() - started
    print(f"{GREEN if tty else ''}Done in {elapsed:.1f}s{RESET if tty else ''} — "
          f"{stats['added']} added, {stats['updated']} updated, "
          f"{stats['removed']} removed, {stats['skipped']} unchanged/skipped, "
          f"{stats['failed']} failed", file=sys.stderr)
    daemon.notify_reload()
    return 0


def _where(hit: Hit) -> str:
    """The column between the name and the path: score and page, or the date.

    A filtered listing has no score to show — nothing was ranked — so the
    modification time takes the place of both.
    """
    if hit.via == "filter":
        return _day(hit.mtime)
    loc = f"p.{hit.page}" if hit.page > 0 else "filename"
    return f"{hit.score:.3f}  {loc}"


def _day(stamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp)) if stamp else ""


def cmd_search(args, cfg: Config) -> int:
    flt = filters_from_args(args)
    hits = run_search(args.query, args.limit, cfg, args.min_score, flt)
    if not hits:
        if flt or ":" in args.query:
            print("No matches with those filters.", file=sys.stderr)
        else:
            print("No matches. Has the index been built? Try: pdfind index",
                  file=sys.stderr)
        return 1

    tty = _colour()
    if args.paths_only:
        for hit in hits:
            print(hit.path)
        return 0

    if tty and not hits[0].confident:
        print(f"{DIM}Nothing stands out strongly — these may just be the "
              f"closest available text.{RESET}\n", file=sys.stderr)

    for i, hit in enumerate(hits, start=1):
        if tty:
            if hit.via == "exact":
                mark = f" {GREEN}exact{RESET}"
            elif hit.confident:
                mark = ""
            else:
                mark = f" {DIM}(weak){RESET}"
            if hit.copies > 1:
                mark += f" {DIM}({hit.copies} copies){RESET}"
            print(f"{BOLD}{i:2}. {hit.name}{RESET}  {DIM}{_where(hit)}{RESET}{mark}")
            print(f"    {CYAN}{home_relative(hit.path)}{RESET}")
            print(f"    {DIM}{hit.snippet}{RESET}\n")
        else:
            loc = f"p.{hit.page}" if hit.page > 0 else "filename"
            print(f"{hit.relevance:.2f}\t{hit.score:.3f}\t{hit.path}\t{loc}\t"
                  f"{hit.snippet}")
    return 0


def cmd_pick(args, cfg: Config) -> int:
    """Search, choose a result in fzf (with preview), then open it."""
    if not shutil.which("fzf"):
        print("fzf is not installed.", file=sys.stderr)
        return 1

    hits = run_search(args.query, args.limit, cfg, args.min_score,
                      filters_from_args(args))
    if not hits:
        print("No matches.", file=sys.stderr)
        return 1

    lines = []
    for i, hit in enumerate(hits):
        loc = f"p.{hit.page}" if hit.page > 0 else "name"
        mark = "" if hit.confident else " (weak)"
        if hit.via == "exact":
            mark = " (exact)"
        elif hit.via == "filter":
            loc = _day(hit.mtime)
        if hit.copies > 1:
            mark += f" ({hit.copies} copies)"
        head = "" if hit.via == "filter" else f"{hit.score:.3f}  "
        lines.append(f"{i}\t{head}{hit.name}  [{loc}]{mark}  "
                     f"{DIM}{home_relative(hit.path)}{RESET}")

    # fzf's preview runs a shell command per row, so the snippets go to a
    # side file keyed by row index rather than being crammed into the line.
    preview_data = Path(tempfile.mkdtemp(prefix="pdfind-")) / "snippets"
    preview_data.write_text(
        "\n".join(f"{i}\x1f{h.path}\x1f{h.snippet}" for i, h in enumerate(hits))
    )
    preview = (
        f"awk -F'\\037' -v k={{1}} '$1==k {{printf \"%s\\n\\n%s\\n\", $2, $3}}' "
        f"{preview_data}"
    )

    result = subprocess.run(
        ["fzf", "--ansi", "--with-nth", "2..", "--delimiter", "\t",
         "--prompt", "open> ", "--height", "80%", "--reverse",
         "--preview", preview, "--preview-window", "down,50%,wrap"],
        input="\n".join(lines), capture_output=True, text=True,
    )
    shutil.rmtree(preview_data.parent, ignore_errors=True)
    if result.returncode != 0 or not result.stdout.strip():
        return 130

    index = int(result.stdout.split("\t", 1)[0])
    hit = hits[index]
    print(f"Opening {home_relative(hit.path)}", file=sys.stderr)
    open_hit(hit, cfg)
    return 0


def _dmenu_tool() -> list[str] | None:
    for tool, args in (
        ("walker", ["--dmenu"]),
        ("fuzzel", ["--dmenu"]),
        ("wofi", ["--dmenu", "--insensitive"]),
        ("rofi", ["-dmenu", "-i"]),
    ):
        if shutil.which(tool):
            return [tool, *args]
    return None


def _dmenu(tool: list[str], prompt: str, lines: str = "",
           index: bool = False) -> tuple[str, int]:
    """Show a dmenu picker. Returns (selection, exit code).

    With index=True on walker, the selected row number is returned instead of
    its text, so results with identical display strings stay distinguishable.
    """
    cmd = [*tool, *_prompt_flags(tool[0], prompt)]
    if not lines and tool[0] == "walker":
        cmd.append("--inputonly")   # free-text entry, no list to filter
    if index and tool[0] == "walker":
        cmd.append("--index")
    proc = subprocess.run(cmd, input=lines, capture_output=True, text=True)
    return proc.stdout.strip(), proc.returncode


def cmd_menu(args, cfg: Config) -> int:
    """Graphical two-step: type a description, pick from the results."""
    tool = _dmenu_tool()
    if tool is None:
        print("No dmenu-capable launcher found (walker, fuzzel, wofi, rofi).",
              file=sys.stderr)
        return 1

    query = args.query
    if not query:
        query, _ = _dmenu(tool, "Describe the document…")
        if not query:
            return 130

    hits = run_search(query, args.limit, cfg, args.min_score,
                      filters_from_args(args))
    if not hits:
        _dmenu(tool, f"No matches for “{query}”")
        return 1

    lines = []
    for hit in hits:
        if hit.via == "filter":
            loc = _day(hit.mtime)
        else:
            loc = f"p.{hit.page}" if hit.page > 0 else "name"
        lines.append(f"{hit.name}   ·   {loc}   ·   {home_relative(hit.path)}")

    use_index = tool[0] == "walker"
    picked, _ = _dmenu(tool, f"{len(hits)} results", "\n".join(lines),
                       index=use_index)
    if not picked:
        return 130

    if use_index:
        try:
            open_hit(hits[int(picked)], cfg)
            return 0
        except (ValueError, IndexError):
            return 1

    for hit, line in zip(hits, lines):
        if line == picked:
            open_hit(hit, cfg)
            return 0
    return 1


def _prompt_flags(tool: str, text: str) -> list[str]:
    return {
        "walker": ["--placeholder", text],
        "fuzzel": ["--prompt", f"{text}: "],
        "wofi": ["--prompt", text],
        "rofi": ["-p", text],
    }.get(tool, [])


def cmd_bar(args, cfg: Config) -> int:
    """Live search-as-you-type overlay window."""
    try:
        from .bar import run
    except (ImportError, ValueError) as exc:
        print(f"The search bar needs PyGObject, GTK4 and gtk4-layer-shell: {exc}",
              file=sys.stderr)
        return 1
    return run(cfg, args.limit, lambda hit: open_hit(hit, cfg),
               args.query or "")


def cmd_serve(args, cfg: Config) -> int:
    daemon.serve(cfg)
    return 0


def cmd_doctor(args, cfg: Config) -> int:
    """Report what works on this machine and what is missing."""
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        mark = "ok  " if good else "MISS"
        if not good:
            ok = False
        print(f"  [{mark}] {label}{'  — ' + detail if detail else ''}")

    system = "macOS" if env.MACOS else ("Linux" if env.LINUX else sys.platform)
    session = "macOS" if env.MACOS else (
        "Wayland" if env.is_wayland() else "X11" if os.environ.get("DISPLAY")
        else "no display")
    print(f"Platform: {system} · {session}")

    print("\nText extraction")
    line(bool(shutil.which("pdftotext")), "pdftotext (PDFs)",
         "" if shutil.which("pdftotext") else env.install_hint("pdftotext"))
    line(bool(shutil.which("pdftoppm")), "pdftoppm (page previews)",
         "" if shutil.which("pdftoppm") else env.install_hint("pdftoppm"))
    if cfg.ocr:
        line(bool(shutil.which("tesseract")), "tesseract (ocr = true)",
             "" if shutil.which("tesseract") else env.install_hint("tesseract"))
    else:
        print("  [--  ] tesseract not needed (ocr = false)")
    print("  [ok  ] .md/.txt/.docx/.epub use the standard library")

    print("\nSearch bar")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk  # noqa: F401
        line(True, "PyGObject + GTK4")
    except (ImportError, ValueError) as exc:
        line(False, "PyGObject + GTK4", str(exc).split("\n")[0])
    if env.is_wayland():
        if env.layer_shell_supported():
            print("  [ok  ] wlr-layer-shell — overlay window with exclusive focus")
        else:
            print("  [--  ] no wlr-layer-shell (GNOME?) — plain window instead")
    elif not env.MACOS:
        print("  [--  ] X11 — plain window instead of an overlay")
    else:
        print("  [--  ] macOS — plain window instead of an overlay")

    print("\nOpening results")
    if env.opens_at_page():
        print("  [ok  ] a page-aware PDF viewer is installed")
    else:
        hint = ("install Skim for page jumps" if env.MACOS
                else "install zathura, okular or evince for page jumps")
        print(f"  [--  ] opens at page 1 only — {hint}")

    print("\nSearch")
    import sqlite3
    try:
        sqlite3.connect(":memory:").execute(
            "CREATE VIRTUAL TABLE probe USING fts5(x)")
        print("  [ok  ] SQLite FTS5 — exact words rank alongside meaning")
    except sqlite3.OperationalError:
        print("  [--  ] SQLite without FTS5 — meaning-based ranking only, "
              "exact terms like order numbers may not surface")

    print("\nOther")
    line(bool(shutil.which("fzf")), "fzf (pdfind pick)",
         "" if shutil.which("fzf") else env.install_hint("fzf"))
    tool = _dmenu_tool()
    print(f"  [{'ok  ' if tool else '--  '}] "
          f"{'dmenu launcher: ' + tool[0] if tool else 'no dmenu launcher (pdfind menu unavailable)'}")
    alive = daemon.request({"action": "ping"}, timeout=2) is not None
    print(f"  [{'ok  ' if alive else '--  '}] daemon "
          f"{'running' if alive else 'not running — searches will be ~2s'}"
          f"  ({daemon.SOCKET_PATH})")

    print("\nEverything essential is present." if ok else
          "\nSome components are missing; see the hints above.")
    return 0


def cmd_status(args, cfg: Config) -> int:
    from .embed import Embedder
    from .store import Store

    print(f"Config:  {CONFIG_PATH}")
    print(f"Roots:   {', '.join(home_relative(str(r)) for r in cfg.roots)}")
    print(f"Types:   {', '.join(cfg.extensions)}")
    print(f"Model:   {cfg.model}")
    alive = daemon.request({"action": "ping"}, timeout=2) is not None
    print(f"Daemon:  {'running' if alive else 'not running'} ({daemon.SOCKET_PATH})")

    store = Store(Embedder(cfg.model).dim)
    st = store.stats()
    when = store.get_meta("indexed_at")
    print(f"Indexed: {st['files']} files, {st['chunks']} chunks"
          f"{f', {st['dead_rows']} reclaimable rows' if st['dead_rows'] else ''}")
    if when:
        age = time.time() - float(when)
        print(f"Updated: {_ago(age)}")
    store.close()
    return 0


def _ago(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def cmd_compact(args, cfg: Config) -> int:
    from .embed import Embedder
    from .store import Store

    store = Store(Embedder(cfg.model).dim)
    freed = store.compact()
    store.close()
    print(f"Reclaimed {freed} vector rows.")
    daemon.notify_reload()
    return 0


def cmd_reset(args, cfg: Config) -> int:
    from .config import DATA_DIR

    targets = [DATA_DIR / "index.db", DATA_DIR / "vecs.f32",
               DATA_DIR / "index.db-wal", DATA_DIR / "index.db-shm"]
    existing = [p for p in targets if p.exists()]
    if not existing:
        print("Nothing to delete.")
        return 0
    if not args.yes:
        print("This will delete the index:")
        for p in existing:
            print(f"  {p} ({p.stat().st_size / 1e6:.1f} MB)")
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    for p in existing:
        p.unlink()
    print("Index deleted. Run 'pdfind index' to rebuild.")
    return 0


def cmd_config(args, cfg: Config) -> int:
    path = ensure_config()
    if args.path:
        print(path)
        return 0
    editor = os.environ.get("EDITOR") or shutil.which("nvim") or "nano"
    subprocess.run([editor, str(path)])
    return 0


# ---------------------------------------------------------------- entrypoint


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdfind",
        description="Find documents by describing what is in them.",
    )
    sub = p.add_subparsers(dest="command")

    def add_query_flags(sp):
        sp.add_argument("-n", "--limit", type=int, default=10,
                        help="maximum results (default: 10)")
        sp.add_argument("--min-score", type=float, default=0.0,
                        help="drop results below this cosine score, exact "
                             "matches excepted (try 0.55; see README)")
        # The same narrowing is available inline as ext:/in:/since:/until:,
        # which is the only form the search bar can offer.
        sp.add_argument("--ext", action="append", metavar="EXT",
                        help="only this file type; repeat or comma-separate")
        sp.add_argument("--in", dest="within", action="append", metavar="PATH",
                        help="only under this folder; repeat for several")
        sp.add_argument("--since", metavar="WHEN",
                        help="only files modified since 2024-03-01, 2024, 7d, 6m…")
        sp.add_argument("--until", metavar="WHEN",
                        help="only files modified before that date")

    s = sub.add_parser("index", help="build or refresh the index")
    s.add_argument("-f", "--force", action="store_true",
                   help="re-read every file, ignoring mtime")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("search", help="print ranked matches")
    s.add_argument("query", nargs="*")
    s.add_argument("--paths-only", action="store_true",
                   help="print just file paths, one per line")
    add_query_flags(s)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("pick", help="search, choose in fzf, open")
    s.add_argument("query", nargs="*")
    add_query_flags(s)
    s.set_defaults(func=cmd_pick)

    s = sub.add_parser("menu", help="graphical search bar (walker/wofi/rofi)")
    s.add_argument("query", nargs="*")
    add_query_flags(s)
    s.set_defaults(func=cmd_menu)

    s = sub.add_parser("bar", help="live search-as-you-type overlay window")
    s.add_argument("-n", "--limit", type=int, default=12,
                   help="maximum results shown (default: 12)")
    s.add_argument("query", nargs="*", help="optional text to prefill")
    s.set_defaults(func=cmd_bar)

    s = sub.add_parser("serve", help="run the search daemon")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("doctor", help="check platform dependencies")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("status", help="show index and daemon state")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("compact", help="reclaim space from deleted files")
    s.set_defaults(func=cmd_compact)

    s = sub.add_parser("reset", help="delete the index")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_reset)

    s = sub.add_parser("config", help="edit the config file")
    s.add_argument("--path", action="store_true", help="print its path and exit")
    s.set_defaults(func=cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # Bare `pdfind some description` is shorthand for `pdfind search …`, and so
    # is a bare flag: `pdfind --ext pdf --since 7d` is a search with nothing to
    # describe. Only -h stays with the top-level parser, which has no other
    # options of its own.
    known = {"index", "search", "pick", "menu", "bar", "serve", "status",
             "doctor", "compact", "reset", "config"}
    if argv and argv[0] not in known and argv[0] not in ("-h", "--help"):
        argv.insert(0, "search")

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    if isinstance(getattr(args, "query", None), list):
        args.query = " ".join(args.query)

    cfg = load_config()
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
