# Development Guide

## Overview

This guide covers how to develop, test, and contribute to Ducky.

## Prerequisites

- Python 3.10+
- Git
- pip

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/tverma101/simple-python-practice.git
cd simple-python-practice
```

### 2. Create Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -e ".[dev]"
```

This installs:
- `pytest>=7.0` (runtime dependency)
- `ruff>=0.4.0` (dev dependency)

### 4. Verify Installation

```bash
python -m pytest
python run_ducky.py
```

---

## Project Structure

```
simple-python-practice/
├── src/ducky/                    # Main source code
│   ├── __init__.py               # Version (0.5.0)
│   ├── cli/                      # CLI layer
│   │   ├── __init__.py
│   │   ├── main.py               # Unified dispatcher
│   │   ├── student_commands.py   # Student sub-CLI
│   │   ├── teacher_commands.py   # Teacher sub-CLI
│   │   └── shared.py             # Shared utilities
│   ├── gui/                      # Terminal TUI
│   │   ├── __init__.py
│   │   ├── app.py                # Main TUI app
│   │   ├── student.py            # Student TUI
│   │   ├── teacher.py            # Teacher TUI
│   │   ├── settings.py           # Settings TUI
│   │   └── widgets.py            # TUI widgets
│   ├── classroom.py              # SQLite teacher database
│   ├── server.py                 # HTTP server
│   ├── student_client.py         # Student-teacher client
│   ├── runner.py                 # Test runner
│   ├── pack.py                   # .duckypack format
│   ├── config.py                 # Configuration
│   ├── llm_client.py             # LLM backends
│   ├── tutor.py                  # Tutor engine
│   ├── diagnostics.py            # Failure classification
│   ├── function_detector.py      # Function detection
│   ├── redactor.py               # PII redaction
│   ├── reports.py                # Report storage
│   ├── progress.py               # Progress tracking
│   ├── importer.py               # Import assignments
│   ├── self_update.py            # Auto-update
│   ├── workspace.py              # Workspace management
│   ├── teacher_workspace.py      # Teacher workspace
│   ├── background_sync.py        # Background sync
│   ├── init_workspace.py         # Initialize workspace
│   ├── paths.py                  # Path management
│   ├── display.py                # Display utilities
│   ├── editor.py                 # VS Code detection
│   ├── dataset_builder.py        # Dataset export
│   ├── reports_dataset.py        # Report dataset
│   └── workflows.py              # Workflow management
├── tests/                        # Test suite
│   ├── __init__.py
│   ├── helpers.py                # Test helpers
│   └── test_*.py                 # Test files
├── assignments/                  # Example lessons
├── dist/                         # Distribution files
├── examples/                     # Example packs
├── docs/                         # Documentation
├── CLAUDE.md                     # AI assistant context
├── pyproject.toml                # Build configuration
├── README.md                     # Main readme
├── CHANGELOG.md                  # Version history
├── PRIVACY.md                    # Privacy policy
├── STUDENT_INSTALL.md            # Student install guide
├── TEACHER_PACK_BUILDER.md       # Teacher pack guide
└── install.sh                    # Install script
```

---

## Running Tests

### Browser checks (not covered by pytest)

```bash
python tools/check_editor.py
```

Drives two pages in headless Chrome and asserts on the rendered DOM. Two of these
checks exist because the failure they guard is invisible to every other kind of
test:

- **CodeMirror mounted and parsed.** A bare specifier missing from the import map
  produces a dead editor with *no* console error and no exception. The page looks
  correct in a screenshot.
- **`static/js/editor.js` fires `onChange`.** This is the check that catches dead
  autosave. The editor mounts and looks perfect while `onChange` throws, because
  the failure is in the handler rather than in rendering. It was found by
  `onChange` calling `view.updateListener.of(...)`: `updateListener` is a static
  Facet on the *constructor*, so `view.updateListener` was `undefined`, the
  TypeError escaped `initEditor`'s try/catch, and autosave silently never ran.

To confirm a new check can actually fail, break the thing on purpose and re-run
it. A browser check that passes against the broken version is not a check.

### Basic Test Run

```bash
python -m pytest
```

### With Verbose Output

```bash
python -m pytest -v
```

### With Coverage

```bash
python -m pytest --cov=ducky
```

### Run Specific Test

```bash
python -m pytest tests/test_runner.py
```

### Run Specific Test Function

```bash
python -m pytest tests/test_runner.py::test_run_pack
```

### Test Expectations

- All tests should pass (267+ tests)
- Tests must NOT produce side effects (no Finder windows, no file browsers)
- All OS-level actions must be mocked
- No regressions in existing tests

---

## Code Style

### Linting with Ruff

```bash
# Check for issues
ruff check src/ducky/

# Fix auto-fixable issues
ruff check --fix src/ducky/

# Format code
ruff format src/ducky/
```

### Configuration

Ruff is configured in `pyproject.toml`:
```toml
[tool.ruff]
line-length = 120
```

### Style Guidelines

- Use type hints
- Follow PEP 8
- Keep functions focused
- Add docstrings for public functions
- Use descriptive variable names

---

## Building

### Build Wheel

```bash
python -m build
```

Creates:
- `dist/ducky-0.5.0-py3-none-any.whl`
- `dist/ducky-v0.5.0.zip`

### Install Locally

```bash
pip install dist/ducky-0.5.0-py3-none-any.whl
```

---

## Distribution

### Cloudflare R2

Upload to R2 for distribution:

```bash
cd dist/
python upload_to_r2.py
```

Requires:
- `~/.aws/credentials` with `[r2]` profile
- R2 account credentials

### GitHub Releases

Create a GitHub release for version tracking:

```bash
git tag v0.5.0
git push origin v0.5.0
```

Then create a release on GitHub with the wheel and zip files.

---

## Development Workflow

### 1. Create Feature Branch

```bash
git checkout -b feature/my-feature
```

### 2. Make Changes

- Write code
- Add tests
- Update documentation

### 3. Run Tests

```bash
python -m pytest
```

### 4. Lint Code

```bash
ruff check src/ducky/
ruff format src/ducky/
```

### 5. Commit Changes

```bash
git add .
git commit -m "Add my feature"
```

### 6. Push and Create PR

```bash
git push origin feature/my-feature
```

---

## Common Development Tasks

### Adding a New CLI Command

1. Add command to `student_commands.py` or `teacher_commands.py`
2. Register in `main.py` dispatcher
3. Add to TUI menu in `gui/app.py`
4. Add tests in `tests/test_cli_*.py`

### Adding a New LLM Backend

1. Create client class in `llm_client.py`
2. Implement `generate_hint()` method
3. Add backend to `build_llm_client()` in `config.py`
4. Add config keys for the new backend
5. Add tests

### Adding New Diagnostics

1. Add classification rule in `diagnostics.py`
2. Add function detection in `function_detector.py`
3. Add hint template in `tutor.py`
4. Add tests

### Modifying the Pack Format

1. Update `DuckyManifest` dataclass in `pack.py`
2. Update `create_duckypack()` function
3. Update `unpack_duckypack()` function
4. Update `validator.py` if needed
5. Update tests

---

## Testing Best Practices

### Mocking

Always mock OS-level operations:

```python
from unittest.mock import patch, MagicMock

@patch('ducky.editor.open_in_finder')
def test_open_lesson(mock_open):
    open_lesson('lesson_01')
    mock_open.assert_called_once()
```

### Test Helpers

Use helpers in `tests/helpers.py`:

```python
from helpers import make_add_pack

def test_run_pack(tmp_path):
    pack_path = make_add_pack(tmp_path)
    result = run_pack(pack_path, student_file='solution.py')
    assert result.passed
```

### Test Coverage

Aim for high coverage:
- Core logic: 90%+
- CLI commands: 80%+
- GUI: 70%+

### Avoiding Side Effects

- Never open files in Finder during tests
- Never modify system files
- Use temp directories for all file operations
- Clean up after tests

---

## Debugging

### Running Ducky in Debug Mode

```bash
# Verbose test output
python -m pytest -v -s

# Debug specific test
python -m pytest tests/test_runner.py -v -s --tb=long
```

### Common Issues

1. **Import errors** — Check `sys.path` and package structure
2. **SQLite errors** — Check database connections and threading
3. **Test failures** — Check mocks and temp directories
4. **Lint errors** — Run `ruff check` and fix issues

### Logging

Add logging for debugging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
logger.debug("Debug message")
```

---

## CI/CD

### GitHub Actions

CI runs on every push and PR:

```yaml
name: tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.11"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: pytest
```

### Pre-commit Hooks

Optional pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
```

---

## Release Process

### 1. Update Version

Update version in `src/ducky/__init__.py`:

```python
__version__ = "0.6.0"
```

### 2. Update Changelog

Add entry to `CHANGELOG.md`:

```markdown
## 0.6.0 (YYYY-MM-DD)

### New Features
- Feature 1
- Feature 2

### Improvements
- Improvement 1

### Bug Fixes
- Fix 1
```

### 3. Build Distribution

```bash
python -m build
```

### 4. Upload to R2

```bash
cd dist/
python upload_to_r2.py
```

### 5. Create GitHub Release

```bash
git tag v0.6.0
git push origin v0.6.0
```

Create release on GitHub with wheel and zip files.

### 6. Update `latest.json`

Update `dist/latest.json` with new version info.

---

## Getting Help

### Documentation

- `docs/ARCHITECTURE.md` — System architecture
- `docs/TEACHER_GUIDE.md` — Teacher workflows
- `docs/STUDENT_GUIDE.md` — Student workflows
- `CLAUDE.md` — AI assistant context

### Issues

Report issues on GitHub: https://github.com/tverma101/simple-python-practice/issues

### Code Review

All changes require code review before merging.
