#!/usr/bin/env bash
# Install hongtai-panel for the current user.
#
#   ./install.sh              install everything
#   ./install.sh --no-service skip enabling the autostart service
#
# Re-running is safe: it upgrades in place and leaves your config alone.
set -euo pipefail

cd "$(dirname "$0")"

WANT_SERVICE=1
[[ "${1:-}" == "--no-service" ]] && WANT_SERVICE=0

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }

# -- dependencies ------------------------------------------------------------

say "Checking dependencies"

if ! command -v python3 >/dev/null; then
    echo "python3 is required" >&2
    exit 1
fi
python3 - <<'EOF' || { echo "Python 3.10+ is required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
ok "python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

missing=()
python3 -c "import PIL" 2>/dev/null || missing+=("Pillow")
python3 -c "import psutil" 2>/dev/null || missing+=("psutil")
python3 -c "import serial" 2>/dev/null || missing+=("pyserial")

if [[ ${#missing[@]} -gt 0 ]]; then
    warn "will install via pip: ${missing[*]}"
else
    ok "Pillow, psutil, pyserial present"
fi

command -v ffmpeg >/dev/null && ok "ffmpeg (video backgrounds)" \
    || warn "ffmpeg missing — video and GIF backgrounds will not work"

python3 -c "import gi; gi.require_version('Gtk','4.0')" 2>/dev/null && ok "GTK4 (GUI)" \
    || warn "PyGObject/GTK4 missing — the GUI will not start (CLI still works)"

python3 -c "import gi; gi.require_version('Gst','1.0')" 2>/dev/null && ok "GStreamer (mirroring)" \
    || warn "GStreamer missing — screen mirroring will not work"

# -- package -----------------------------------------------------------------

say "Installing the package"
pip install --user --upgrade . >/dev/null
BIN="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
ok "installed to $BIN/hongtai-panel"

case ":$PATH:" in
    *":$BIN:"*) ;;
    *) warn "$BIN is not on your PATH — add it to your shell profile" ;;
esac

# -- device access -----------------------------------------------------------

say "Granting device access"
RULE=/etc/udev/rules.d/70-hongtai-panel.rules  # install-udev also drops the old 99- copy
if [[ -f "$RULE" ]]; then
    ok "udev rule already installed"
else
    "$BIN/hongtai-panel" install-udev || warn "could not install the udev rule; run it yourself later"
fi

# -- desktop entry -----------------------------------------------------------

say "Installing the desktop entry"
install -Dm644 hongtai-panel.desktop "$HOME/.local/share/applications/hongtai-panel.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
ok "\"LCD Panel\" added to your application menu"

# -- service -----------------------------------------------------------------

if [[ $WANT_SERVICE -eq 1 ]]; then
    say "Setting up autostart"
    install -Dm644 systemd/hongtai-panel.service \
        "$HOME/.config/systemd/user/hongtai-panel.service"
    systemctl --user daemon-reload
    systemctl --user enable --now hongtai-panel.service
    sleep 3
    if systemctl --user is-active --quiet hongtai-panel.service; then
        ok "service running"
    else
        warn "service did not start — check: journalctl --user -u hongtai-panel -n 20"
        warn "if it is a permissions error, replug the panel so the udev rule applies"
    fi
fi

say "Done"
echo "  Launch the GUI:   hongtai-panel gui"
echo "  Check the panel:  hongtai-panel info"
echo "  Logs:             journalctl --user -u hongtai-panel -f"
