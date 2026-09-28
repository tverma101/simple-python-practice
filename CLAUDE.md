# Ducky — Project Context for AI Assistants

## What is Ducky?

Ducky is a **local-first Python coding tutor** with a terminal UI, test runner, teacher-student connection system, and smart diagnostics. It lets teachers create coding lesson packs and students run tests against their code locally.

**Privacy-first:** No cloud sync, no telemetry, no public student data. Reports stay on your machine unless manually exported.

## Architecture

### Distribution Model

- **Cloudflare R2** is used for distribution (NOT GitHub Releases)
- Public URL: `https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev` (custom domain)
- R2 **bucket NAME** is `ducky` — the `pub-be8cbd…` host is the public custom domain, NOT the bucket name. Upload scripts use bucket `ducky`; never use the public hostname as the S3 bucket key or you get `NoSuchBucket`.
- Students download from R2 with **no auth required**
- GitHub is used for source code and auto-update version checks
- R2 is the fallback when GitHub is unreachable

### R2 Release Pipeline

Canonical, repeatable release-to-R2 flow. Filenames matter — they have drifted before.

```bash
bash setup_r2.sh                 # one-time: writes ~/.aws/credentials [r2] + config [profile r2]
bash build_student.sh            # builds → downloads/ducky_student-<v>-py3-none-any.whl + ducky_student-v<v>.zip
python3 upload_to_r2.py --dry-run  # preview: uploads downloads/* + dist/latest.json + student-install.sh
python3 upload_to_r2.py --check    # non-destructive signed LIST — verifies the token works
python3 upload_to_r2.py            # real upload to bucket `ducky`
```

**Filename invariants (do not regress):**
- Build artifacts use the **`ducky_student-`** prefix (underscore), set by `DIST_NAME` in `build_student.sh`. The hyphen spelling (`ducky-student-`) returns 404.
- `dist/latest.json` `download_url` must point to `ducky_student-v<v>.zip` (the underscore zip). This is what `self_update.py` downloads for auto-update — a stale `ducky-v<v>.zip` value breaks auto-update silently.
- `student-install.sh` (repo root) fetches `ducky_student-v<v>.zip` and must be uploaded to the bucket root so `curl … | bash` works.

**Credential locations (read by upload_to_r2.py):**
- `~/.aws/credentials` `[r2]` → `aws_access_key_id`, `aws_secret_access_key`
- `~/.aws/config` `[profile r2]` → `s3.endpoint_url = https://<account-id>.r2.cloudflarestorage.com` (also `R2_ACCOUNT_ID` env var works)
- Override bucket/doman via `R2_BUCKET` / `R2_PUBLIC_BASE` env vars if needed.

- This is the **teacher repository** — it contains the full Ducky tool (student + teacher features)
- Do NOT create student-facing files (badges, README instructions) in this repo unless explicitly asked
- Students install via: `curl -sSL https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev/student-install.sh | bash`
- Or from source: `pip install git+https://github.com/tverma101/simple-python-practice.git && ducky init`

### Student/Teacher Separation

**CRITICAL: The student build strips teacher modules. Follow these rules.**

Teacher-only modules (deleted from student build):
- `server.py`, `classroom.py`, `teacher_workspace.py`, `teacher_backend.py`
- `importer.py`, `validator.py`, `lesson_builder.py`
- `cli/teacher_commands.py`, `gui/teacher.py`

Rules:
- **NEVER** import teacher-only modules at the top level of shared code
- Use lazy imports inside functions for teacher features in shared code
- Config keys (`teacher_url`, `teacher_token`) are safe — students need them to connect
- `student_client.py` is shared — it's the student↔teacher HTTP client
- After changes, always run `bash build_student.sh` and verify the output
- See `STUDENT_BUILD.md` for the full separation guide

### Key Directories

```
src/ducky/           # Main source code
  cli/               # CLI dispatcher (main.py, student_commands.py, teacher_commands.py)
  gui/               # Terminal TUI (app.py, student.py, teacher.py, settings.py, widgets.py)
  classroom.py       # SQLite database for teacher (classes, students, packs, submissions)
  server.py          # stdlib HTTP server for teacher-student connection
  student_client.py  # Client library for student-teacher communication
  runner.py          # Test runner with pytest integration
  pack.py            # .duckypack format (ZIP with manifest.json)
  config.py          # Configuration management
  llm_client.py      # LLM backend abstraction (Ollama, LM Studio, Gemini)
  tutor.py           # TutorEngine with hint generation
  diagnostics.py     # Rule-based failure classification
  function_detector.py  # Function signature mismatch detection
  redactor.py        # PII redaction before export
  reports.py         # Report storage and rendering
  progress.py        # Progress tracking (SQLite)
  importer.py        # Import .duckypack or .zip assignments
  self_update.py     # Auto-update via GitHub + R2 fallback
  workspace.py       # Workspace management
  teacher_workspace.py  # Teacher workspace (lessons, packs)
  background_sync.py # Background sync with teacher
  init_workspace.py  # Initialize new workspace
  paths.py           # Application directory paths (~/.ducky/)
  display.py         # Display utilities
  editor.py          # VS Code detection and opening
  dataset_builder.py # Dataset JSONL export
  reports_dataset.py # Report dataset generation
  workflows.py       # Workflow management
tests/               # pytest test suite (715+ tests)
assignments/         # Example lesson folders
dist/                # Static release metadata (latest.json, install.sh). NOT the build output dir.
downloads/           # build_student.sh output: ducky_student-<v>-py3-none-any.whl, ducky_student-v<v>.zip
upload_to_r2.py      # Canonical R2 uploader (pure stdlib SigV4). --dry-run, --check
setup_r2.sh          # One-time R2 credential setup → ~/.aws/{credentials,config}
examples/            # Example packs and student submissions
```

### Technology Stack

- **Language:** Python 3.10+
- **Dependencies:** Only `pytest>=7.0` at runtime (stdlib everything else)
- **Database:** SQLite (via Python stdlib `sqlite3`)
- **HTTP Server:** Python stdlib `http.server` with threading
- **Build:** setuptools with `pyproject.toml`
- **Linting:** ruff
- **Testing:** pytest
- **CI:** GitHub Actions (Python 3.10, 3.11 on ubuntu-latest)

## Testing Workflow

- Always run the full test suite after making changes: `python -m pytest`
- Target: all tests passing (715+ tests)
- Tests must NOT produce side effects (e.g., opening Finder windows, file browsers)
- All OS-level actions must be mocked in tests
- When modifying test infrastructure, verify no regressions in existing tests
- After any module changes, rebuild student distribution: `bash build_student.sh`
- Verify student zip has no teacher modules: check `STUDENT_BUILD.md` for verification steps

## Infrastructure Changes

When building or modifying infrastructure (R2 distribution, build scripts, upload tooling, CI, server endpoints, networking), **write or update documentation in the same change** — do not leave infra as undocumented code. Specifically:
- Add a short "how to set up / run this" section to `README.md` (e.g. the R2 upload block under Distribution).
- If it changes command names or arguments, update the matching `## CLI Commands` entries here.
- If it changes assumptions teachers/students must know, update the relevant `STUDENT_*.md` / `TEACHER_*.md` guide.
- Record one-time setup steps (tokens, env vars, config files) so they're repeatable — see `setup_r2.sh` + `upload_to_r2.py` for the R2 example.
Infra without docs is incomplete; treat the docs as part of the deliverable, not a follow-up.

## Common Pitfalls

1. **Finder windows opening during tests:** If tests open Finder/file browser, check for missing mocks on `open_in_finder` or similar OS-level actions
2. **Lesson file handling:** `my_lesson` folder behavior — check whether code is literally opening files in Finder vs just referencing them as fixtures
3. **Distribution assumptions:** Always use Cloudflare R2 for distribution, NOT GitHub Releases
4. **Teacher repo vs student repo:** This repo contains both teacher and student features. Do not assume separate repos.

## CLI Commands

### Student Commands
```bash
ducky                          # Open interactive TUI
ducky test                     # Run tests on current lesson
ducky test --lesson <id>       # Run tests on a specific lesson
ducky hint                     # Show hint for last failure
ducky instructions             # View lesson instructions
ducky quiz [--lesson <id>] [--status]   # Take the lesson quiz / show stats
ducky open                     # Open lesson in editor
ducky init [dir]               # Initialize workspace
ducky import <file>            # Import .duckypack or .zip assignment
ducky setup-llm                # Configure LLM hints (optional)
ducky student connect --code <code> --name <name> --url <teacher_url>
ducky student update           # Pull new lessons from teacher
ducky student submit           # Submit work to teacher
ducky student quiz [--status]  # Take the lesson quiz (alias of `ducky quiz`)
```

### Teacher Commands
```bash
ducky teacher serve            # Start teacher server (default port 8420)
ducky teacher add-pack         # Add lesson pack to server
ducky teacher students         # List connected students
ducky teacher reports          # View student submissions
ducky teacher export           # Export class data as JSONL
ducky teacher announce         # Send announcement
ducky teacher scan --source <dir>  # Scan folder and build packs
ducky teacher pack create      # Create .duckypack file
ducky teacher config show/set  # Configuration management
ducky teacher dataset export   # Build and export dataset JSONL
ducky teacher redact           # Redact sensitive info
ducky teacher quiz create --lesson <dir> --file quizzes.json  # Author & save a quiz
ducky teacher quiz validate --file quizzes.json             # Validate a quiz
```

## LLM Integration

Optional LLM backends for smarter hints:
- **Gemini:** Cloud API (free tier available at https://aistudio.google.com/apikey)
- **OpenAI:** Cloud API (gpt-4o-mini recommended, get a key at https://platform.openai.com/api-keys)
- **Ollama:** Local inference via `http://localhost:11434`
- **LM Studio:** Local inference via `http://localhost:1234`

When connected to a teacher server, students automatically get AI hints through the teacher's LLM — no setup needed on the student side.

## Auto-Update

- Checks GitHub for new versions on every launch
- Falls back to Cloudflare R2 if GitHub is unreachable
- Enabled by default (`auto_update_app: true`)
- Background thread checks for updates without blocking CLI

## .duckypack Format

A `.duckypack` is a ZIP file containing:
```
manifest.json      # Pack metadata (lesson_id, title, entry_file, concept_tags, etc.)
starter/           # Starter code files
tests/             # pytest test files
quizzes.json       # Optional: teacher-authored quiz questions
```

## Quiz Questions

Teachers can bundle a quiz with a lesson as `quizzes.json` inside the
`.duckypack` (alongside `manifest.json`). Quizzes are **teacher-authored only**
(no LLM generation) and graded **offline / deterministically** — no cloud, no
telemetry — so they preserve Ducky's local-first privacy promise.

Core module: `src/ducky/quiz.py`. Question types (all normalized exact-match):

- `multiple_choice` — `prompt`, `options` (≥2), `correct` (exact option text).
- `predict_output` — `code` (snippet), `correct` (expected printed output; `""` allowed for "prints nothing").
- `true_false` — `correct` is `true`/`false`. The friendly alias `mcq` for
  `multiple_choice` is accepted and normalized on load.

JSON shape (`quizzes.json`):
```json
{
  "quiz_id": "quiz.<lesson_id>",
  "lesson_id": "<lesson_id>",
  "title": "Lesson Quiz",
  "description": "optional",
  "questions": [
    {"id": "q1", "type": "multiple_choice", "prompt": "...", "options": ["a","b"], "correct": "a"},
    {"id": "q2", "type": "true_false", "prompt": "...", "correct": "false"},
    {"id": "q3", "type": "predict_output", "prompt": "...", "code": "print(1)", "correct": "1"}
  ]
}
```
Distribution reuses the existing pack pipeline (`create_duckypack(quizzes_path=...)`),
the pack validator (`validate_quiz_dict` runs inside `validate_duckypack`), and
both student surfaces (CLI + TUI). `QuizResultsStore` (SQLite at
`~/.ducky/quiz_results.db`) persists attempts locally. `quizzes.json` is
**student-facing** and must NOT be stripped from the student build (it is not
in the teacher-only module list in `STUDENT_BUILD.md`).

## Version

Current version: `0.5.2` (defined in `src/ducky/__init__.py`)
