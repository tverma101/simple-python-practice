#!/usr/bin/env bash
# Build a student-only Ducky distribution (wheel + zip).
# Excludes teacher-only modules. Keeps all shared/student modules.
set -euo pipefail

DIST_NAME="ducky_student"
ROOT="$(cd "$(dirname "$0")" && pwd)"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Keep the release version in one place: pyproject.toml.  This prevents the
# student filename, manifest, installer, and package metadata from drifting.
VERSION="$(awk -F'\"' '$1 ~ /^version[[:space:]]*=/ {print $2; exit}' "$ROOT/pyproject.toml")"
if [[ -z "$VERSION" ]]; then
    echo "ERROR: Could not find project version in pyproject.toml" >&2
    exit 1
fi
PUBLIC_BASE="${R2_PUBLIC_BASE:-https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev}"

echo ""
echo "  Building student-only Ducky v${VERSION}"
echo ""

# ── 1. Copy source ──────────────────────────────────────────────
echo "  [1/6] Copying source..."
cp -r "$ROOT/src/ducky" "$TMPDIR/ducky"

# ── 2. Remove teacher-only modules ──────────────────────────────
echo "  [2/6] Removing teacher modules..."
rm -f "$TMPDIR/ducky/server.py"           # HTTP server
rm -f "$TMPDIR/ducky/classroom.py"        # SQLite classroom DB
rm -f "$TMPDIR/ducky/teacher_workspace.py" # Teacher workspace
rm -f "$TMPDIR/ducky/teacher_backend.py"  # Teacher backend client
rm -f "$TMPDIR/ducky/importer.py"         # Assignment import (teacher)
rm -f "$TMPDIR/ducky/validator.py"        # Pack validation (teacher)
rm -f "$TMPDIR/ducky/lesson_builder.py"   # Pack building (teacher)
rm -f "$TMPDIR/ducky/authoring.py"        # Teacher authoring helpers
rm -f "$TMPDIR/ducky/cli/teacher_commands.py"
rm -f "$TMPDIR/ducky/gui/teacher.py"
rm -f "$TMPDIR/ducky/gui/teacher_gui.py"
echo "      Removed teacher-only modules"

# Ship the teacher-curated platform quiz bank before fingerprinting. This keeps
# the release identity tied to the actual practice catalog as well as the
# Python source, and makes it impossible for a future packaging refactor to
# silently omit the root-level quiz files.
if [[ -d "$ROOT/quizzes" ]]; then
    mkdir -p "$TMPDIR/ducky/data/quizzes"
    cp "$ROOT/quizzes"/*.json "$TMPDIR/ducky/data/quizzes/" 2>/dev/null || true
fi
QUIZ_COUNT=$(find "$TMPDIR/ducky/data/quizzes" -maxdepth 1 -type f -name '*.json' | wc -l | tr -d ' ')
if [[ "$QUIZ_COUNT" -eq 0 ]]; then
    echo "  ERROR: Student build has no bundled quiz JSON files" >&2
    exit 1
fi
echo "      Bundled ${QUIZ_COUNT} platform quiz file(s)"

# Fingerprint the student payload after teacher-only files are removed, then
# add a release nonce. The nonce matters because a same-version rebuild can
# contain different archive bytes and must never reuse a CDN-cached URL.
BUILD_ID="$(python3 - "$TMPDIR/ducky" <<'PYFINGERPRINT'
import hashlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "__init__.py"):
    digest.update(path.relative_to(root).as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
print(digest.hexdigest()[:16])
PYFINGERPRINT
)"
BUILD_ID="${BUILD_ID}-$(python3 -c 'import time; print(time.time_ns())')"
echo "      Student release id: ${BUILD_ID}"

# ── 3. Patch __init__.py — student build type, version suffix ──
echo "  [3/6] Patching __init__.py..."
python3 - "$TMPDIR/ducky/__init__.py" "$VERSION" "$BUILD_ID" <<'PYINIT'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = text.replace('__build_type__ = "full"', '__build_type__ = "student"')
text = re.sub(r'__version__\s*=\s*["\'][^"\']+["\']',
              f'__version__ = "{sys.argv[2]}s"', text, count=1)
text = re.sub(r'__build_id__\s*=\s*["\'][^"\']+["\']',
              f'__build_id__ = "{sys.argv[3]}"', text, count=1)
path.write_text(text, encoding="utf-8")
print(f"      Patched __init__.py (student build, v{sys.argv[2]}s, build {sys.argv[3]})")
PYINIT

# ── 3. Patch cli/__init__.py — remove teacher export ───────────
echo "  [4/6] Patching cli/__init__.py..."
cat > "$TMPDIR/ducky/cli/__init__.py" <<'EOF'
"""Ducky CLI — modular command-line interface."""

from .main import main
from .student_commands import main_student

__all__ = ["main", "main_student"]
EOF

# ── 4. Patch gui/app.py — remove teacher imports and menu ──────
echo "  [5/7] Patching gui/app.py..."
python3 - "$TMPDIR/ducky/gui/app.py" <<'PYAPP'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)
out = []
skip_teacher_menu = False

for line in lines:
    stripped = line.strip()

    # Skip teacher-only imports
    if "from ..server import DuckyServer" in stripped:
        continue
    if "from .teacher import TeacherMenuMixin" in stripped:
        continue

    # Remove TeacherMenuMixin from class bases
    if "TeacherMenuMixin" in line and "class DuckyTerminal" in line:
        line = line.replace("TeacherMenuMixin, ", "")

    # Skip teacher_server attribute init
    if "self.teacher_server: DuckyServer | None = None" in stripped:
        continue

    # Skip teacher menu item [7]
    if 'print(f"  {CYAN}[7]{RESET}  Teacher' in stripped:
        continue

    # Renumber Settings from [8] to [7]
    if '{CYAN}[8]{RESET}  Settings' in stripped:
        line = line.replace("[8]", "[7]")

    # Skip teacher menu handler
    if 'elif choice == "7":' in stripped:
        skip_teacher_menu = True
        continue
    if skip_teacher_menu:
        if stripped.startswith("elif ") or stripped.startswith("else:"):
            skip_teacher_menu = False
        else:
            continue

    # Renumber settings handler from "8" to "7"
    if stripped == 'elif choice == "8":':
        line = line.replace('"8"', '"7"')

    out.append(line)

path.write_text("".join(out))
print("      Patched gui/app.py")
PYAPP

# ── 5. Patch cli/main.py — remove teacher commands ─────────────
echo "  [6/7] Patching cli/main.py..."
python3 - "$TMPDIR/ducky/cli/main.py" <<'PYMAIN'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text().splitlines(keepends=True)
out = []
i = 0
n = len(lines)


def skip_block(start_idx, prefixes):
    """Skip lines while any stripped line starts with one of the prefixes.
    Also consumes multi-line statements (tracks parenthesis nesting)."""
    j = start_idx
    paren_depth = 0
    while j < n:
        s = lines[j].strip()
        if any(s.startswith(p) for p in prefixes):
            paren_depth += s.count('(') - s.count(')')
            j += 1
            continue
        # If we're inside a multi-line call, keep skipping
        if paren_depth > 0:
            paren_depth += s.count('(') - s.count(')')
            j += 1
            continue
        break
    return j


while i < n:
    line = lines[i]
    stripped = line.strip()

    # ── Skip teacher_commands import (multi-line) ──
    if "from .teacher_commands import" in line:
        i += 1
        while i < n and ")" not in lines[i]:
            i += 1
        i += 1  # skip closing paren
        continue

    # ── Skip "# Unified check handler" comment block + _handle_check function ──
    if "Unified check handler" in stripped:
        # Skip comment block (lines with # or ═)
        while i < n and (lines[i].strip().startswith("#") or lines[i].strip() == ""):
            i += 1
        # Skip function definition
        if i < n and lines[i].strip().startswith("def _handle_check("):
            i += 1
            while i < n:
                s = lines[i].strip()
                if s.startswith("def ") or s.startswith("class "):
                    break
                i += 1
        continue

    # ── Skip elif argv[0] == "teacher": block ──
    if 'elif argv[0] == "teacher":' in stripped or "elif argv[0] == 'teacher':" in stripped:
        i += 2
        continue

    # ── Skip teacher subcommand in epilog ──
    if 'ducky teacher <command>' in stripped:
        i += 1
        continue

    # ── Skip check subparser (entire block) ──
    if stripped.startswith('check_p = sub.add_parser'):
        i = skip_block(i + 1, ["check_p.add_argument"])
        continue

    # ── Skip "# check" comment ──
    if stripped == "# check":
        i += 1
        continue

    # ── Skip student old-style subparser (entire block) ──
    if stripped.startswith('student_old = sub.add_parser'):
        i = skip_block(i + 1, ["student_old.add_argument"])
        continue

    # ── Skip "# student (old-style)" comment ──
    if stripped.startswith("# student (old-style"):
        i += 1
        continue

    # ── Skip dataset subparser (entire block) ──
    if stripped.startswith('dataset_p = sub.add_parser'):
        i = skip_block(i + 1, [
            "dataset_p.add_argument", "dataset_sub",
            "ds_export", "ds_preview", "ds_stats",
        ])
        continue

    # ── Skip redact subparser (entire block) ──
    if stripped.startswith('redact_p = sub.add_parser'):
        i = skip_block(i + 1, ["redact_p.add_argument"])
        continue

    # ── Skip scan subparser (entire block) ──
    if stripped.startswith('scan_p = sub.add_parser'):
        i = skip_block(i + 1, ["scan_p.add_argument"])
        continue

    # ── Skip import subparser (entire block) ──
    if stripped.startswith('import_p = sub.add_parser'):
        i = skip_block(i + 1, ["import_p.add_argument"])
        continue

    # ── Skip "# scan" / "# dataset" / "# redact" / "# import" comments ──
    if stripped in ("# scan", "# dataset", "# redact", "# import"):
        i += 1
        continue

    # ── Skip orphaned ═══ decoration lines ──
    if stripped.startswith("# ═") and len(stripped) > 10:
        i += 1
        continue

    # ── Skip teacher-related args in config set ──
    if stripped.startswith('config_set.add_argument("--teacher'):
        i += 1
        continue
    if stripped.startswith('config_set.add_argument("--student-name'):
        i += 1
        continue
    if stripped.startswith('config_set.add_argument("--teacher-upload-all'):
        i += 1
        continue

    # ── Skip dispatch blocks: check, student old-style, scan, dataset, redact ──
    if stripped.startswith('if args.cmd == "check":'):
        i += 2
        continue
    if stripped.startswith('if args.cmd == "student" and args.student_pack'):
        i += 2
        continue
    if stripped.startswith('if args.cmd == "scan":'):
        i += 2
        continue
    if stripped.startswith('if args.cmd == "dataset":'):
        i += 2
        continue
    if stripped.startswith('if args.cmd == "redact":'):
        i += 2
        continue

    # ── Skip import handler block (multi-line) ──
    if stripped.startswith('if args.cmd == "import":'):
        i += 1
        while i < n:
            s = lines[i].strip()
            # Stop at next top-level handler or comment block
            if (s.startswith('if args.cmd ==') or s.startswith('elif args.cmd ==')
                    or (s.startswith('#') and '═' in s)):
                break
            i += 1
        continue

    out.append(line)
    i += 1

# ── Clean up config_set handler: remove teacher refs ──
cleaned = []
skip_config_teacher = False
for line in out:
    s = line.strip()
    # Remove teacher_url, teacher_token, student_name, teacher_submit_url,
    # teacher_fetch_url, teacher_access_code, teacher_upload_all from config dict
    if any(k in s for k in [
        '"teacher_url": args.teacher_url',
        '"teacher_token": args.teacher_token',
        '"student_name": args.student_name',
        '"teacher_submit_url": args.teacher_submit_url',
        '"teacher_fetch_url": args.teacher_fetch_url',
        '"teacher_access_code": args.teacher_code',
        '"teacher_upload_all": args.teacher_upload_all',
    ]):
        continue
    # Remove "if args.teacher_code:" block
    if s.startswith("if args.teacher_code:"):
        skip_config_teacher = True
        continue
    if skip_config_teacher:
        if s.startswith("updates["):
            continue
        skip_config_teacher = False
    cleaned.append(line)

path.write_text("".join(cleaned))
print("      Patched cli/main.py")
PYMAIN

# ── 6. Build wheel + zip ───────────────────────────────────────
echo "  [7/7] Building distribution..."

BUILD_DIR="$TMPDIR/build"
mkdir -p "$BUILD_DIR/src"
cp -r "$TMPDIR/ducky" "$BUILD_DIR/src/"

# Copy pyproject.toml with student-only name
cp "$ROOT/pyproject.toml" "$BUILD_DIR/pyproject.toml"
sed -i '' "s/^name = \"ducky\"/name = \"${DIST_NAME}\"/" "$BUILD_DIR/pyproject.toml"
sed -i '' '/^ducky-teacher = /d' "$BUILD_DIR/pyproject.toml"

cd "$BUILD_DIR"
# Use venv python if available, otherwise system python
PYTHON="python3"
if [[ -f "$ROOT/.venv/bin/python3" ]]; then
    PYTHON="$ROOT/.venv/bin/python3"
fi
"$PYTHON" -m pip install --quiet --upgrade build 2>/dev/null || true
"$PYTHON" -m build --wheel --outdir "$TMPDIR/wheels" 2>&1 | tail -5

WHEEL=$(ls "$TMPDIR/wheels/"*.whl 2>/dev/null | head -1)
if [[ -z "$WHEEL" ]]; then
    echo "  ERROR: Wheel build failed"
    exit 1
fi

# Create zip from the build directory (includes pyproject.toml + src/ducky/ + examples/)
cd "$BUILD_DIR"
# Copy examples/packs into the build dir so students get starter lessons
cp -r "$ROOT/examples" "$BUILD_DIR/examples" 2>/dev/null || true
zip -r "$TMPDIR/${DIST_NAME}-v${VERSION}.zip" src/ducky/ pyproject.toml examples/ \
    -x "*.pyc" "*/__pycache__/*" "*.DS_Store" 2>/dev/null

# Copy to downloads/
mkdir -p "$ROOT/downloads"
cp "$WHEEL" "$ROOT/downloads/"
cp "$TMPDIR/${DIST_NAME}-v${VERSION}.zip" "$ROOT/downloads/"

# Generate the public student release manifest from the artifacts just built.
# The uploader publishes stable aliases for these URLs, so installers never
# need a version hardcoded into a shell script or README.
python3 - "$ROOT" "$VERSION" "$BUILD_ID" "$ROOT/downloads/$(basename "$WHEEL")" \
    "$ROOT/downloads/${DIST_NAME}-v${VERSION}.zip" "$PUBLIC_BASE" <<'PYMETA'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
version = sys.argv[2]
build_id = sys.argv[3]
wheel = Path(sys.argv[4])
archive = Path(sys.argv[5])
public_base = sys.argv[6].rstrip("/")
release_prefix = f"releases/v{version}-{build_id}"

def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

manifest = {
    "build_type": "student",
    "package": "ducky_student",
    "version": version,
    "build_id": build_id,
    # The updater and installer consume immutable, versioned URLs. Stable
    # aliases remain available for README links, but a mutable alias can
    # change between manifest fetch and checksum verification.
    "download_url": f"{public_base}/{release_prefix}/{archive.name}",
    "zip_url": f"{public_base}/{release_prefix}/{archive.name}",
    "wheel_url": f"{public_base}/{release_prefix}/{wheel.name}",
    "latest_zip_url": f"{public_base}/student-latest.zip",
    "latest_wheel_url": f"{public_base}/student-latest.whl",
    "versioned_zip_url": f"{public_base}/{release_prefix}/{archive.name}",
    "versioned_wheel_url": f"{public_base}/{release_prefix}/{wheel.name}",
    "sha256": sha256(archive),
    "wheel_sha256": sha256(wheel),
    "min_python": "3.10",
    "release_notes": "Student-only release generated from the current project version.",
    "released_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}

dist = root / "dist"
dist.mkdir(exist_ok=True)
manifest_text = json.dumps(manifest, indent=2) + "\n"
(dist / "latest.json").write_text(manifest_text, encoding="utf-8")
# Keep the checked-in copy useful for local tooling; the uploader publishes
# the generated dist copy to R2.
(root / "latest.json").write_text(manifest_text, encoding="utf-8")
print(f"      Wrote {dist / 'latest.json'} and {root / 'latest.json'}")
PYMETA

echo ""
echo "  Done! Files in downloads/:"
ls -lh "$ROOT/downloads/"* 2>/dev/null | sed 's/^/    /'
echo ""
echo "  Wheel: $(basename "$WHEEL")"
echo "  Zip:   ${DIST_NAME}-v${VERSION}.zip"
echo ""
