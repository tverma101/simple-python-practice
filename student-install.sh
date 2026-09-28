#!/usr/bin/env bash
set -euo pipefail

# Ducky Student Installer
# Downloads from Cloudflare R2 — no GitHub auth needed.
# Usage: curl -sSL https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev/student-install.sh | bash

APP_NAME="Ducky Student"
INSTALL_DIR="${DUCKY_INSTALL_DIR:-$HOME/.ducky}"
BIN_DIR="${DUCKY_BIN_DIR:-$HOME/.local/bin}"
VENV_DIR="$INSTALL_DIR/venv"
SRC_DIR=""
TMP_DIR=""
# The manifest is the single source of truth for the latest student build.
# DUCKY_ZIP_URL remains an escape hatch for local mirrors and tests.
R2_BASE_URL="${DUCKY_R2_BASE_URL:-https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev}"
DUCKY_LATEST_URL="${DUCKY_LATEST_URL:-$R2_BASE_URL/latest.json}"
DUCKY_ZIP_URL="${DUCKY_ZIP_URL:-}"
DUCKY_ZIP_SHA256="${DUCKY_ZIP_SHA256:-}"

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

if [[ -z "$DUCKY_ZIP_URL" ]]; then
  say "Checking latest student version"
  if ! MANIFEST_INFO="$(python3 - "$DUCKY_LATEST_URL" "$R2_BASE_URL" <<'PYLATEST'
import json
import re
import sys
from urllib.request import Request, urlopen

manifest_url, public_base = sys.argv[1:]
try:
    request = Request(
        manifest_url,
        headers={"Accept": "application/json", "User-Agent": "ducky-student-installer"},
    )
    with urlopen(request, timeout=20) as response:
        manifest = json.loads(response.read().decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"could not read {manifest_url}: {exc}")

if manifest.get("build_type") != "student":
    raise SystemExit("latest.json is not a student release")
download_url = manifest.get("download_url") or manifest.get("zip_url")
if not download_url or not download_url.startswith(public_base.rstrip("/") + "/"):
    raise SystemExit("latest.json does not contain a trusted student download URL")
if not re.fullmatch(r"[0-9a-fA-F]{64}", str(manifest.get("sha256", ""))):
    raise SystemExit("latest.json does not contain a valid student archive checksum")
if "/releases/" not in download_url or not download_url.endswith(".zip") or "student-latest.zip" in download_url:
    raise SystemExit("latest.json must point to an immutable versioned student zip")
print(f"{download_url}|{manifest['sha256']}")
PYLATEST
  )"; then
    err "Could not resolve the latest student release from $DUCKY_LATEST_URL."
    exit 1
  fi
  DUCKY_ZIP_URL="${MANIFEST_INFO%%|*}"
  DUCKY_ZIP_SHA256="${MANIFEST_INFO#*|}"
fi

# --- Download from R2 ---
TMP_DIR="$(mktemp -d)"
say "Downloading Ducky from $DUCKY_ZIP_URL"
if command -v curl >/dev/null 2>&1; then
  curl --connect-timeout 20 --max-time 300 -L --fail --retry 3 --retry-delay 1 \
    "$DUCKY_ZIP_URL" -o "$TMP_DIR/ducky.zip"
elif command -v python3 >/dev/null 2>&1; then
  python3 -c "from urllib.request import urlretrieve; urlretrieve('$DUCKY_ZIP_URL', '$TMP_DIR/ducky.zip')"
else
  err "Need curl or python3 to download Ducky."
  exit 1
fi

if [[ -n "$DUCKY_ZIP_SHA256" ]]; then
  if ! python3 - "$TMP_DIR/ducky.zip" "$DUCKY_ZIP_SHA256" <<'PYSHA'
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected = sys.argv[2].lower()
digest = hashlib.sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != expected:
    raise SystemExit(f"checksum mismatch: expected {expected[:16]}..., got {actual[:16]}...")
PYSHA
  then
    err "Downloaded student archive failed checksum verification."
    rm -rf "$TMP_DIR"
    exit 1
  fi
fi

python3 -c "
import zipfile
from pathlib import Path
archive = Path('$TMP_DIR/ducky.zip').resolve()
dest = Path('$TMP_DIR/unpacked').resolve()
dest.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive) as zf:
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise SystemExit(f'unsafe zip symlink: {member.filename}')
        try:
            target.relative_to(dest)
        except ValueError as exc:
            raise SystemExit(f'unsafe zip member: {member.filename}') from exc
    zf.extractall(dest)
"

PYPROJECT_PATH="$(find "$TMP_DIR/unpacked" -maxdepth 3 -name pyproject.toml -print -quit)"
if [[ -z "$PYPROJECT_PATH" ]]; then
  err "Downloaded archive did not contain pyproject.toml."
  rm -rf "$TMP_DIR"
  exit 1
fi
SRC_DIR="$(dirname "$PYPROJECT_PATH")"

# --- Install ---
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

# --- Create student-only shims ---
cat > "$BIN_DIR/ducky" <<SHIM
#!/usr/bin/env bash
# Ducky Student — opens the student menu by default
if [[ "\$#" -eq 0 ]]; then
  # The student subcommand is a CLI namespace and requires a second command
  # (for example, ducky student lessons).  Launch the actual TUI entry point
  # here so a clean install's documented ducky command opens the learning
  # home and its Quiz Center.
  exec "$VENV_DIR/bin/ducky-gui"
else
  exec "$VENV_DIR/bin/ducky" "\$@"
fi
SHIM
chmod +x "$BIN_DIR/ducky"

# --- Set default student config ---
CONFIG_DIR="$HOME/.ducky"
CONFIG_FILE="$CONFIG_DIR/config.json"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_FILE" ]]; then
  cat > "$CONFIG_FILE" <<'CONFIG'
{
  "auto_update": true,
  "auto_update_app": true,
  "llm_backend": "none",
  "ollama_model": "qwen2.5-coder:1.5b",
  "ollama_url": "http://localhost:11434",
  "lmstudio_model": "default",
  "lmstudio_url": "http://localhost:1234",
  "lmstudio_max_tokens": 2500,
  "gemini_api_key": "",
  "gemini_model": "gemini-2.5-flash",
  "openai_api_key": "",
  "openai_model": "gpt-4o-mini",
  "openai_base_url": "https://api.openai.com/v1",
  "student_name": "",
  "teacher_url": "",
  "teacher_token": ""
}
CONFIG
  say "Created student config at $CONFIG_FILE"
fi

# --- Add to PATH ---
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

# --- Cleanup ---
rm -rf "$TMP_DIR"

say "Ducky Student installed successfully."
printf "\nRun:\n  ducky\n\n"
printf "Then choose a lesson and start coding!\n\n"
