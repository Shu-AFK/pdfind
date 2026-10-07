"""Platform differences, kept in one place.

pdfind runs on macOS and on Linux under any desktop. The parts that actually
differ are: where the daemon socket may live, how a file is opened, and whether
the search bar can use the wlroots layer-shell protocol or has to settle for an
ordinary window.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MACOS = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
WINDOWS = os.name == "nt"


def runtime_dir() -> Path:
    """Directory for the daemon socket.

    XDG_RUNTIME_DIR is per-user and mode 0700 on Linux; macOS has no such
    variable but its TMPDIR is already per-user under /var/folders.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and Path(xdg).is_dir():
        return Path(xdg)
    return Path(tempfile.gettempdir())


def is_wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY")) and not MACOS


LAYER_SHELL_LIBS = ("/usr/lib/libgtk4-layer-shell.so",
                    "/usr/lib64/libgtk4-layer-shell.so",
                    "/usr/local/lib/libgtk4-layer-shell.so")

_PROBE = (
    "import gi; gi.require_version('Gtk4LayerShell','1.0');"
    "from gi.repository import Gtk4LayerShell as L; print(int(L.is_supported()))"
)


def layer_shell_lib() -> str | None:
    return next((p for p in LAYER_SHELL_LIBS if Path(p).exists()), None)


def _preloaded() -> bool:
    lib = layer_shell_lib()
    return bool(lib and lib in os.environ.get("LD_PRELOAD", ""))


def layer_shell_supported() -> bool:
    """Whether the compositor speaks wlr-layer-shell.

    True on wlroots compositors (Hyprland, Sway, river, niri). False on X11,
    on GNOME (Mutter does not implement it), and on macOS.

    `is_supported()` only answers correctly when the library was preloaded
    ahead of libwayland-client, so from a process that was not preloaded this
    probes a short-lived child that is.
    """
    if not is_wayland() or not layer_shell_lib():
        return False
    if _preloaded():
        try:
            import gi
            gi.require_version("Gtk4LayerShell", "1.0")
            from gi.repository import Gtk4LayerShell as LayerShell
            return bool(LayerShell.is_supported())
        except Exception:
            return False

    lib = layer_shell_lib()
    child = dict(os.environ)
    existing = child.get("LD_PRELOAD", "")
    child["LD_PRELOAD"] = f"{lib}:{existing}" if existing else str(lib)
    try:
        out = subprocess.run([sys.executable, "-c", _PROBE], env=child,
                             capture_output=True, timeout=15)
        return out.stdout.strip() == b"1"
    except (subprocess.SubprocessError, OSError):
        return False


# -- opening files ---------------------------------------------------------

# Viewers that can jump straight to a page, in order of preference.
PAGE_VIEWERS = (
    ("zathura", lambda page: ["-P", str(page)]),
    ("okular", lambda page: ["-p", str(page)]),
    ("evince", lambda page: ["-i", str(page)]),
    ("qpdfview", lambda page: [f"#{page}"]),
    ("mupdf", lambda page: [str(page)]),
)

# macOS: Skim takes a page, Preview does not.
MACOS_PAGE_VIEWERS = ("Skim",)


def _spawn(argv: list[str]) -> None:
    subprocess.Popen(argv, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_file(path: str, page: int = 1, is_pdf: bool | None = None) -> None:
    """Open a document, at `page` when the platform's viewer can manage it."""
    page = max(1, page)
    if is_pdf is None:
        is_pdf = path.lower().endswith(".pdf")

    if MACOS:
        if is_pdf:
            for app in MACOS_PAGE_VIEWERS:
                if Path(f"/Applications/{app}.app").exists():
                    # Skim honours a page via its URL scheme.
                    _spawn(["open", "-a", app, path])
                    return
        _spawn(["open", path])
        return

    if WINDOWS:  # untested, but better than failing outright
        os.startfile(path)  # type: ignore[attr-defined]
        return

    if is_pdf:
        for viewer, args in PAGE_VIEWERS:
            if shutil.which(viewer):
                if viewer == "mupdf":
                    _spawn([viewer, path, *args(page)])
                else:
                    _spawn([viewer, *args(page), path])
                return

    for opener in ("xdg-open", "gio"):
        if shutil.which(opener):
            _spawn([opener, "open", path] if opener == "gio" else [opener, path])
            return


def opens_at_page(is_pdf: bool = True) -> bool:
    """Whether opening will actually land on the matching page."""
    if not is_pdf:
        return False
    if MACOS:
        return any(Path(f"/Applications/{a}.app").exists() for a in MACOS_PAGE_VIEWERS)
    if WINDOWS:
        return False
    return any(shutil.which(v) for v, _ in PAGE_VIEWERS)


# -- dependency reporting --------------------------------------------------

def install_hint(tool: str) -> str:
    brew = {"pdftotext": "brew install poppler", "pdftoppm": "brew install poppler",
            "tesseract": "brew install tesseract", "fzf": "brew install fzf"}
    arch = {"pdftotext": "pacman -S poppler", "pdftoppm": "pacman -S poppler",
            "tesseract": "pacman -S tesseract tesseract-data-eng",
            "fzf": "pacman -S fzf"}
    debian = {"pdftotext": "apt install poppler-utils",
              "pdftoppm": "apt install poppler-utils",
              "tesseract": "apt install tesseract-ocr", "fzf": "apt install fzf"}
    if MACOS:
        return brew.get(tool, f"brew install {tool}")
    if Path("/etc/arch-release").exists():
        return arch.get(tool, f"pacman -S {tool}")
    return debian.get(tool, f"apt install {tool}")
