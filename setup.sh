#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="${XDG_DESKTOP_DIR:-$(xdg-user-dir DESKTOP 2>/dev/null || true)}"
if [[ -z "${DESKTOP_DIR}" ]]; then
  DESKTOP_DIR="$HOME/Desktop"
fi

APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
LOCAL_DEPS_DIR="$SCRIPT_DIR/.python-deps"
ICON_TARGET="$ICON_DIR/codex-gui.svg"
SUPERMODE_ICON_TARGET="$ICON_DIR/codex-gui-supermode.svg"
APP_LAUNCHER="$APP_DIR/codex-gui.desktop"
SUPERMODE_LAUNCHER="$APP_DIR/codex-gui-supermode.desktop"
DESKTOP_LAUNCHER="$DESKTOP_DIR/Codex GUI.desktop"
DESKTOP_SUPERMODE_LAUNCHER="$DESKTOP_DIR/Codex GUI Supermode.desktop"

APT_PACKAGES=(
  python3
  python3-pip
  python3-gi
  gir1.2-gtk-3.0
  gir1.2-vte-2.91
  alsa-utils
  gstreamer1.0-tools
  gstreamer1.0-plugins-base
  gnome-screenshot
  xdg-desktop-portal
  xdg-desktop-portal-gnome
  desktop-file-utils
)

log() {
  printf '[setup] %s\n' "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1
}

run_root() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
    return
  fi
  if need_cmd sudo; then
    sudo "$@"
    return
  fi
  log "Root-Rechte fehlen fuer: $*"
  exit 1
}

install_system_packages() {
  if ! need_cmd apt-get; then
    log "apt-get nicht gefunden. Ueberspringe Systempakete."
    return
  fi
  log "Installiere benoetigte Systempakete..."
  run_root apt-get update
  run_root apt-get install -y "${APT_PACKAGES[@]}"
}

install_local_python_deps() {
  mkdir -p "$LOCAL_DEPS_DIR"
  log "Installiere lokale Python-Abhaengigkeiten nach $LOCAL_DEPS_DIR ..."
  python3 -m pip install --upgrade --target "$LOCAL_DEPS_DIR" SpeechRecognition
}

check_codex_cli() {
  if need_cmd codex; then
    log "Codex CLI gefunden: $(command -v codex)"
    return
  fi
  log "Warnung: 'codex' wurde nicht gefunden. Bitte Codex CLI separat installieren."
}

write_launcher() {
  local target="$1"
  local name="$2"
  local exec_cmd="$3"
  local comment="$4"
  local icon_path="$5"
  cat >"$target" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$name
Comment=$comment
Exec=$exec_cmd
Path=$SCRIPT_DIR
Icon=$icon_path
Terminal=false
Categories=Development;Utility;
StartupNotify=true
EOF
  chmod +x "$target"
  if need_cmd gio; then
    gio set "$target" metadata::trusted true >/dev/null 2>&1 || true
  fi
}

install_launchers() {
  mkdir -p "$APP_DIR" "$ICON_DIR" "$DESKTOP_DIR"
  install -m 0644 "$SCRIPT_DIR/codex-gui-icon.svg" "$ICON_TARGET"
  install -m 0644 "$SCRIPT_DIR/codex-gui-supermode-icon.svg" "$SUPERMODE_ICON_TARGET"

  write_launcher \
    "$APP_LAUNCHER" \
    "Codex GUI" \
    "$SCRIPT_DIR/start.sh" \
    "Startet die Codex-GUI" \
    "$ICON_TARGET"

  write_launcher \
    "$SUPERMODE_LAUNCHER" \
    "Codex GUI Supermode" \
    "$SCRIPT_DIR/start_supermode.sh" \
    "Startet die Codex-GUI mit automatischem Neustart" \
    "$SUPERMODE_ICON_TARGET"

  install -m 0755 "$APP_LAUNCHER" "$DESKTOP_LAUNCHER"
  install -m 0755 "$SUPERMODE_LAUNCHER" "$DESKTOP_SUPERMODE_LAUNCHER"
  if need_cmd gio; then
    gio set "$DESKTOP_LAUNCHER" metadata::trusted true >/dev/null 2>&1 || true
    gio set "$DESKTOP_SUPERMODE_LAUNCHER" metadata::trusted true >/dev/null 2>&1 || true
  fi

  if need_cmd update-desktop-database; then
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
  fi
}

pin_to_gnome_dock() {
  if ! need_cmd gsettings; then
    return
  fi
  if ! gsettings writable org.gnome.shell favorite-apps >/dev/null 2>&1; then
    return
  fi

  python3 - <<'PY'
import ast
import subprocess

desktop_ids = [
    "codex-gui.desktop",
    "codex-gui-supermode.desktop",
]
try:
    raw = subprocess.check_output(
        ["gsettings", "get", "org.gnome.shell", "favorite-apps"],
        text=True,
    ).strip()
    favorites = ast.literal_eval(raw)
except Exception:
    favorites = []

changed = False
for desktop_id in desktop_ids:
    if desktop_id not in favorites:
        favorites.append(desktop_id)
        changed = True

if changed:
    subprocess.run(
        [
            "gsettings",
            "set",
            "org.gnome.shell",
            "favorite-apps",
            repr(favorites),
        ],
        check=False,
    )
PY
}

main() {
  chmod +x "$SCRIPT_DIR/start.sh" "$SCRIPT_DIR/start_supermode.sh"
  install_system_packages
  install_local_python_deps
  check_codex_cli
  install_launchers
  pin_to_gnome_dock
  log "Fertig."
  log "Normaler Start: $SCRIPT_DIR/start.sh"
  log "Supermode: $SCRIPT_DIR/start_supermode.sh"
  log "Desktop-Launcher: $DESKTOP_LAUNCHER"
}

main "$@"
