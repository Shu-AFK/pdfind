"""Live search bar: a GTK4 layer-shell overlay that queries as you type.

Dmenu-style launchers filter a fixed list client-side, so they cannot re-run a
semantic query per keystroke — which is the whole point here. This is a small
purpose-built window instead: every keystroke (after a short debounce) goes to
the pdfind daemon, and each row shows the filename, its location, and the
matching text.

Queries run on a worker thread; only the UI update is marshalled back to the
main loop, so typing never blocks on the model.
"""

from __future__ import annotations

import html
import os
import sys
import threading
import time
from pathlib import Path

# On wlroots compositors gtk4-layer-shell must be loaded before
# libwayland-client, which the Python bindings cannot arrange on their own —
# GTK pulls in libwayland first and the layer surface silently fails to
# initialise. Re-exec once with the library preloaded. See linking.md in
# gtk4-layer-shell. Irrelevant on X11, GNOME and macOS, which get an ordinary
# window instead, so this is skipped there.
_PRELOAD = next((p for p in ("/usr/lib/libgtk4-layer-shell.so",
                             "/usr/lib64/libgtk4-layer-shell.so",
                             "/usr/local/lib/libgtk4-layer-shell.so")
                 if Path(p).exists()), None)

if (_PRELOAD and sys.platform.startswith("linux")
        and os.environ.get("WAYLAND_DISPLAY")
        and _PRELOAD not in os.environ.get("LD_PRELOAD", "")):
    _env = dict(os.environ)
    _existing = _env.get("LD_PRELOAD", "")
    _env["LD_PRELOAD"] = f"{_PRELOAD}:{_existing}" if _existing else _PRELOAD
    os.execve(sys.executable, [sys.executable, "-m", "pdfind", *sys.argv[1:]], _env)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from . import env  # noqa: E402
from .config import Config  # noqa: E402
from .hit import Hit  # noqa: E402

USE_LAYER_SHELL = env.layer_shell_supported()
if USE_LAYER_SHELL:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell

# Wait this long after the last keystroke before querying. Long enough to skip
# most intermediate words, short enough to feel immediate.
DEBOUNCE_MS = 120

WIDTH = 760                 # results only
WIDTH_WITH_PREVIEW = 1180   # widened when a preview is showing
HEIGHT = 520
PREVIEW_WIDTH = 400

CSS = b"""
window.pdfind {
    background: transparent;
}
box.frame {
    background-color: #1a1b26;
    border: 1px solid #33364a;
    border-radius: 14px;
}
entry.query {
    background: transparent;
    border: none;
    outline: none;
    box-shadow: none;
    color: #c0caf5;
    font-size: 19px;
    padding: 16px 20px;
    caret-color: #7aa2f7;
}
entry.query text { background: transparent; }
entry.query placeholder { color: #565f89; }
.divider { background-color: #33364a; min-height: 1px; }
list.results { background: transparent; }
list.results > row {
    padding: 9px 18px;
    border-radius: 8px;
    margin: 2px 8px;
}
list.results > row:selected { background-color: #2c3350; }
.name    { color: #c0caf5; font-size: 14px; font-weight: 600; }
.meta    { color: #7aa2f7; font-size: 11px; }
.meta.weak { color: #e0af68; }
.snippet { color: #787c99; font-size: 12px; }
.status  { color: #565f89; font-size: 13px; padding: 14px 20px; }
.vdivider { background-color: #33364a; min-width: 1px; }
box.preview { padding: 14px 16px; background-color: #16171f; }
.preview-title { color: #7aa2f7; font-size: 12px; font-weight: 600; }
.preview-text  { color: #9aa0bf; font-size: 11px; }
.preview-page  { background: #ffffff; border-radius: 6px; }
"""


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


class SearchBar(Gtk.Application):
    def __init__(self, cfg: Config, limit: int, on_open, query: str = ""):
        super().__init__(application_id=None, flags=0)
        self.cfg = cfg
        self.limit = limit
        self.on_open = on_open
        self.initial_query = query
        self.hits: list[Hit] = []
        self._timer: int | None = None
        self._generation = 0
        self._preview_generation = 0
        # Auto-selecting the first row must not pop the preview open; only a
        # deliberate hover, arrow key or click should.
        self._suppress_preview = True
        self._was_active = False

    # -- construction -----------------------------------------------------

    def do_activate(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.window = Gtk.ApplicationWindow(application=self)
        self.window.add_css_class("pdfind")
        self.window.set_default_size(WIDTH, HEIGHT)

        if USE_LAYER_SHELL:
            LayerShell.init_for_window(self.window)
            LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(self.window,
                                         LayerShell.KeyboardMode.EXCLUSIVE)
            LayerShell.set_namespace(self.window, "pdfind")
        else:
            # X11, GNOME and macOS: a normal window. It cannot force itself
            # above everything the way a layer surface can, so it is at least
            # undecorated and centred, and closes when it loses focus.
            self.window.set_decorated(False)
            self.window.set_resizable(False)
            self.window.set_title("pdfind")
            self.window.connect("notify::is-active", self._on_active_changed)

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.add_css_class("frame")

        self.entry = Gtk.Entry()
        self.entry.add_css_class("query")
        self.entry.set_placeholder_text("Describe the document…")
        self.entry.connect("changed", self._on_changed)
        self.entry.connect("activate", self._on_activate)
        frame.append(self.entry)

        divider = Gtk.Box()
        divider.add_css_class("divider")
        frame.append(divider)

        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("status")
        self.status.set_text("Type to search your documents.")
        frame.append(self.status)

        self.list = Gtk.ListBox()
        self.list.add_css_class("results")
        self.list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", lambda _lb, row: self._open(row.get_index()))
        self.list.connect("row-selected", self._on_selected)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.set_child(self.list)

        # Results on the left, preview on the right.
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(scroller)

        vdivider = Gtk.Box()
        vdivider.add_css_class("vdivider")
        content.append(vdivider)
        self.vdivider = vdivider
        self.preview_box = self._build_preview()
        content.append(self.preview_box)

        self.content = content
        content.set_visible(False)
        frame.append(content)

        self.window.set_child(frame)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.window.add_controller(keys)

        self.window.present()
        self.entry.grab_focus()
        if self.initial_query:
            self.entry.set_text(self.initial_query)
            self.entry.set_position(-1)

    def _build_preview(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("preview")
        box.set_size_request(PREVIEW_WIDTH, -1)
        box.set_visible(False)

        self.preview_title = Gtk.Label(xalign=0)
        self.preview_title.add_css_class("preview-title")
        self.preview_title.set_ellipsize(3)
        box.append(self.preview_title)

        self.preview_image = Gtk.Picture()
        self.preview_image.set_can_shrink(True)
        self.preview_image.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview_image.set_vexpand(True)
        self.preview_image.add_css_class("preview-page")

        self.preview_text = Gtk.Label(xalign=0, yalign=0)
        self.preview_text.add_css_class("preview-text")
        self.preview_text.set_wrap(True)
        self.preview_text.set_wrap_mode(2)      # Pango.WrapMode.WORD_CHAR
        self.preview_text.set_max_width_chars(46)
        self.preview_text.set_selectable(True)  # let long URLs be copied out

        text_scroller = Gtk.ScrolledWindow()
        text_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        text_scroller.set_vexpand(True)
        text_scroller.set_child(self.preview_text)
        self.preview_text_scroller = text_scroller

        # Only one of the two is ever shown, depending on the file type.
        self.preview_stack = Gtk.Stack()
        self.preview_stack.add_named(self.preview_image, "image")
        self.preview_stack.add_named(text_scroller, "text")
        self.preview_stack.set_vexpand(True)
        box.append(self.preview_stack)

        return box

    # -- preview ----------------------------------------------------------

    def _reveal_preview(self, shown: bool) -> None:
        self.preview_box.set_visible(shown)
        self.vdivider.set_visible(shown)
        self.window.set_default_size(WIDTH_WITH_PREVIEW if shown else WIDTH, HEIGHT)

    def _on_selected(self, _list, row) -> None:
        if row is None or self._suppress_preview:
            return
        self._preview_for(row.get_index())

    def _preview_for(self, index: int) -> None:
        """Show the preview pane for one result. Hover and keyboard both land here."""
        if not (0 <= index < len(self.hits)):
            return
        hit = self.hits[index]
        self._reveal_preview(True)

        self._preview_generation += 1
        generation = self._preview_generation
        page = max(1, hit.page)
        self.preview_title.set_text(
            f"{hit.name} · {'filename' if hit.page == 0 else f'page {page}'}")

        def work() -> None:
            from . import preview as pv
            image = pv.render_pdf_page(hit.path, page)
            text = ""
            if image is None:
                # Page 0 is the synthetic filename chunk, which would preview
                # as nothing but the filename again. Show the document instead.
                if hit.page == 0:
                    text = pv.page_text(hit.path, 1)
                else:
                    text = pv.page_text(hit.path, hit.page, hit.snippet)
            GLib.idle_add(self._show_preview, generation, image, text)
            pv.prune_cache()

        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, generation: int, image, text: str) -> bool:
        if generation != self._preview_generation:
            return False  # selection moved on while we were rendering
        if image is not None:
            self.preview_image.set_filename(str(image))
            self.preview_stack.set_visible_child_name("image")
        else:
            self.preview_text.set_text(text or "No preview available.")
            self.preview_text_scroller.get_vadjustment().set_value(0)
            self.preview_stack.set_visible_child_name("text")
        return False

    # -- input ------------------------------------------------------------

    def _on_active_changed(self, window, _param) -> None:
        """Close when focus goes elsewhere (non-layer-shell only).

        A layer surface takes exclusive keyboard focus and is dismissed with
        Escape; an ordinary window can be clicked away from, and leaving a
        stray undecorated window floating around would be worse than closing.

        Only after the window has actually been focused once — some window
        managers report it inactive before it is first mapped, which would
        otherwise close it instantly.
        """
        if window.is_active():
            self._was_active = True
        elif self._was_active:
            self.quit()

    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if keyval == Gdk.KEY_Escape:
            self.quit()
            return True
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up) or (
                ctrl and keyval in (Gdk.KEY_n, Gdk.KEY_p)):
            down = keyval == Gdk.KEY_Down or keyval == Gdk.KEY_n
            self._move(1 if down else -1)
            return True
        return False

    def _move(self, delta: int) -> None:
        if not self.hits:
            return
        self._suppress_preview = False
        row = self.list.get_selected_row()
        index = (row.get_index() if row else -1) + delta
        index = max(0, min(len(self.hits) - 1, index))
        target = self.list.get_row_at_index(index)
        if target:
            self.list.select_row(target)
            target.grab_focus()
            self.entry.grab_focus_without_selecting()

    def _on_changed(self, _entry) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(DEBOUNCE_MS, self._fire)

    def _on_activate(self, _entry) -> None:
        row = self.list.get_selected_row()
        self._open(row.get_index() if row else 0)

    def _open(self, index: int) -> None:
        if 0 <= index < len(self.hits):
            self.on_open(self.hits[index])
            self.quit()

    # -- querying ---------------------------------------------------------

    def _fire(self) -> bool:
        self._timer = None
        query = self.entry.get_text().strip()
        if not query:
            self._render([], "")
            return False

        self._generation += 1
        generation = self._generation

        def work() -> None:
            from . import daemon
            hits = daemon.search_via_daemon(query, self.limit)
            if hits is None:  # daemon down: fall back in-process
                from .search import Searcher
                hits = Searcher(self.cfg).search(query, limit=self.limit)
            GLib.idle_add(self._deliver, generation, hits, query)

        threading.Thread(target=work, daemon=True).start()
        return False

    def _deliver(self, generation: int, hits: list[Hit], query: str) -> bool:
        if generation == self._generation:  # ignore stale, out-of-order replies
            self._render(hits, query)
        return False

    # -- rendering --------------------------------------------------------

    def _render(self, hits: list[Hit], query: str) -> None:
        self.hits = hits
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)

        if not query:
            self.status.set_text("Type to search your documents.")
            self.status.set_visible(True)
            self.content.set_visible(False)
            self._reveal_preview(False)
            return
        if not hits:
            self.status.set_text(f"No matches for “{query}”.")
            self.status.set_visible(True)
            self.content.set_visible(False)
            self._reveal_preview(False)
            return

        self.status.set_visible(False)
        self.content.set_visible(True)
        self._suppress_preview = True
        self._was_active = False
        self._reveal_preview(False)
        for hit in hits:
            self.list.append(self._row(hit))
        first = self.list.get_row_at_index(0)
        if first:
            self.list.select_row(first)  # Enter opens this; no preview yet

    def _row(self, hit: Hit) -> Gtk.ListBoxRow:
        from .cli import home_relative

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(xalign=0, label=hit.name)
        name.add_css_class("name")
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END
        header.append(name)

        if hit.via == "filter":
            # A filters-only query ("ext:pdf since:7d") ranked nothing, so
            # there is no score to show and the date says more.
            label = time.strftime("%Y-%m-%d", time.localtime(hit.mtime))
        else:
            where = "filename" if hit.page == 0 else f"p.{hit.page}"
            label = f"{where} · {hit.score:.2f}"
            if hit.via == "exact":
                label += " · exact"
            elif not hit.confident:
                label += " · weak"
        if hit.copies > 1:
            label += f" · {hit.copies} copies"
        meta = Gtk.Label(xalign=0, label=label)
        meta.add_css_class("meta")
        if not hit.confident:
            meta.add_css_class("weak")
        header.append(meta)
        box.append(header)

        path = Gtk.Label(xalign=0, label=home_relative(hit.path))
        path.add_css_class("snippet")
        path.set_ellipsize(1)  # START: keep the filename end visible
        box.append(path)

        # The snippet is the "what is actually in it" line the filename cannot
        # give you, so it gets two wrapped lines rather than one truncated one.
        snippet = Gtk.Label(xalign=0, label=hit.snippet)
        snippet.add_css_class("snippet")
        snippet.set_wrap(True)
        snippet.set_lines(2)
        snippet.set_ellipsize(3)
        snippet.set_max_width_chars(94)
        box.append(snippet)

        row = Gtk.ListBoxRow()
        row.set_child(box)

        # Hovering previews without disturbing the keyboard selection.
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self._on_hover(row))
        row.add_controller(motion)
        return row

    def _on_hover(self, row: Gtk.ListBoxRow) -> None:
        self._suppress_preview = False
        self.list.select_row(row)


def run(cfg: Config, limit: int, on_open, query: str = "") -> int:
    app = SearchBar(cfg, limit, on_open, query)
    return app.run([])
