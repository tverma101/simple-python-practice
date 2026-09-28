# Teacher Guide

## Overview

This guide covers everything teachers need to know to set up and use Ducky in their classroom.

## Quick Start

### 1. Install Ducky

```bash
pip install git+https://github.com/tverma101/simple-python-practice.git
```

Or use the one-liner:
```bash
curl -sSL https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev/student-install.sh | bash
```

### 2. Initialize Your Workspace

```bash
ducky init
```

This creates:
- `assignments/` folder for your lessons
- `ducky.toml` configuration file

### 3. Start the Teacher Server

```bash
ducky teacher serve --port 8420 --class-name "Period 1"
```

The server will display a **9-digit access code** for students to connect.

### 4. Create a Lesson Pack

```bash
ducky teacher pack create \
  --source lesson_folder/ \
  --output lesson.duckypack \
  --lesson-id python_basics.lesson_01 \
  --title "Return Values" \
  --entry-file lesson_01.py \
  --tags "functions,return_vs_print"
```

### 5. Add Pack to Server

```bash
ducky teacher add-pack --source lesson.duckypack --lesson-id lesson_01 --title "Return Values"
```

### 6. Students Connect and Learn

Students run:
```bash
ducky student connect --code 123456789 --name "Ada" --url http://TEACHER_IP:8420
ducky student update  # Pull new lessons
```

---

## Creating Lessons

### Lesson Folder Structure

```
my_lesson/
├── starter/
│   └── lesson_01.py       # Student starter code
├── tests/
│   └── test_lesson_01.py  # pytest test cases
└── INSTRUCTIONS.md         # Optional: human-readable instructions
```

### Starter File Template

```python
# lesson_01.py
# Complete the function below to return the sum of two numbers.

def add_numbers(a, b):
    # Replace this with your solution
    pass
```

### Test File Template

```python
# test_lesson_01.py
import unittest
from lesson_01 import add_numbers

class TestAddNumbers(unittest.TestCase):
    def test_adds_two_numbers(self):
        self.assertEqual(add_numbers(2, 3), 5)

    def test_adds_negative_numbers(self):
        self.assertEqual(add_numbers(-1, -1), -2)

    def test_adds_zero(self):
        self.assertEqual(add_numbers(0, 5), 5)

if __name__ == "__main__":
    unittest.main()
```

### INSTRUCTIONS.md Template

```markdown
# Lesson 1: Return Values

## Objective
Learn the difference between `print()` and `return` in Python functions.

## Instructions
1. Complete the `add_numbers` function in `lesson_01.py`
2. The function should return the sum of two numbers
3. Run tests with `ducky test`

## Hints
- Use `return` to send a value back from the function
- `print()` only displays text — it doesn't return a value
```

### Diagnostic Cases

Add diagnostic cases to `manifest.json` for better hints:

```json
{
  "diagnostic_cases": [
    {
      "name": "adds two numbers",
      "function_name": "add_numbers",
      "args": [2, 3],
      "expected": 5
    },
    {
      "name": "adds negative numbers",
      "function_name": "add_numbers",
      "args": [-1, -1],
      "expected": -2
    }
  ]
}
```

---

## Teacher Server

> The commands below under "Teacher Server" belong to the older Ducky teacher
> tooling. The **practice site** -- the browser UI students use to write and
> submit Python -- is started with `ducky web`, documented next. They are
> different programs and `ducky teacher serve` does not bring up the practice
> site.

## The practice site

### Build the content database

The site reads a verified content database. Without one it refuses to start
rather than serving an empty list.

```bash
python tools/build_content.py \
  --db /path/to/coding_platform.db \
  --out ~/.ducky/content/content.db
```

### Issue access codes

Reading is open; running code is not. Codes are per student, not per class, so
that "who ran this and how much CPU did it burn" is answerable and one machine
cannot be saturated by a single shared secret.

```bash
ducky access-codes c1 Ada Bob --class-name "Period 1"
```

Hand each code out individually. Anyone holding one can run code on this
machine.

### Start the server

```bash
# This machine only
ducky web --host 127.0.0.1

# The local network -- the usual case for a classroom
ducky web --allow-lan
```

Binding `0.0.0.0` requires `--allow-lan` rather than a warning, because it makes
a code-executing service reachable by every device on the network. The banner
prints the URLs to share and points at `docs/SECURITY_MODEL.md`; read that before
putting this on a network you do not control.

`--content` and `--classroom` point at specific databases, and must name the same
files `build_content.py` and `access-codes` used, or nothing will be served and
no code will be accepted.

### What a student sees

- The problem list is paged at 50 per page, so every verified problem is
  reachable; the earlier 200-item cap left 2,400 of 2,603 reachable only by
  guessing a filter.
- The editor autosaves after a pause and restores the draft when a student comes
  back. The save indicator tells the truth about which one it is.
- **Run samples** checks the visible tests only and records nothing.
  **Submit solution** runs every test including hidden ones and records the
  attempt. These were the same request until recently.
- An expired session reveals an access-code field in place, rather than telling
  the student to navigate away from unsaved work.

---

## Teacher Server

### Starting the Server

```bash
# Basic usage
ducky teacher serve

# With options
ducky teacher serve \
  --port 8420 \
  --class-name "Period 1" \
  --host 0.0.0.0
```

### Managing Classes

```bash
# List classes
ducky teacher classes

# Add a new class
ducky teacher add-class --name "Period 2" --subject "Python 101"
```

### Managing Packs

```bash
# List available packs
ducky teacher packs

# Add a pack
ducky teacher add-pack \
  --source lesson.duckypack \
  --lesson-id lesson_01 \
  --title "Return Values"

# Remove a pack
ducky teacher remove-pack --lesson-id lesson_01
```

### Managing Students

```bash
# List connected students
ducky teacher students

# View student details
ducky teacher students --verbose
```

### Viewing Submissions

```bash
# List recent submissions
ducky teacher reports

# View submission details
ducky teacher reports --student "Ada" --lesson lesson_01

# Export submissions
ducky teacher export --output class_data.jsonl
```

### Sending Announcements

```bash
# Send announcement to all students
ducky teacher announce --message "New lesson available: Conditionals"

# Send to specific class
ducky teacher announce --class "Period 1" --message "Assignment due tomorrow"
```

---

## LLM Integration

### Setting Up LLM Hints

Ducky supports optional LLM backends for smarter hints:

#### Option 1: Gemini (Free Tier)

1. Get API key at https://aistudio.google.com/apikey
2. Configure:
   ```bash
   ducky config set --llm-backend gemini --gemini-api-key YOUR_KEY
   ```

#### Option 2: Ollama (Local)

1. Install Ollama: https://ollama.com
2. Pull a model:
   ```bash
   ollama pull qwen2.5-coder:1.5b
   ```
3. Configure:
   ```bash
   ducky config set --llm-backend ollama --ollama-model qwen2.5-coder:1.5b
   ```

#### Option 3: LM Studio (Local)

1. Install LM Studio: https://lmstudio.ai
2. Start the server in LM Studio
3. Configure:
   ```bash
   ducky config set --llm-backend lmstudio --lmstudio-url http://localhost:1234
   ```

### Per-Class LLM Settings

Each class can have its own LLM configuration:

```bash
# Set LLM for specific class
ducky teacher config set --class-id period_1 --llm-backend gemini
ducky teacher config set --class-id period_2 --llm-backend ollama
```

### LLM Hint Proxy

When the teacher server has LLM configured, students automatically get AI hints without any setup:

1. Teacher configures LLM
2. Student requests hint via `POST /api/hint`
3. Server forwards request to LLM backend
4. Server returns hint to student

---

## Workspace Management

### Teacher Workspace Location

```
~/.ducky/teacher/workspace/
├── lessons/           # Lesson source folders
│   ├── lesson_01_return_values/
│   ├── lesson_02_conditionals/
│   └── lesson_grade_calculator/
└── built_packs/       # Compiled .duckypack files
    ├── lesson_01_return_values.duckypack
    └── lesson_02_conditionals.duckypack
```

### GUI Workspace Management

```bash
# Open teacher GUI
ducky teacher gui

# In the GUI:
# [n] Create new blank assignment
# [b] Build pack from lesson folder
# [a] Add pack to server
# [s] Sync from repo assignments/
# [d] Delete lesson
# [o] Open lesson folder
```

### Syncing from Repository

```bash
# Sync lessons from repo assignments/ folder
ducky teacher scan --source assignments/
```

---

## Dataset Export

### Exporting Class Data

```bash
# Export all reports to JSONL
ducky teacher dataset export --output dataset.jsonl

# Export last run only
ducky teacher dataset export-last --output last_run.jsonl

# Preview dataset
ducky teacher dataset preview --input dataset.jsonl

# Dataset statistics
ducky teacher dataset stats --input dataset.jsonl
```

### Dataset Format

```json
{
  "task": "explain_python_test_failure",
  "lesson_id": "python_basics.lesson_01",
  "student_code_redacted": "def add_numbers(a, b):\n    print(a + b)",
  "test_failure": {
    "name": "test_adds_two_numbers",
    "expected": 5,
    "actual": null,
    "diagnosis": "prints_instead_of_returns"
  },
  "target": {
    "hint": "Your function prints the result but doesn't return it. Use 'return' instead of 'print'."
  }
}
```

---

## Privacy and Redaction

### Automatic Redaction

Before export, Ducky automatically redacts:
- Email addresses
- Phone numbers
- Absolute file paths
- API keys and tokens
- Personal comments

### Manual Redaction

```bash
# Redact a specific file
ducky teacher redact --input code.py --strip-comments

# Redact all reports
ducky teacher redact --input reports/ --output redacted_reports/
```

### Privacy Settings

- No cloud sync by default
- No automatic telemetry
- Reports stay local unless manually exported
- Teacher backend submission is opt-in

---

## Best Practices

### Lesson Design

1. **Start simple** — One concept per lesson
2. **Clear instructions** — Write `INSTRUCTIONS.md` with objectives
3. **Test cases** — Cover edge cases (negatives, zeros, empty inputs)
4. **Diagnostic cases** — Add to manifest for better hints
5. **Concept tags** — Use tags for categorization

### Classroom Management

1. **Access codes** — Generate new codes each session
2. **Monitor progress** — Check submissions regularly
3. **Announcements** — Keep students updated
4. **Export data** — Backup class data regularly

### LLM Configuration

1. **Start without LLM** — Rule-based hints work great
2. **Add LLM gradually** — Test with one class first
3. **Monitor usage** — Check LLM status via API
4. **Fallback ready** — Rule-based hints always available

---

## Troubleshooting

### Server Won't Start

```bash
# Check if port is in use
lsof -i :8420

# Use different port
ducky teacher serve --port 8421

# Check firewall settings
```

### Students Can't Connect

1. Verify teacher IP address
2. Check firewall allows port 8420
3. Confirm access code is correct
4. Test with `curl http://TEACHER_IP:8420/api/status`

### Packs Not Distributing

```bash
# Verify pack exists
ducky teacher packs

# Re-add pack
ducky teacher add-pack --source lesson.duckypack

# Check server logs
```

### LLM Not Working

```bash
# Check LLM status
curl http://localhost:8420/api/llm-status

# Verify config
ducky config show

# Test LLM directly
ducky test --lesson test_lesson --verbose
```

---

## Commands Reference

### Server Commands

| Command | Description |
|---------|-------------|
| `ducky teacher serve` | Start teacher server |
| `ducky teacher classes` | List classes |
| `ducky teacher add-class` | Create new class |
| `ducky teacher packs` | List available packs |
| `ducky teacher add-pack` | Add pack to server |
| `ducky teacher remove-pack` | Remove pack from server |
| `ducky teacher students` | List connected students |
| `ducky teacher reports` | View submissions |
| `ducky teacher export` | Export class data |
| `ducky teacher announce` | Send announcement |

### Pack Commands

| Command | Description |
|---------|-------------|
| `ducky teacher pack create` | Create .duckypack |
| `ducky teacher pack inspect` | Inspect pack contents |
| `ducky teacher scan` | Scan folder and build packs |

### Configuration Commands

| Command | Description |
|---------|-------------|
| `ducky teacher config show` | Show configuration |
| `ducky teacher config set` | Update configuration |

### Dataset Commands

| Command | Description |
|---------|-------------|
| `ducky teacher dataset export` | Export all reports |
| `ducky teacher dataset export-last` | Export last run |
| `ducky teacher dataset preview` | Preview dataset |
| `ducky teacher dataset stats` | Dataset statistics |

### Utility Commands

| Command | Description |
|---------|-------------|
| `ducky teacher redact` | Redact sensitive info |
| `ducky teacher gui` | Open teacher GUI |
