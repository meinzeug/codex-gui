#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$SCRIPT_DIR"
exec python3 "$SCRIPT_DIR/codex_gui_supermode.py" -- --working-dir "$SCRIPT_DIR"
