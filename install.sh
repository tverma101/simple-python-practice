#!/usr/bin/env bash
set -euo pipefail

APP_NAME="Ducky"
INSTALL_DIR="${DUCKY_INSTALL_DIR:-$HOME/.ducky}"
BIN_DIR="${DUCKY_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_DIR/venv"
SRC_DIR=""
TMP_DIR=""

say() { printf "\033[1;36m[Ducky]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[Ducky ERROR]\033[0m %s\n" "$*" >&2; }

need_python() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
  else
    return 1
  fi
}

if ! need_python; then
  err "Python 3.10+ is required. Install Python 3, then rerun this installer."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/src/ducky" ]]; then
  SRC_DIR="$SCRIPT_DIR"
elif [[ -n "${DUCKY_ZIP_URL:-}" ]]; then
  TMP_DIR="$(mktemp -d)"
  say "Downloading Ducky from $DUCKY_ZIP_URL"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --retry 3 --retry-delay 1 "$DUCKY_ZIP_URL" -o "$TMP_DIR/ducky.zip"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - <<PY
from urllib.request import urlretrieve
urlretrieve("$DUCKY_ZIP_URL", "$TMP_DIR/ducky.zip")
PY
  else
    err "Need curl or python3 to download Ducky."
    exit 1
  fi
  python3 - <<PY
from pathlib import Path
import zipfile

archive = Path("$TMP_DIR/ducky.zip").resolve()
dest = Path("$TMP_DIR/unpacked").resolve()
dest.mkdir(parents=True, exist_ok=True)

with zipfile.ZipFile(archive) as zf:
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest)
        except ValueError as exc:
            raise SystemExit(f"unsafe zip member: {member.filename}") from exc
    zf.extractall(dest)
PY
  PYPROJECT_PATH="$(find "$TMP_DIR/unpacked" -maxdepth 3 -name pyproject.toml -print -quit)"
  if [[ -z "$PYPROJECT_PATH" ]]; then
    err "Downloaded archive did not contain pyproject.toml."
    exit 1
  fi
  SRC_DIR="$(dirname "$PYPROJECT_PATH")"
else
  err "Could not find Ducky source. Run this from the repo root, or set DUCKY_ZIP_URL."
  exit 1
fi

say "Installing into $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install "$SRC_DIR" >/dev/null

# Copy example lesson packs so students see lessons immediately
PACKS_DEST="$HOME/.ducky/packs"
PACKS_SRC="$SRC_DIR/examples/packs"
if [[ -d "$PACKS_SRC" ]]; then
  mkdir -p "$PACKS_DEST"
  cp "$PACKS_SRC"/*.duckypack "$PACKS_DEST/" 2>/dev/null || true
  say "Copied lesson packs to $PACKS_DEST"
fi

cat > "$BIN_DIR/ducky" <<SHIM
#!/usr/bin/env bash
exec "$VENV_DIR/bin/ducky" "\$@"
SHIM
chmod +x "$BIN_DIR/ducky"

cat > "$BIN_DIR/ducky-gui" <<SHIM
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" -m ducky.gui
SHIM
chmod +x "$BIN_DIR/ducky-gui"

if [[ "${DUCKY_NO_PROFILE:-0}" != "1" ]]; then
  PROFILE_FILE=""
  case "${SHELL:-}" in
    */zsh) PROFILE_FILE="$HOME/.zshrc" ;;
    */bash) PROFILE_FILE="$HOME/.bashrc" ;;
    *) PROFILE_FILE="$HOME/.profile" ;;
  esac

  if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    touch "$PROFILE_FILE"
    if ! grep -q "export PATH=\"$BIN_DIR:\$PATH\"" "$PROFILE_FILE" 2>/dev/null; then
      printf '\n# Ducky local tutor\nexport PATH="%s:$PATH"\n' "$BIN_DIR" >> "$PROFILE_FILE"
      say "Added $BIN_DIR to PATH in $PROFILE_FILE. Restart Terminal or run: export PATH=\"$BIN_DIR:\$PATH\""
    fi
  fi
fi

say "Ducky installed successfully."
printf "\nRun:\n  ducky-gui\n\nOr:\n  ducky --help\n\n"

if [[ -n "$TMP_DIR" ]]; then
  rm -rf "$TMP_DIR"
fi
