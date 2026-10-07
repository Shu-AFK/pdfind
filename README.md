# pdfind

Find documents by describing roughly what is written in them, instead of
remembering the filename.

```console
$ pdfind "that chemical sheet with the hazard and toxicity tables"
 1. 41-L0EL0085.pdf  0.664  p.16
    ~/Work/sdsextv2/pdfs/41-L0EL0085.pdf
    …CAS: Chemical Abstracts Service (Abteilung der American Chemical…

$ pdfind "Reisenotizen Australien"          # same documents, either language
 1. Cairns.md  0.537  filename
    ~/Travel/Australia/Cairns.md

$ pdfind "41-L0EL0085"                      # an order number is not a description
 1. 41-L0EL0085.pdf  0.530  filename exact (2 copies)
    ~/Work/sdsextv2/pdfs/41-L0EL0085.pdf

$ pdfind "ext:pdf since:2025 atemschutz"    # narrow it when you remember more
```

Everything runs locally. No API keys, no network after the first run, no data
leaves the machine.

## How it works

Each document is split into overlapping ~1200-character chunks, and every chunk
is turned into a 384-dimensional vector by a small ONNX embedding model
(`paraphrase-multilingual-MiniLM-L12-v2`, ~450 MB). A query is embedded the
same way, and results are ranked by cosine similarity. Because the comparison happens in meaning-space
rather than on words, "warehouse lease" finds a document that only ever says
"rental contract for storage premises" — including across languages, so an
English description matches a German document.

The filename and parent folder are embedded as an extra chunk, so
`pdfind "australia trip notes"` finds `~/Travel/Australia/Cairns.md` even when
the text inside never mentions Australia.

A second, literal index runs alongside it, because meaning-space has no room
for an order number — see [Exact words and codes](#exact-words-and-codes).

- **Previews** are cached PNGs under `~/.cache/pdfind/previews`, keyed by path,
  mtime and page. A cold render takes ~0.6 s, cached is instant, and the cache
  self-prunes to 400 files.
- **Metadata** lives in SQLite at `~/.local/share/pdfind/index.db`.
- **Vectors** live in a flat `float32` file, memory-mapped at query time.
  Brute-force scoring stays well under 100 ms for this corpus size, so there is
  no approximate index to tune or corrupt.
- **Text extraction** shells out to `pdftotext` (poppler) and, for scanned PDFs,
  `tesseract`. `.docx` and `.epub` are read with the standard library. No PDF
  or Office parsing libraries are involved.

## Commands

| Command | Purpose |
|---|---|
| `pdfind index` | Build or refresh the index (incremental; `--force` to redo everything) |
| `pdfind "some description"` | Search and print ranked results |
| `pdfind pick "some description"` | Search, choose in `fzf` with a preview, open it |
| `pdfind bar` | Live search-as-you-type window with previews |
| `pdfind menu` | Two-step picker via walker/fuzzel/wofi/rofi (fallback) |
| `pdfind serve` | Run the search daemon |
| `pdfind status` | Show index and daemon state |
| `pdfind compact` | Reclaim space left by deleted files |
| `pdfind reset` | Delete the index (needed after changing the model) |
| `pdfind doctor` | Check what this machine has and what is missing |
| `pdfind config` | Edit `~/.config/pdfind/config.toml` |

Useful flags: `-n/--limit` for the number of results, `--min-score` to drop weak
matches, `--paths-only` to pipe filenames into another command, and the filters
below.

## Filters

Semantic ranking answers *what is this document about*. Filters answer *which
of those*, and people usually remember roughly when a document arrived or where
it lives even when they cannot remember a word of its contents.

| Flag | Inline | Meaning |
|---|---|---|
| `--ext pdf` | `ext:pdf` | only this file type; repeat or comma-separate |
| `--in Work` | `in:Work` | only under this folder |
| `--since 2024-03` | `since:2024-03` | modified on or after |
| `--until 2024` | `until:2024` | modified before the end of that period |

Dates take `2024`, `2024-03`, `2024-03-05`, or a span: `7d`, `6w`, `3m`, `2y`.
A partial date resolves to the *end* of what it names when used with `until`,
so `until:2024` includes all of 2024 rather than stopping on New Year's Day.
`in:work` matches a whole path component — `~/Work/lease.pdf` but not
`~/Homework/lease.pdf`.

The two forms mean the same thing:

```console
$ pdfind --ext pdf --in sdsextv2 "atemschutz maske"
$ pdfind "ext:pdf in:sdsextv2 atemschutz maske"
 1. 80-9417.pdf  0.607  p.10
    ~/Work/sdsextv2/pdfs/80-9417.pdf
    …rationen über den AGW-Werten ist ein entsprechendes, geprüftes
    Atemschutzgerät zu tragen. Schutzmaßnahmen : Dampf/Aerosol nicht einatmen…
```

The inline form exists because the search bar and the dmenu picker have a text
entry and nothing else — every entry point parses it, so `ext:pdf` works while
typing into the bar. It is parsed leniently: an unknown key or an unreadable
date stays part of the description rather than becoming an error, because the
bar re-parses on every keystroke and `since:letz` must not blow up halfway
through the word. The flags are strict instead and say what went wrong.

Filters apply *before* ranking, not to the results, so narrowing to a handful of
files still returns the best of those handful rather than whatever survived a
top-200 cut.

### Filters on their own

With no description left over, there is nothing to rank, so the newest matching
files are listed and the score column gives way to the date:

```console
$ pdfind --ext pdf --in sdsextv2 -n 3
 1. 70-123331.pdf  2025-12-21
    ~/Work/sdsextv2/pdfs/70-123331.pdf
    3M™ 08855 MS Dichtungsmasse Sicherheitsdatenblatt Copyright 2025, 3M Company…

 2. 41-L0EL0085.pdf  2025-12-21
    ~/Work/sdsextv2/pdfs/41-L0EL0085.pdf
    Sicherheitsdatenblatt ENERGY SPRAY LINE UHS FADE-OUT BLENDER…
```

## Duplicate copies

The same document often sits in several places. Results are collapsed by
content, so each document appears once, represented by its best-ranked path,
with the rest shown as a count:

```
 1. 70-123331.pdf  0.710  p.23 (2 copies)
    ~/Work/sdsextv2/pdfs/70-123331.pdf
```

Files are matched on a hash of their **bytes**, not their extracted text. That
distinction matters: hashing the text merges everything that extracts to
nothing — a scanned PDF, an empty note and an empty `dependency_links.txt` all
collide — which would silently hide unrelated documents. Byte hashing found 9
genuine duplicate groups here and no false ones, including two files that are
identical despite different names (`70-1010.pdf` and `15-23303.pdf`).

Hashes are stored per file and computed during indexing. An index built before
this feature is migrated in place: the files are hashed, but nothing is
re-embedded, so it takes seconds rather than minutes.

Set `deduplicate = false` in the config to see every copy separately.

## Exact words and codes

A 384-dimension vector has no room for an order number. "41-L0EL0085" is not a
concept, and the nearest neighbours of its embedding are simply other strings
that look alphanumeric — so meaning-based ranking answers this kind of query
with confident nonsense:

| `pdfind "41-L0EL0085"` | first result | score |
|---|---|---|
| vectors alone | *202510 - Introduction to binary exploitation.pdf* (a page of `Aa0Aa1Aa2`) | 0.787 |
| with the keyword index | **41-L0EL0085.pdf** | 0.530 |

Note which score is higher. The wrong answer is the *more confident* one, which
is why no threshold could have caught it.

So the chunk text — already sitting in SQLite — also gets an FTS5 index, and
every query runs twice: once through the model, once through BM25. The keyword
index costs postings only (`content='chunks'`, no second copy of the text), and
a lookup takes well under a millisecond on this corpus.

Query words are OR-ed, because a description is not a boolean query: "hazard and
toxicity tables" should still reach a chunk that only says "toxicity". Words of
four characters or more match as prefixes, which is what makes the keyword half
useful in the live bar before you finish typing, and doubles as crude
morphology (`miete*` reaches *Mieten*). A word the tokeniser splits further
stays a **phrase**: `41-L0EL0085` is one thing, not a `41` and an `L0EL0085` —
searched apart, that bare `41` drags in every page containing the number.

### Combining the two rankings

The textbook answer is reciprocal rank fusion — each side contributes
1/(K+rank), positions only, no incomparable numbers to weight. It was tried
first and it ranks *worse than either half alone* here, because it throws away
the one thing a cosine carries: magnitude. A 0.605 match and a 0.503 match sit
at ranks 0 and 1, which RRF prices 0.2% apart, and the 0.503 document then wins
on nothing but having more mediocre chunks. Since `confidence_threshold` and
every score in the output are built on that same magnitude, discarding it for
ranking would leave the order and the numbers telling different stories.

Cosine scoring therefore stays exactly as it was, and a literal match adds a
bounded boost on top of it. How large depends on the query, which is the one
judgement call here:

- **The query names a code** — letters mixed with digits, or a long run of
  digits. The embedding cannot represent it and the keyword index is the only
  side that knows anything, so it leads outright.
- **The query is a description.** A literal match is corroboration: worth a
  place in the results, not the top of them.

That second number was swept over this corpus against five descriptive queries:

| boost | effect |
|---|---|
| 0.00 | keyword-only matches never surface |
| **0.03** | pulls literal matches *into* the results; the first result never changes |
| 0.05 | starts reordering documents a hundredth of a point apart |
| 0.15 | puts a passing mention of "Reisenotizen" above the travel notes themselves |

0.03 it is. The practical consequence is that descriptive queries rank as they
always did — with no literal match, or with `hybrid = false` in the config, the
order is bit-for-bit what it was before any of this existed — while codes,
surnames and case references now find their document.

Results found this way are marked `exact`:

```
 1. 41-L0EL0085.pdf  0.530  filename exact (2 copies)
```

The badge is claimed only for code queries. Under an ordinary description
nearly every result contains *some* word of it literally, so saying so would be
noise — and it is exactly the code case where a low cosine is still the right
answer, so `exact` also suppresses the `(weak)` label and is exempt from
`--min-score`. Both of those are cutoffs on how far to trust a number that an
exact match does not rest on: at `--min-score 0.6`, 41-L0EL0085.pdf (0.530)
would otherwise be dropped in favour of the page of `Aa0Aa1Aa2` that scored
0.787.

An index built before this feature gains the keyword half on first use: the
text is already there, so it is rebuilt from SQLite in seconds rather than
re-embedded in minutes. A Python whose SQLite lacks FTS5 falls back to
meaning-based ranking alone; `pdfind doctor` says which one you have.

## Reading the scores

Results show the best matching chunk's cosine similarity, and anything below
`confidence_threshold` (default **0.53**) is labelled `(weak)`.

That threshold is empirical. Measured on this corpus with deliberately
meaningless queries versus real ones:

| | scores |
|---|---|
| junk queries | 0.314, 0.470, 0.484, 0.484, 0.504, **0.630** |
| real queries | **0.537**, 0.576, 0.584, 0.616, 0.647, 0.681, 0.731, 0.856, 0.866 |

At 0.53 every real query is kept and only one junk query slips through — and
that one ("purple monkey dishwasher tuesday") is made of ordinary English
words, so it is not really semantic noise. The ranges do overlap; a bi-encoder
cannot separate them perfectly. Treat `(weak)` as a hint, not a verdict, and
raise `confidence_threshold` in the config if junk looks confident to you.

**The threshold is model-specific.** Under the English-only `bge-small-en`
these numbers are meaningless — there, raw cosine put a nonsense query at 0.80,
above genuine ones, and a variance-based measure worked instead. Changing the
model means re-measuring.

## The daemon

Loading the model costs about a second, which is fine for a shell command and
far too slow for a launcher. `pdfind serve` keeps the model and index resident
and answers over a unix socket at `$XDG_RUNTIME_DIR/pdfind.sock`.

```bash
systemctl --user enable --now pdfind.service   # already done on this machine
```

Every command tries the daemon first and silently falls back to loading the
model in-process, so nothing breaks if it is not running — it is only slower.

Measured on this machine:

| Path | Latency |
|---|---|
| Query over the socket | 26–60 ms |
| Full `pdfind search` command | ~280 ms (mostly Python startup) |
| Without the daemon | ~2.0 s |

## Platforms

The index, search, daemon and CLI are portable: they need Python, `pdftotext`
from poppler, and nothing else platform-specific. The search bar and the
hotkey are where systems differ.

Run `pdfind doctor` first — it reports what is present, what is missing, and
the install command for your platform.

| | wlroots Wayland (Hyprland, Sway, niri) | GNOME / KDE / X11 | macOS |
|---|---|---|---|
| Search + index | yes | yes | yes |
| `pdfind bar` | overlay, exclusive focus | plain window | plain window |
| Opens at page | zathura / okular / evince | same | Skim, else page 1 |
| Hotkey | compositor binding | desktop shortcut | skhd / Raycast |
| Daemon | systemd user unit | systemd user unit | launchd |

### The search bar across platforms

The bar is a GTK4 window. On wlroots compositors it uses `wlr-layer-shell` to
float above everything with exclusive keyboard focus. Everywhere else that
protocol is unavailable — GNOME's Mutter has never implemented it, X11 has no
equivalent, and macOS is not Wayland at all — so it falls back to an ordinary
undecorated window that centres itself and closes when it loses focus. Same
search, same previews, same keys; it just cannot force itself above a
fullscreen window.

Detection is at runtime via `Gtk4LayerShell.is_supported()`, so one build
behaves correctly on all of them.

On a tiling WM the fallback window gets tiled like any other. If you end up on
that path under Hyprland, float it:

```
windowrulev2 = float, title:^(pdfind)$
```

### Installing

**Arch (AUR)** — pulls in everything, including the optional bits as
`optdepends`:

```bash
paru -S pdfind          # or: pdfind-git
```

**Anywhere else (PyPI):**

```bash
# system parts first — pdftotext is required, the rest are optional
sudo apt install poppler-utils python3-gi gir1.2-gtk-4.0   # Debian/Ubuntu
brew install poppler pygobject3 gtk4                        # macOS

pipx install --system-site-packages pdfind
```

**From a checkout:**

```bash
pipx install --system-site-packages -e /path/to/pdfind
```

`--system-site-packages` matters whenever you want `pdfind bar`: PyGObject
comes from the system package manager, not pip, and without that flag `gi` is
invisible inside the pipx venv. The CLI works fine without it.

Then:

```bash
pdfind doctor && pdfind index
```

Packaging files live in `packaging/`. Releases are automated: pushing a
`v*.*.*` tag builds the distributions and publishes to PyPI, the AUR, and the
Homebrew tap. See `RELEASING.md`.

### Hotkey

**Hyprland** (`~/.config/hypr/bindings.conf`) — this is what is configured here:

```
bindd = SUPER ALT, D, Find document, exec, uwsm app -- $HOME/.local/bin/pdfind bar
```

**Sway:** `bindsym $mod+Mod1+d exec pdfind bar`

**GNOME:** Settings → Keyboard → Custom Shortcuts, command `pdfind bar`.

**KDE:** System Settings → Shortcuts → Custom Shortcuts → new command.

**macOS:** there is no built-in way to bind an arbitrary command to a key.
Either [skhd](https://github.com/koekeishiya/skhd):

```
cmd + alt - d : /opt/homebrew/bin/pdfind bar
```

or a Raycast/Alfred script command, or an Automator Quick Action with a
keyboard shortcut.

### Running the daemon

**Linux (systemd):**

```bash
cp ~/Work/pdfind/scripts/pdfind.service ~/.config/systemd/user/
systemctl --user enable --now pdfind.service
```

**macOS (launchd):**

```bash
cp ~/Work/pdfind/scripts/com.pdfind.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pdfind.daemon.plist
```

Adjust the path inside the plist if Homebrew is at `/usr/local` (Intel Macs)
rather than `/opt/homebrew` (Apple Silicon). `scripts/com.pdfind.index.plist`
does the hourly re-index, the counterpart to the systemd timer.

### Keys and behaviour

| Key | Action |
|---|---|
| type | search (120 ms after you stop typing) |
| ↓ / ↑, Ctrl-N / Ctrl-P | move selection, opening the preview |
| hover | preview without touching the keyboard |
| Enter | open the selected result |
| Escape | close |

Results update as you type; each row shows the filename, matching page, score,
and a two-line excerpt. Selecting or hovering a row opens the preview pane:
for PDFs the actual matching page rendered by poppler, otherwise that page's
full text. Nothing is previewed until you select or hover, so a fresh result
list stays compact.

`pdfind bar "some text"` prefills the query, which is handy for binding a
"search for the selected text" shortcut.

### Why not walker / rofi / dmenu?

Walker's providers are compiled in, and dmenu mode filters a fixed list
client-side — neither can re-run a semantic query per keystroke, which is the
whole point. Hence the purpose-built window. `pdfind menu` still exists as a
two-step walker/fuzzel/wofi/rofi fallback for machines without GTK4.

Two things that bit me, in case they come up again: on wlroots,
gtk4-layer-shell must be loaded *before* libwayland-client or the layer surface
silently fails, so the bar re-execs itself once with `LD_PRELOAD` set (skipped
on X11 and macOS); and the pipx venv needs `--system-site-packages` for `gi` to
be importable at all.

## Configuration

`~/.config/pdfind/config.toml`. The defaults index `~` for
`pdf, md, txt, docx, epub`, skipping dependency and build caches — without those
exclusions this machine's Go module cache alone contributed 4870 vendored
markdown files against 184 real documents.

Notable settings:

- `roots` — directories to index. Narrow this to `["~/Documents", "~/Downloads"]`
  if a whole-home index is more than you want.
- `model` — see below.
- `ocr` — off by default. Turn on to run tesseract over PDFs that contain no
  extractable text (scans and photographed documents). Slow, but it is the only
  way those become searchable at all.
- `max_chunks_per_file` — caps embedding cost per file (default 200). One
  imgui changelog in this corpus would otherwise produce 589 chunks on its own.
- `hybrid` — on by default. Set false to rank on the embedding alone; the
  keyword index stays built and stays in step, it is simply not consulted.

### Language

The default model covers 50+ languages, so an English description finds a
German document. This matters here: the corpus contains `Gefahrstoffverordnung`,
`Eidesstattliche Versicherung`, and an `Abitur` folder.

The English-only alternative, `BAAI/bge-small-en-v1.5`, is somewhat sharper on
pure English — but it fails *quietly* on anything else. Cosine similarity
between an English phrase and its German translation, versus the same English
phrase and unrelated German text:

| Pair | `bge-small-en` | multilingual |
|---|---|---|
| "hazardous substances ordinance" ↔ *Gefahrstoffverordnung Sicherheitsdatenblatt* | 0.616 | 0.726 |
| "rental contract for an apartment" ↔ *Mietvertrag für eine Wohnung* | 0.485 | 0.903 |
| "school leaving exam certificate" ↔ *Abitur Zeugnis der Hochschulreife* | 0.551 | 0.485 |
| **unrelated control** ↔ *Rezept für Schokoladenkuchen* | **0.512** | **-0.001** |

The control row is the point. `bge-small-en` rates unrelated German text 0.512
— above two of its three genuine translations. It is not merely worse at
German; it cannot rank German at all. The multilingual model puts the same
control at zero.

To switch back, edit `model` and rebuild — changing the model invalidates every
stored vector:

```bash
pdfind reset && pdfind index
```

## Keeping the index fresh

Re-indexing is incremental — files are skipped unless mtime or size changed, so
a no-op run takes about 1.4 seconds. To run it hourly:

```bash
# Linux
cp ~/Work/pdfind/scripts/pdfind-index.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now pdfind-index.timer

# macOS
cp ~/Work/pdfind/scripts/com.pdfind.index.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pdfind.index.plist
```

## Performance notes

Indexing is bound by the embedding model on CPU. A full rebuild of 184 files /
2558 chunks takes **2m19s** on this Ryzen AI 7 350 — roughly 18 chunks/second.
Extraction runs in parallel threads and chunks are embedded in large batches,
but the model itself is the floor; there is no GPU acceleration here.

(The English-only `bge-small-en` took 11m53s for the same corpus — its fastembed
build is markedly slower on this CPU despite being the smaller model. Combined
with the language results, there was no reason to keep it.)

This cost is paid once; day-to-day runs only touch changed files.

On the query side the model dominates too. Across five queries the median
in-process search went from 17.0 ms to 19.5 ms when the keyword half was added
— the BM25 lookup itself is 0.1–0.6 ms, the rest is embedding the query. A
filtered query is not slower than an unfiltered one: it is decided per file
(184 of them) and fanned out to chunks with a single array gather.

## Known limitations

- **Only byte-identical copies are merged.** A document re-exported, annotated,
  or saved by a different program is a different file and appears separately,
  even though a person would call it the same document.
- **Scanned PDFs are invisible unless `ocr = true`.** They contain no
  extractable text, so they index as filename-only.
- **The tail of very long files is unindexed** once `max_chunks_per_file` is hit.
- **macOS support is written but untested.** It was developed on Arch/Hyprland;
  the macOS paths (`open`, Skim, launchd, `~/Library` exclusions, the
  non-layer-shell window) follow from the platform's documented behaviour but
  have not been run on a Mac. The X11/plain-window fallback *was* verified, by
  forcing `GDK_BACKEND=x11`.
- **The confidence threshold is empirical and model-specific.** 0.53 was
  measured against this corpus with this model. It does not transfer to a
  different embedding model, and junk and real scores genuinely overlap.
- **Short queries in a non-dominant language score lower.** "Reisenotizen
  Australien" lands at 0.537, right on the threshold, where the English
  equivalent reaches 0.647.
- **"Is this a code?" is decided by shape, not by knowledge.** A query is
  treated as literal when it mixes letters and digits or runs six digits or
  more. A purely alphabetic identifier — a surname, a project codename — looks
  like a description and only gets the small boost, so it can still be
  outranked by documents that merely read like it.
- **The keyword index matches words, not stems.** Prefixes cover *Miete →
  Mieten*, but nothing reaches *Haus* from *Häuser*: `remove_diacritics` folds
  umlauts, it does not decline German nouns.

## Layout

```
pdfind/
├── config.py    configuration and default excludes
├── extract.py   pdftotext / tesseract / docx / epub -> text, and chunking
├── embed.py     ONNX embedding model wrapper
├── store.py     SQLite metadata + FTS5 keyword index + flat float32 vectors
├── filters.py   ext/in/since/until, from flags and from the query itself
├── indexer.py   filesystem walk and incremental (re)index
├── search.py    query embedding, scoring, per-file grouping
├── hit.py       result type and snippet rendering (numpy-free, kept light)
├── daemon.py    unix-socket search daemon
├── bar.py       GTK4 search-as-you-type window
├── preview.py   page rendering and text preview, with an on-disk cache
├── env.py       platform differences: opening files, layer-shell, socket dir
└── cli.py       command line interface
```

`hit.py` is separate from `search.py` on purpose: the CLI and launcher import
only the lightweight module, so talking to a running daemon does not pay for
numpy and the model stack.
