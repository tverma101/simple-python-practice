# Ducky Architecture

## Overview

Ducky is a local-first Python coding tutor designed for classroom use. It operates on a teacher-student model where:

1. **Teachers** create lesson packs, serve them to students, and collect submissions
2. **Students** download lessons, write code, run tests, and get hints
3. **Communication** happens over a local network (no internet required for classroom use)

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Teacher Machine                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  CLI/TUI     │  │  HTTP Server │  │  SQLite Database      │  │
│  │  (teacher    │  │  (port 8420) │  │  (classroom.db)       │  │
│  │   commands)  │  │              │  │  - classes            │  │
│  └──────┬───────┘  └──────┬───────┘  │  - students           │  │
│         │                 │          │  - packs               │  │
│         │                 │          │  - submissions         │  │
│         │                 │          │  - announcements       │  │
│         │                 │          └──────────────────────┘  │
│         │                 │                                     │
│  ┌──────┴─────────────────┴──────────────────────────────────┐ │
│  │                    Teacher Workspace                       │ │
│  │  ~/.ducky/teacher/workspace/lessons/                      │ │
│  │  ~/.ducky/teacher/workspace/built_packs/                  │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP API (local network)
                              │ Port 8420, 9-digit access code
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                        Student Machine                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │  CLI/TUI     │  │  Test Runner │  │  Tutor Engine        │  │
│  │  (student    │  │  (pytest)    │  │  (hints & diagnosis) │  │
│  │   commands)  │  │              │  │                      │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────────────┘  │
│         │                 │                                     │
│  ┌──────┴─────────────────┴──────────────────────────────────┐ │
│  │                    Student Workspace                       │ │
│  │  ~/ducky-workspace/                                       │ │
│  │  ~/.ducky/packs/                                          │ │
│  │  ~/.ducky/reports/                                        │ │
│  │  ~/.ducky/progress.db                                     │ │
│  └───────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. CLI Layer (`src/ducky/cli/`)

The CLI is the primary interface for both teachers and students.

**Entry Points:**
- `ducky` — Interactive TUI (no arguments)
- `ducky student <command>` — Student operations
- `ducky teacher <command>` — Teacher operations
- `ducky check` — Quick grading (legacy)
- `ducky test` — Run tests (convenience alias)

**Key Files:**
- `main.py` — Unified dispatcher with backward compatibility
- `student_commands.py` — Student sub-CLI (10 commands)
- `teacher_commands.py` — Teacher sub-CLI (13 commands)
- `shared.py` — Shared utilities (tutor engine, config printing, progress recording)

### 2. HTTP Server (`src/ducky/server.py`)

A stdlib HTTP server that handles teacher-student communication.

**Features:**
- Threaded request handling
- Rate limiting (10 attempts per 60 seconds)
- Session tokens via 9-digit access codes
- CORS support for web clients
- LLM hint proxy (forwards hints to students)

**API Endpoints:**
- `POST /api/connect` — Student connects with access code
- `POST /api/packs` — Student fetches available packs
- `POST /api/submit` — Student submits work
- `POST /api/hint` — Student requests LLM hint
- `GET /api/llm-status` — Check LLM backend status
- `POST /api/announce` — Teacher sends announcements

### 3. Classroom Database (`src/ducky/classroom.py`)

SQLite database managing teacher-side data.

**Tables:**
- `classes` — Class definitions with LLM settings
- `students` — Connected students with session tokens
- `packs` — Available lesson packs
- `submissions` — Student work submissions
- `announcements` — Teacher announcements

**Features:**
- Thread-safe with `check_same_thread=False`
- Auto-generates 9-digit access codes
- Per-class LLM configuration

### 4. Test Runner (`src/ducky/runner.py`)

Runs pytest against student code and generates reports.

**Flow:**
1. Unpacks `.duckypack` to temp directory
2. Copies student code to starter location
3. Runs pytest with timeout (default 15s)
4. Parses JUnit XML output
5. Classifies failures with diagnostics
6. Generates `TestRunReport` with hints

**Key Classes:**
- `TestRunReport` — Complete test run results
- `TestFailure` — Individual test failure with diagnosis
- `PytestSummary` — Parsed pytest output

### 5. Tutor Engine (`src/ducky/tutor.py`)

Generates hints for failed tests using rule-based diagnostics or LLM.

**Hint Generation:**
1. Rule-based diagnostics (always available):
   - Syntax errors
   - Missing functions
   - Wrong return types
   - Print vs return issues
   - Function signature mismatches
2. LLM-powered hints (optional):
   - Full context: instructions, test cases, student code
   - Teacher-proxied hints (no student setup needed)

### 6. Pack System (`src/ducky/pack.py`)

Manages `.duckypack` files — the distribution format for lessons.

**.duckypack Format:**
```
lesson.duckypack (ZIP file)
├── manifest.json      # Pack metadata
├── starter/           # Starter code files
│   └── lesson_01.py
└── tests/             # pytest test files
    └── test_lesson_01.py
```

**Manifest Fields:**
- `lesson_id` — Unique identifier (e.g., `python_basics.lesson_01`)
- `title` — Human-readable title
- `entry_file` — Main student file
- `language` — Programming language (default: python)
- `test_command` — Test command (default: `python -m pytest tests/`)
- `concept_tags` — Learning concepts
- `diagnostic_cases` — Test cases for better hints
- `instructions` — Lesson instructions

### 7. Configuration (`src/ducky/config.py`)

Manages user configuration in `~/.ducky/config.json`.

**Config Keys:**
```json
{
  "llm_backend": "none|ollama|lmstudio|gemini",
  "ollama_model": "qwen2.5-coder:1.5b",
  "ollama_url": "http://localhost:11434",
  "lmstudio_model": "default",
  "lmstudio_url": "http://localhost:1234",
  "lmstudio_max_tokens": 2500,
  "gemini_api_key": "",
  "gemini_model": "gemini-2.5-flash",
  "teacher_url": "",
  "teacher_token": "",
  "student_name": "",
  "server_port": 8420,
  "auto_update": false,
  "auto_update_app": true,
  "r2_base_url": "https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev",
  "github_owner": "tverma101",
  "github_repo": "simple-python-practice"
}
```

### 8. Auto-Update System (`src/ducky/self_update.py`)

Keeps Ducky up-to-date automatically.

**Update Flow:**
1. Check GitHub for latest version (`/releases/latest`)
2. If GitHub unreachable, fall back to R2 (`latest.json`)
3. Download wheel/zip to temp directory
4. Apply update on next launch (before CLI starts)
5. Background thread checks for updates without blocking

### 9. Workspace Management (`src/ducky/workspace.py`)

Manages student workspaces.

**Directories:**
- `~/ducky-workspace/` — Active lesson files
- `~/.ducky/packs/` — Downloaded lesson packs
- `~/.ducky/reports/` — Test run reports
- `~/.ducky/progress.db` — Progress tracking database

**Features:**
- Auto-detects current lesson
- Tracks progress per lesson
- Extracts `.duckypack` files to workspace

### 10. Diagnostics (`src/ducky/diagnostics.py`)

Rule-based failure classification for better hints.

**Detected Issues:**
- Syntax errors
- Missing functions
- Wrong return types
- Print vs return (common beginner mistake)
- Function signature mismatches
- Import errors
- Runtime errors

### 11. Redaction (`src/ducky/redactor.py`)

Anonymizes sensitive data before export.

**Redacted Patterns:**
- Email addresses
- Phone numbers
- Absolute file paths
- API keys and tokens
- Personal comments

### 12. Background Sync (`src/ducky/background_sync.py`)

Synchronizes student data with teacher server.

**Sync Operations:**
- Pull new packs from teacher
- Submit work to teacher
- Sync progress tracking

## Data Flow

### Student Workflow

```
1. Student runs `ducky`
2. TUI opens with options: Lessons, Tests, Connect, Update, Submit
3. Student selects lesson → workspace.py extracts pack
4. Student writes code in editor
5. Student runs `ducky test` → runner.py executes pytest
6. If tests fail → tutor.py generates hints
7. Student iterates until all tests pass
8. Student submits work to teacher (optional)
```

### Teacher Workflow

```
1. Teacher runs `ducky teacher serve`
2. HTTP server starts on port 8420
3. Teacher generates 9-digit access code
4. Students connect with code
5. Teacher adds lesson packs
6. Students pull packs and work on lessons
7. Students submit work
8. Teacher reviews submissions and exports data
```

## Security Model

### Access Control
- 9-digit access codes for student connection
- Bearer token authentication
- Rate limiting (10 attempts per 60 seconds)

### Data Privacy
- No cloud sync by default
- No automatic telemetry
- Reports stay local unless manually exported
- PII redacted before export
- Teacher backend submission is opt-in

### Network Security
- Local network only (no internet required)
- CORS headers for web clients
- Thread-safe database operations

## Performance Considerations

### Test Execution
- pytest timeout: 15 seconds (configurable)
- Temp directory cleanup after each run
- JUnit XML parsing for structured output

### Database
- SQLite with WAL mode for concurrent reads
- Thread-safe connections
- Indexed queries for fast lookups

### Auto-Update
- Background thread (non-blocking)
- GitHub-first, R2-fallback strategy
- Cached version checks

## Extension Points

### Adding New LLM Backends
1. Create new client class in `llm_client.py`
2. Implement `generate_hint()` method
3. Add backend to `build_llm_client()` in `config.py`
4. Add config keys for the new backend

### Adding New Diagnostics
1. Add classification rule in `diagnostics.py`
2. Add function detection in `function_detector.py`
3. Add hint template in `tutor.py`

### Adding New CLI Commands
1. Add command to `student_commands.py` or `teacher_commands.py`
2. Register in `main.py` dispatcher
3. Add to TUI menu in `gui/app.py`
