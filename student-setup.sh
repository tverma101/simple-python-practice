#!/usr/bin/env bash
set -euo pipefail

# Ducky one-command installer
# Downloads from Cloudflare R2 (no auth needed)
# Usage: curl -fsSL <r2_url>/ducky.zip | bash
#
# For private repos (requires GitHub auth):
#   curl -fsSL <gist_raw_url> | bash

R2_BASE_URL="${DUCKY_R2_BASE_URL:-https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev}"
DUCKY_LATEST_URL="${DUCKY_LATEST_URL:-$R2_BASE_URL/latest.json}"
DUCKY_ZIP_URL="${DUCKY_ZIP_URL:-}"
DUCKY_ZIP_SHA256="${DUCKY_ZIP_SHA256:-}"
CLONE_DIR=""
TOKEN=""

# Source repository for the GitHub-auth fallback path below. Owner and repo are
# the single source of truth: both the SSH and the HTTPS URL are derived from
# them, and every part is overridable so a fork can install itself the same way.
REPO_OWNER="${DUCKY_REPO_OWNER:-tverma101}"
REPO_NAME="${DUCKY_REPO_NAME:-simple-python-practice}"
REPO_URL="${DUCKY_REPO_URL:-git@github.com:${REPO_OWNER}/${REPO_NAME}.git}"
HTTPS_REPO_URL="${DUCKY_HTTPS_REPO_URL:-https://github.com/${REPO_OWNER}/${REPO_NAME}.git}"
BRANCH="${DUCKY_BRANCH:-main}"

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
request = Request(manifest_url, headers={"Accept": "application/json", "User-Agent": "ducky-student-installer"})
try:
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

# --- Download method ---
# If DUCKY_ZIP_URL is set or provided, download directly (no auth needed)
# Otherwise, fall back to git clone (requires auth for private repos)

if [[ -n "$DUCKY_ZIP_URL" ]]; then
  # Direct download from R2 or similar hosting
  TMP_DIR=$(mktemp -d)
  CLONE_DIR="$TMP_DIR/ducky_setup"
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
dest = Path('$CLONE_DIR').resolve()
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
  bash "$CLONE_DIR/install.sh"
  rm -rf "$TMP_DIR"
  say "Done! Open a new terminal and run:"
  printf "\n  ducky\n\n"
  exit 0
fi

# --- Git clone method (requires auth for private repos) ---
CLONE_DIR="$(mktemp -d)/ducky_setup"

# Clone over HTTPS with the token supplied as an Authorization header instead of
# as `https://TOKEN@github.com/...`. The URL form leaks the token twice over: it
# is visible in `ps` for the life of the clone, and git records it in the new
# clone's own .git/config as remote.origin.url, where it outlives the install —
# and it is the form a student ends up pasting into their shell history after
# being told to "just use the token in the URL". The header form keeps it out of
# all three. It is passed via GIT_CONFIG_* rather than `git -c` so that it is not
# in git's argv either, and it is never written to a file. The token is unset as
# soon as the clone returns.
clone_with_token() {
  local header probe
  header="Authorization: Basic $(printf 'x-access-token:%s' "$TOKEN" | base64 | tr -d '\n')"
  if probe="$(GIT_CONFIG_COUNT=1 \
      GIT_CONFIG_KEY_0=http.extraHeader \
      GIT_CONFIG_VALUE_0="$header" \
      git config --get http.extraHeader 2>/dev/null)" && [[ -n "$probe" ]]; then
    GIT_CONFIG_COUNT=1 \
    GIT_CONFIG_KEY_0=http.extraHeader \
    GIT_CONFIG_VALUE_0="$header" \
      git clone --depth 1 --branch "$BRANCH" "$HTTPS_REPO_URL" "$CLONE_DIR"
  else
    # git older than 2.31 has no GIT_CONFIG_* support. Still no token in the URL.
    git -c "http.extraheader=$header" clone --depth 1 --branch "$BRANCH" \
      "$HTTPS_REPO_URL" "$CLONE_DIR"
  fi
}

# --- Authentication ---
# Prefer credentials this machine already has, and only ask for a token when
# there is nothing to use.
AUTH_METHOD=""
CLONE_SOURCE=""
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  AUTH_METHOD="gh"
  # HTTPS, not SSH: `gh auth login` configures the git credential helper, which
  # only an HTTPS remote will consult.
  CLONE_SOURCE="$HTTPS_REPO_URL"
  say "Using GitHub CLI authentication."
else
  # Check for existing git credentials: HTTPS first (that is what gh and the
  # osxkeychain helper are wired up for), then SSH.
  if git ls-remote "$HTTPS_REPO_URL" HEAD >/dev/null 2>&1; then
    AUTH_METHOD="git"
    CLONE_SOURCE="$HTTPS_REPO_URL"
    say "Using existing git credentials."
  elif git ls-remote "$REPO_URL" HEAD >/dev/null 2>&1; then
    AUTH_METHOD="git"
    CLONE_SOURCE="$REPO_URL"
    say "Using existing git credentials (SSH)."
  else
    echo ""
    say "This is a private repository. You need to authenticate."
    echo ""
    echo "  Option 1: Run 'gh auth login' first, then re-run this script"
    echo "  Option 2: Enter a GitHub Personal Access Token below"
    echo "            (create one at: https://github.com/settings/tokens)"
    echo "            Required scope: repo"
    echo ""
    # Hidden input, and read from the terminal rather than stdin: this installer
    # is documented as `curl … | bash`, so stdin is the script itself and a
    # plain read would swallow the next line of the program. The probe runs in a
    # subshell so that a machine with no controlling terminal fails quietly.
    if [[ -c /dev/tty ]] && ( exec 3>/dev/tty ) 2>/dev/null; then
      printf "  GitHub token (or press Enter to try gh auth login): " > /dev/tty
      read -rs TOKEN < /dev/tty || TOKEN=""
      printf '\n' > /dev/tty
    else
      read -rsp "  GitHub token (or press Enter to try gh auth login): " TOKEN || TOKEN=""
    fi
    if [[ -z "$TOKEN" ]]; then
      if command -v gh >/dev/null 2>&1; then
        say "Running gh auth login..."
        gh auth login
        AUTH_METHOD="gh"
        CLONE_SOURCE="$HTTPS_REPO_URL"
      else
        err "No token provided and gh CLI not found. Install gh or provide a token."
        exit 1
      fi
    else
      AUTH_METHOD="token"
    fi
  fi
fi

# --- Clone ---
say "Cloning Ducky repo..."
case "$AUTH_METHOD" in
  gh)
    if ! git clone --depth 1 --branch "$BRANCH" "$CLONE_SOURCE" "$CLONE_DIR" 2>/dev/null; then
      err "Clone failed. Try: gh auth login"
      exit 1
    fi
    ;;
  token)
    if ! clone_with_token 2>/dev/null; then
      unset TOKEN
      err "Clone failed. Check your token has 'repo' scope."
      exit 1
    fi
    unset TOKEN
    ;;
  git)
    if ! git clone --depth 1 --branch "$BRANCH" "$CLONE_SOURCE" "$CLONE_DIR" 2>/dev/null; then
      err "Clone failed. Check your git credentials."
      exit 1
    fi
    ;;
esac

# --- Install ---
say "Running installer..."
bash "$CLONE_DIR/install.sh"

# --- Cleanup ---
say "Cleaning up..."
rm -rf "$CLONE_DIR"

say "Done! Open a new terminal and run:"
printf "\n  ducky\n\n"
