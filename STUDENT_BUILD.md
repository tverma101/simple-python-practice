# Student-Only Build — Separation Guide

This document defines the boundary between the **teacher version** (full codebase) and the **student version** (stripped distribution). Follow these rules to keep them separate.

## Module Classification

### Teacher-Only (removed from student build)

These files exist only in the teacher version. The build script deletes them.

```
src/ducky/server.py              # HTTP server (teacher-student connection)
src/ducky/classroom.py           # SQLite database (classes, students, packs, submissions)
src/ducky/teacher_workspace.py   # Teacher workspace management
src/ducky/teacher_backend.py     # Teacher backend client
src/ducky/importer.py            # Assignment import (teacher tool)
src/ducky/validator.py           # Pack validation (teacher tool)
src/ducky/lesson_builder.py      # Pack building (teacher tool)
src/ducky/cli/teacher_commands.py # All teacher CLI commands
src/ducky/gui/teacher.py         # Teacher GUI menu mixin
```

### Shared (used by both teacher and student)

These files exist in both versions. Students need them to connect to a teacher server.

```
src/ducky/student_client.py      # Student↔teacher HTTP client (connect, submit, update)
src/ducky/background_sync.py     # Background sync with teacher server
src/ducky/workflows.py           # Shared workflows (connect, submit, test)
src/ducky/config.py              # Configuration management
src/ducky/paths.py               # Application directory paths
src/ducky/pack.py                # .duckypack format (read/write)
src/ducky/quiz.py                # Custom quiz questions (student-facing — NOT stripped)
src/ducky/runner.py              # Test runner (pytest integration)
src/ducky/tutor.py               # Hint generation engine
src/ducky/diagnostics.py         # Rule-based failure classification
src/ducky/function_detector.py   # Function signature mismatch detection
src/ducky/llm_client.py          # LLM backends (Ollama, LM Studio, Gemini)
src/ducky/reports.py             # Report storage and rendering
src/ducky/progress.py            # Progress tracking (SQLite)
src/ducky/self_update.py         # Auto-update via GitHub + R2
src/ducky/workspace.py           # Workspace management
src/ducky/editor.py              # VS Code detection and file opening
src/ducky/display.py             # Display utilities
src/ducky/dataset_builder.py     # Dataset JSONL export
src/ducky/reports_dataset.py     # Report dataset generation
src/ducky/network.py             # Network utilities
src/ducky/init_workspace.py      # Initialize new workspace
src/ducky/__init__.py            # Version info
```

### Student-Only (only in student version)

These files are not used by teacher code.

```
src/ducky/cli/student_commands.py # Student CLI commands
src/ducky/gui/student.py         # Legacy student workflow/service mixin
src/ducky/gui/student_desktop.py # Windowed student GUI and quiz browser
src/ducky/gui/settings.py        # Settings GUI (shared LLM config)
src/ducky/gui/widgets.py         # Shared GUI widgets
src/ducky/gui/app.py             # Entry point (patched for student GUI)
```

## What the Build Script Does

`build_student.sh` creates a student-only wheel and zip in `downloads/`.

### Step 1: Copy source
Copies `src/ducky/` to a temp directory.

### Step 2: Delete teacher modules
Removes all 9 teacher-only files listed above.

### Step 3: Patch `cli/__init__.py`
Rewrites to export only `main` and `main_student` (removes `main_teacher`).

### Step 4: Patch `gui/app.py`
- Removes `from ..server import DuckyServer`
- Removes `from .teacher import TeacherMenuMixin`
- Removes `TeacherMenuMixin` from class bases
- Removes `self.teacher_server` attribute
- Removes Teacher menu item [7]
- Renumbers Settings from [8] to [7]
- Keeps `ducky-gui` available; the student build opens a native windowed
  learning home with a searchable quiz browser, timed quiz flow, lessons,
  coding editor, isolated tests, classroom sync, progress, and settings.

### Step 5: Patch `cli/main.py`
Removes:
- `from .teacher_commands import (...)` block
- `elif argv[0] == "teacher":` dispatch
- `_handle_check` function (uses `teacher_backend`)
- `check`, `student old-style`, `dataset`, `redact`, `scan`, `import` subparsers
- `if args.cmd == "check"/"dataset"/"redact"/"scan"/"import"` dispatch blocks
- Teacher-related args from `config set` (`--teacher-url`, `--teacher-token`, etc.)
- Teacher config keys from the config set handler dict

### Step 6: Build
Creates versioned student wheel/zip artifacts in `downloads/` and generates
`dist/latest.json` from the version in `pyproject.toml`. The manifest points to
immutable build-specific URLs (`releases/<version>-<build_id>/...`) and includes
checksums; stable aliases remain for manual downloads. Both expose the student GUI through `ducky-gui`; the archive
excludes Python bytecode and macOS metadata. The uploader validates the student
archive, publishes immutable objects before `latest.json`, and refreshes
`student-latest.whl` and `student-latest.zip` aliases in R2.

## Import Rules

### ❌ NEVER import teacher-only modules from shared code

```python
# WRONG — will crash in student build
from ..server import DuckyServer
from ..classroom import ClassroomDB
from ..teacher_workspace import get_built_packs_dir
from ..teacher_backend import TeacherBackendClient
from ..importer import import_assignment
from ..validator import validate_pack
from ..lesson_builder import build_all_packs
```

### ✅ Use lazy imports for teacher-only features in shared code

If a shared module needs a teacher-only feature, use a lazy import inside the function:

```python
# OK — only imported when teacher feature is used
def serve_teacher():
    from ..server import DuckyServer
    ...
```

The build script removes these code paths, so the lazy import is never reached.

### ✅ Config keys are safe

Config keys like `teacher_url`, `teacher_token`, `teacher_access_code` are safe to keep in shared code. Students need them to connect to a teacher server. The build script only removes the CLI args that set these values.

### ✅ student_client.py is shared

`student_client.py` is the HTTP client students use to communicate with the teacher server. It imports nothing from teacher-only modules.

## Verification

After building, run these checks:

### 1. Build and verify no teacher modules
```bash
bash build_student.sh
python3 -c "
import zipfile
zf = zipfile.ZipFile('downloads/ducky_student-v<VERSION>.zip')
teacher_files = ['server.py', 'classroom.py', 'teacher_workspace.py',
                 'teacher_backend.py', 'importer.py', 'validator.py',
                 'lesson_builder.py']
for tf in teacher_files:
    found = [n for n in zf.namelist() if n.endswith('/' + tf)]
    if found:
        print(f'STILL PRESENT: {found}')
"
```

### 1b. Verify quiz support is kept (student-facing)
`quizzes.json` and `quiz.py` are student-facing — they must survive the strip.
```bash
python3 -c "
import zipfile
zf = zipfile.ZipFile('downloads/ducky_student-v<VERSION>.zip')
names = zf.namelist()
assert any(n.endswith('/quiz.py') for n in names), 'quiz.py was stripped!'
# Example packs should still bundle quizzes.json
assert any(n.endswith('quizzes.json') for n in names), 'no quizzes.json bundled!'
print('Quiz support present in student build.')
"
```

### 2. Verify no teacher imports in student zip
```bash
cd /tmp && mkdir verify && cd verify && python3 -c "
import zipfile
zf = zipfile.ZipFile('/path/to/downloads/ducky_student-v<VERSION>.zip')
for name in zf.namelist():
    if name.endswith('.py'):
        zf.extract(name, '.')
" && grep -rn 'teacher_backend\|_handle_check\|_handle_teacher\|main_teacher\|from.*teacher_commands' ducky/ || echo 'Clean'
```

### 3. Run full test suite
```bash
python -m pytest
# All 332+ tests must pass
```

### 4. Verify GUI menu numbering
The student GUI should show [1]-[7] with no gaps:
```
[1]  Start
[2]  Packs
[3]  Workspace
[4]  Tutor
[5]  Reports
[6]  Classroom
[7]  Settings
[0]  Quit
```

## Adding New Features

### Adding a student feature
1. Add to `src/ducky/cli/student_commands.py` or `src/ducky/gui/student.py`
2. Import only from shared modules
3. Run `bash build_student.sh` and verify

### Adding a teacher feature
1. Add to `src/ducky/cli/teacher_commands.py` or `src/ducky/gui/teacher.py`
2. If it needs a new module, add to the teacher-only list above
3. If it needs new config args, add to `cli/main.py` config_set section
4. Update `build_student.sh` if the new module needs removal

### Adding a shared feature
1. Add to the appropriate shared module
2. **Do NOT import teacher-only modules at the top level**
3. Use lazy imports inside functions for teacher-only features
4. Run `bash build_student.sh` and verify

### If you add a new teacher-only module
1. Add `rm -f "$TMPDIR/ducky/new_module.py"` to `build_student.sh` step 2
2. Add the module name to the verification script above
3. Update this document's teacher-only list

## Common Mistakes

1. **Adding `from ..server import DuckyServer` at the top of `gui/app.py`** — this breaks the student build. The build script removes it, but it's cleaner to use lazy imports.

2. **Importing `teacher_commands` from `cli/main.py`** — the build script removes the import, but top-level imports break if the module is deleted. Use lazy imports.

3. **Adding teacher config args without updating `build_student.sh`** — new `config_set.add_argument("--teacher-*")` lines need to be added to the skip list in step 5.

4. **Renumbering GUI menu items without checking student build** — the build script patches [7]→remove and [8]→[7]. If you add/reorder menu items, update the patching.

5. **Forgetting to rebuild** — always run `bash build_student.sh` after changes and verify the output.
