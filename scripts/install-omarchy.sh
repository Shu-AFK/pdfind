#!/usr/bin/env bash
#
# Set up pdfind on Omarchy: system packages, the pdfind CLI (via pipx), the
# search daemon, the hourly re-index timer, the hotkey, and a first index.
#
# Safe to re-run: every step checks what is already there.
#
#   ./scripts/install-omarchy.sh                 # from a checkout (editable install)
#   curl -fsSL https://raw.githubusercontent.com/Shu-AFK/pdfind/main/scripts/install-omarchy.sh | bash
#
# Environment:
#   PDFIND_KEY="SUPER + ALT + D"   hotkey to bind
#   PDFIND_NO_INDEX=1              skip the first index (it downloads ~450 MB)

set -euo pipefail

KEY="${PDFIND_KEY:-SUPER + ALT + D}"
REPO_URL="https://github.com/Shu-AFK/pdfind"
BIN="$HOME/.local/bin/pdfind"
HYPR="$HOME/.config/hypr"
UNITS="$HOME/.config/systemd/user"

step() { printf '\n\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

command -v pacman >/dev/null || die "this script is for Omarchy / Arch Linux"
command -v omarchy >/dev/null || warn "omarchy not found — continuing as plain Arch + Hyprland"

# Running from a checkout gives an editable install; piped through curl there
# is no checkout, so install straight from GitHub.
SRC=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [[ -f "$dir/pyproject.toml" && -d "$dir/pdfind" ]] && SRC="$dir"
fi

# --- packages ----------------------------------------------------------------

step "Installing system packages"
pkgs=(
  python python-pipx
  poppler                                  # pdftotext / pdftoppm (required)
  python-gobject gtk4 gtk4-layer-shell     # the search bar overlay
  fzf                                      # pdfind pick
  zathura zathura-pdf-mupdf                # opens PDFs at the matching page
  tesseract tesseract-data-eng             # OCR, used only with ocr = true
)
if command -v omarchy >/dev/null; then
  omarchy pkg add "${pkgs[@]}"
else
  sudo pacman -S --needed --noconfirm "${pkgs[@]}"
fi

# --- pdfind itself -------------------------------------------------------------

# --system-site-packages: PyGObject comes from pacman, and without it `gi` is
# invisible inside the pipx venv and `pdfind bar` cannot start.
step "Installing pdfind"
if [[ -n "$SRC" ]]; then
  pipx install --force --system-site-packages -e "$SRC"
else
  pipx install --force --system-site-packages "git+$REPO_URL.git"
fi
[[ -x "$BIN" ]] || die "pdfind did not end up at $BIN"

# --- systemd user units --------------------------------------------------------

step "Enabling the search daemon and hourly re-index"
mkdir -p "$UNITS"

cat >"$UNITS/pdfind.service" <<'EOF'
[Unit]
Description=pdfind semantic search daemon

[Service]
Type=simple
ExecStart=%h/.local/bin/pdfind serve
Restart=on-failure
RestartSec=5
MemoryMax=2G
Nice=5

[Install]
WantedBy=default.target
EOF

cat >"$UNITS/pdfind-index.service" <<'EOF'
[Unit]
Description=Refresh the pdfind document index

[Service]
Type=oneshot
ExecStart=%h/.local/bin/pdfind index
Nice=10
IOSchedulingClass=idle
EOF

cat >"$UNITS/pdfind-index.timer" <<'EOF'
[Unit]
Description=Refresh the pdfind document index hourly

[Timer]
OnBootSec=10min
OnUnitActiveSec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable pdfind.service pdfind-index.timer
systemctl --user start pdfind-index.timer
# Restart rather than start, so a re-run picks up the newly installed code.
systemctl --user restart pdfind.service

# --- hotkey --------------------------------------------------------------------

step "Binding $KEY to the search bar"

# "SUPER + ALT + D" -> mods "SUPER ALT", key "D"
norm="$(tr -s ' ' <<<"${KEY//+/ }" | sed 's/^ //; s/ $//')"
key="${norm##* }"
mods="${norm% *}"; [[ "$mods" == "$key" ]] && mods=""

# Find what, if anything, the key does today, so it can be unbound and reported.
existing=""
if command -v omarchy >/dev/null; then
  existing="$(omarchy menu keybindings --print 2>/dev/null \
    | grep -iE "^${mods:+$mods }\+ ${key} +→" | sed 's/.*→ *//' | head -1 || true)"
fi

if [[ -f "$HYPR/bindings.lua" ]]; then
  target="$HYPR/bindings.lua"
elif [[ -f "$HYPR/bindings.conf" ]]; then
  target="$HYPR/bindings.conf"          # Omarchy before the Lua config
else
  die "no $HYPR/bindings.lua or bindings.conf found"
fi

if grep -q 'pdfind bar' "$target"; then
  echo "already bound in $target — leaving it alone"
else
  cp "$target" "$target.bak.$(date +%s)"
  if [[ -n "$existing" && "$existing" != "Find document" ]]; then
    warn "$KEY was bound to \"$existing\" — overriding it"
  fi

  if [[ "$target" == *.lua ]]; then
    {
      echo
      echo "-- pdfind: search documents by description"
      [[ -n "$existing" && "$existing" != "Find document" ]] \
        && echo "hl.unbind(\"$KEY\")  -- was: $existing"
      echo "o.bind(\"$KEY\", \"Find document\", { launch = \"$BIN bar\" })"
    } >>"$target"
  else
    {
      echo
      echo "# pdfind: search documents by description"
      [[ -n "$existing" && "$existing" != "Find document" ]] \
        && echo "unbind = $mods, $key"
      echo "bindd = $mods, $key, Find document, exec, uwsm app -- $BIN bar"
    } >>"$target"
  fi
  echo "added to $target"
fi

if command -v hyprctl >/dev/null && [[ -n "${HYPRLAND_INSTANCE_SIGNATURE:-}" ]]; then
  hyprctl reload >/dev/null
  errors="$(hyprctl configerrors)"
  if [[ -n "$errors" && "$errors" != *"no errors"* ]]; then
    warn "Hyprland reports config errors:"
    echo "$errors" >&2
  fi
fi

# --- first index ---------------------------------------------------------------

if [[ -n "${PDFIND_NO_INDEX:-}" ]]; then
  step "Skipping the first index (PDFIND_NO_INDEX set) — run 'pdfind index' later"
else
  step "Building the index (first run downloads the ~450 MB model)"
  "$BIN" index
  # The daemon loaded an empty index at start; let it see the new one.
  systemctl --user restart pdfind.service
fi

step "Checking the setup"
"$BIN" doctor || true

printf '\n\033[1;32mDone.\033[0m Press %s to search. Edit what gets indexed with: pdfind config\n' "$KEY"
