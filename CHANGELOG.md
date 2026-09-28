# Changelog

## 0.5.2 (2026-09-27)

Simple Python Practice Platform: a layered grading sandbox, a verified problem
library, and a LAN practice server. This is the only 0.5.x release with a build
in `dist/latest.json`; 0.5.0 and 0.5.1 predate this repository's history and are
not recorded here rather than guessed at.

### New Features
- **LAN Practice Server** (`ducky web`): stdlib `http.server`, no framework, no build step, no remote assets, so the R2 student build keeps its zero-dependency property. Browsing is open, running code requires a per-student access code, grading is rate limited, the `Host` header is checked before routing, and responses carry `nosniff`, `DENY` and `no-referrer`
- **Per-Student Access Codes** (`ducky access-codes`): individual codes on the existing classroom tables instead of one shared class code, so "who ran this, and how much CPU did they burn" is answerable
- **Problem Surface**: filtering and paging in SQL with the total from `COUNT(*)`, keyboard-complete tabs, results in a live region, pass and fail distinguished by icon, word and rule rather than colour alone, and a save state that is reported honestly with a `beforeunload` guard
- **Vendored CodeMirror 6** (1.17 MB, no build step, no web worker) replacing Monaco from a CDN, with `tools/check_editor.py` driving the editor rather than only the vendored files
- **Content Verification** (`tools/verify_content.py`): every reference solution is executed against its own test cases inside the grading sandbox. 5,132 problems imported, **2,603 verified shippable**, 2,529 quarantined with a reason each
- **Content Repair** (`tools/repair_content.py`): rewrites an expectation only when every expectation that is already a plausible answer still matches, and declines rather than guessing. New `NAME_MISMATCH` defect class
- **Generated Explanations** (`src/ducky/explain.py`): each problem statement composed from that problem's own signature, worked examples and detectable edge cases, replacing one template shared byte-identically by 1,149 problems. Where the tests demonstrate no edge case, `explain()` says so in `missing` rather than inventing one
- **Persistence** (`src/ducky/store.py`): run attribution and drafts actually persist

### Security
- **Arbitrary file read closed**: the grader returns the submission's own value to the browser, so any file-reading builtin is an exfiltration primitive — `open("/etc/passwd").read()` returned the file's contents in `actual_repr`. Now blocked: `open`, `file`, `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `__builtins__`, `__loader__`, `__spec__`
- **The dunder allowlist made to mean something**: the AST check only sees *literal* attribute names, so `getattr(x, "__cla" + "ss__")` reached `__class__` and from there the `__subclasses__` ladder, while `x.__class__` was rejected. Blocking `getattr` is what makes the allowlist hold
- **Answer key exfiltration closed two ways**: a 256 KB print truncated the result sentinel and the diagnostic then quoted the trailing output, hidden expectations included; and `open('_ducky_verify.py').read()` read the driver's source off disk. The child now unlinks its own source and `chdir`s to an empty directory
- **Hidden expectations moved to their own table**, so reading a student-facing problem is a query that structurally cannot return them, and every failure payload goes through `CaseResult.student_view()`
- **Layer 1 now applies to reference solutions**: `verify_problem` runs the same `check_student_code` filter the grader does, so a solution that calls `eval` is rejected before it runs. Three `eval` problems stopped shipping, which is correct — a grader that will not run `eval` for a student must not have its answer supplied by the reference implementation
- **"Run" no longer grades**: Run and Submit issued the same request, so pressing Run ran the hidden cases and wrote the run log. Run is now samples-only, records nothing, and says so
- **`Content-Length: -1` no longer reads to EOF**: a negative length slipped past `length > MAX_BODY_BYTES`, and `rfile.read(-1)` read until the client closed
- **Concurrency bounded**: a socket timeout and a quiet `handle_one_request` so a silent client cannot pin a thread, and a semaphore sized near the core count so one valid code cannot hold every core. The rate limiter is re-keyed from client IP to the resolved student, because every student on a classroom LAN shares one NAT address
- **Layering documented, gaps included**: `docs/SECURITY_MODEL.md` states what is *not* protected — no network isolation, no uid drop, no seccomp or Landlock, and on macOS memory is bounded only by the wallclock because `RLIMIT_AS` has a ceiling too low for CPython to start. `enforcement_summary()` reports a refused limit as refused

### Improvements
- **Nine defects found by running the site, most of them silent**: static assets 404'd (`StaticFiles` mounted at `/static`, then `resolve()` stripped a second `static/` prefix, so the whole UI was unstyled and every button inert); autosave never ran (a `Facet` read off the constructor instead of the extension list); drafts were keyed two different ways, so saved work could never come back; 2,400 of 2,603 problems were unreachable (`limit=500` default, discarded by a slice to 200); internal vocabulary leaked into student-facing messages; a 401 told a student to navigate away from unsaved work
- **Template renderer rewritten**: parse into a tree, then render recursively. The regex version evaluated `{% if %}` before the enclosing `{% for %}` bound its variable, could not match a `for` inside an `if` in either order, re-matched a value containing `{{{ x }}}`, passed unknown tags through verbatim, and had never implemented `{% else %}`, filters or `!=` — so every if/else in the shipped templates was rendering as literal text into students' pages
- **`TestCase.usable` now means "parses"**: it also required non-empty `args`/`kwargs`, which made every zero-argument function untestable and silently excluded 920 of 30,511 cases. Verified shippable goes 2,547 → 2,603
- **Editor check serves production URLs**: it served the vendor tree at the site root while the import map uses absolute `/static/...` specifiers, and a relative map fails silently against `/p/{slug}`
- **Three sandbox bugs the CI matrix caught** (Ubuntu, Python 3.10–3.13, all passing on macOS): a `while True: pass` reported as `runtime_error` instead of `timeout`; CPU-limit kills attributed to signal 9 because Linux escalates straight to `SIGKILL`; two tests asserting macOS behaviour unconditionally

### Documentation
- `docs/SECURITY_MODEL.md` — each layer, what it stops, where it lives, and what is not protected
- `docs/CONTENT_VERIFICATION.md` — why 2,603 verified / 2,529 quarantined, and what is wrong with each quarantined group
- `docs/UX_DECISIONS.md`, `docs/CONTENT_BUILD.md`, `SECURITY.md` (private vulnerability reporting), `CONTRIBUTING.md`

### Architecture
- `sandbox.py`: four independent layers — static AST allowlist, POSIX rlimits via a generated bootstrap (not `preexec_fn`, which is unsafe alongside threads), wallclock plus process-group `SIGKILL` with a hard output cap, and a from-scratch environment scrub. Fixes real bugs found while building it: student code inherited the entire parent environment, so `GEMINI_API_KEY`, `OPENAI_API_KEY` and `~/.ducky/config.json` were readable inside a graded run; `communicate()` buffered student output without bound; a returncode of `-9` is truthy, so the output-limit verdict was lost
- `content.py` / `verify.py` / `contentstore.py`: the upstream schema is normalised in one module, verification runs in the grading sandbox, and the store is read-only to the server with `solutions` refused outright
- `webapp.py` / `routes.py` / `webtemplate.py`: the LAN server, its routes, and the recursive template renderer
- `explain.py` / `derive_examples.py`: explanations and worked examples derived from each problem's own test data; hidden cases contribute their inputs to edge detection but never their expected value
- `store.py`: SQLite persistence for runs, drafts and attribution
- `cli/main.py`: `ducky web` and `ducky access-codes`, both excluded from the student build

## 0.4.0 (2026-07-02)

LLM backend support, teacher workspace overhaul, student-friendly experience, and CLI rebuild.

### New Features
- **CLI Rebuild**: two clean editions — `ducky student` (10 commands) and `ducky teacher` (13 commands)
- **Full-Context LLM Hints**: hints include instructions, test cases, answer key, and student code — model nudges without solving
- **LM Studio Backend**: local LM Studio integration via OpenAI-compatible API (`/v1/chat/completions`)
- **LM Studio Model Browser**: browse, load, and select models from LM Studio with context length config
- **Teacher Workspace**: `~/.ducky/teacher/workspace/lessons/` for managing lesson folders
- **Blank Assignment Creator** (`ducky teacher gui` → Workspace → [n]): auto-generates starter/solution.py, tests/test_solution.py, INSTRUCTIONS.md
- **Lesson Validation**: `get_lesson_issues()` checks starter, tests, instructions, entry_file
- **Repo Sync** (`ducky teacher gui` → Workspace → [s]): syncs from repo `assignments/` folder
- **Server Hint Proxy**: teacher server forwards LLM hints to students (`POST /api/hint`) with configurable `lmstudio_max_tokens`
- **LLM Status API**: `GET /api/llm-status` shows backend, model, status
- **Per-Class LLM Settings**: each class has its own LLM backend, model, URL
- **Grade Calculator Assignment**: complete blank assignment with INSTRUCTIONS.md, tests, and solution reference
- **Repo Assignments Folder**: `assignments/` at repo root as lesson source of truth

### Improvements
- `ducky` with no args opens interactive TUI (not help)
- `ducky student lessons/start/test/connect/update/submit/status` — clean sub-CLI
- `ducky teacher serve/classes/add-pack/students/reports/export/announce/scan/pack/config/dataset/redact` — full teacher CLI
- `ducky check` and `ducky student --pack ...` backward compatible
- `detect_lesson()` returns `is_valid` as proper boolean (fixes `and` returning string)
- `DuckyManifest.instructions` field for pack instructions
- `HintRequest` with `to_dict()`/`from_dict()` for JSON serialization
- Config exports `build_llm_client()` factory and `get_student_config()`
- LM Studio timeout 120s (large models need ~20s to respond)
- `scan` and `dataset` and `redact` are teacher-only
- Teacher GUI menu redesigned: Dashboard, Students, Workspace, Server Packs, Submissions, Announcements, LLM Settings, Server Control
- Workspace GUI: [n] new blank, [b] build, [a] add to server, [s] sync, [d] delete, [o] open folder

### Architecture
- `cli.py`: unified `main()` with backward compatibility, `main_student()` and `main_teacher()` sub-CLIs
- `teacher_workspace.py`: workspace management, `create_blank_assignment()`, `get_lesson_issues()`, `validate_lesson()`, `sync_from_repo()`
- `llm_client.py`: LLM backend abstraction with OpenAI-compatible client, `LMStudioClient` with `list_models()`, `load_model()`
- `server.py`: hint proxy with `llm_config` passthrough, `lmstudio_max_tokens` support
- `classroom.py`: LLM columns on classes table (`llm_enabled`, `llm_backend`, `llm_model`, `llm_url`)
- `runner.py`: `_read_test_files()` for full-context hints, `_attach_llm_hints()` passes instructions/test_cases/student_code
- `tutor.py`: `TutorEngine` with `remote_client` for teacher-proxied LLM hints
- Repo `assignments/` folder with `lesson_01_return_values`, `lesson_02_conditionals`, `lesson_grade_calculator`

## 0.3.0 (2026-07-02)

Student-friendly experience with VS Code auto-open, auto-detected lessons, and simplified workflows.

### New Features
- **VS Code Auto-Open**: detects VS Code, opens lesson file automatically
- **Lesson Discovery**: auto-scan `~/.ducky/packs/` for available lessons
- **Workspace Management**: extract lessons to `~/ducky-workspace/`, track progress via `.ducky-meta.json`
- **Student UI**: simplified terminal TUI with lessons, tests, connect, update, submit
- **CLI Commands**: `ducky lessons`, `ducky start <n>`, `ducky test`, `ducky scan --source`
- **Starter File Flattening**: copies starter files to lesson root (no `starter/` subfolder in workspace)

### Improvements
- `student-setup.sh`: curl-pipe installer with R2 URL as default
- `install.sh`: creates venv and shim at `~/.local/bin/ducky`
- Lesson auto-discovery on every CLI invocation
- Progress tracking with SQLite and `.ducky-meta.json`

### Architecture
- `editor.py`: VS Code detection + setup instructions
- `lesson_builder.py`: auto-detect lesson folders, build .duckypacks
- `workspace.py`: extract lessons, track progress, manage workspace
- `student_ui.py`: simplified student TUI

## 0.2.0 (2026-07-02)

Major expansion: teacher-student live connection system.

### New Features
- **Teacher Server** (`ducky teacher serve`): local HTTP server with 9-digit access code pairing
- **Student Connection** (`ducky connect`): students connect to teacher with 9-digit code + name
- **Pack Distribution** (`ducky update`): students pull new packs/assignments from teacher
- **Work Submission** (`ducky submit`): students submit reports and code to teacher
- **Multi-class Support**: one teacher server manages multiple classes
- **Classroom TUI**: new Classroom tab for students, Teacher tab for teachers
- **CLI Commands**: `teacher serve`, `teacher classes`, `teacher add-pack`, `teacher add-assignment`, `teacher students`, `teacher reports`, `teacher export`, `teacher announce`, `connect`, `update`, `submit`, `status`
- **Cloudflare R2 Script**: `upload_to_r2.sh` for frictionless ZIP distribution

### Architecture
- `classroom.py`: SQLite database for teacher (classes, students, packs, assignments, submissions, announcements)
- `server.py`: stdlib HTTP server with threaded request handling, rate limiting, session tokens
- `student_client.py`: client library for student-teacher communication
- Thread-safe SQLite with `check_same_thread=False` and threading locks
- Zero new runtime dependencies (stdlib `http.server`, `sqlite3`, `urllib`)

### Improvements
- Terminal TUI expanded with Classroom and Teacher tabs
- Settings menu shows teacher connection status
- Config includes new keys: `teacher_url`, `server_port`, `auto_update`, `update_interval_seconds`
- Paths expanded with `server_packs`, `teacher`, `student_packs` directories

## 0.1.0 (2026-07-01)

Initial release:

- GUI app with Packs, Workspace, Tutor, and Reports tabs
- CLI commands: `ducky`, `ducky-gui`, `ducky check`, `ducky pack`, `ducky dataset`
- `.duckypack` creation and inspection
- Python test runner with timeout
- Beginner error diagnostics
- Optional Ollama LLM hints
- Local report storage
- Dataset JSONL export
- Redaction of emails, paths, secrets, and comments
- Progress tracking
- Install script for students
