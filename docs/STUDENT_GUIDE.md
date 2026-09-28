# Student Guide

## Overview

This guide covers everything students need to know to use Ducky for learning Python programming.

## Quick Start

### 1. Install Ducky

**One-command install (recommended):**
```bash
curl -sSL https://pub-be8cbd81214d42469aa45b91d710100b.r2.dev/student-install.sh | bash
```

**Or install from source:**
```bash
pip install git+https://github.com/tverma101/simple-python-practice.git
```

### 2. Initialize Your Workspace

```bash
ducky init
```

This creates:
- `~/ducky-workspace/` — Your lesson files
- `~/.ducky/` — Ducky configuration and data

### 3. Start Learning

```bash
ducky  # Opens interactive menu
```

---

## Interactive Menu

When you run `ducky`, you see the interactive menu:

```
  ╔══════════════════════════════════════════════════════════════╗
  ║                    Ducky — Student Mode                     ║
  ╠══════════════════════════════════════════════════════════════╣
  ║  1. Lessons        — View and start lessons                 ║
  ║  2. Test           — Run tests on current lesson            ║
  ║  3. Hint           — Get hint for last failure              ║
  ║  4. Instructions   — View lesson instructions               ║
  ║  5. Connect        — Connect to teacher server              ║
  ║  6. Update         — Pull new lessons from teacher          ║
  ║  7. Submit         — Submit work to teacher                 ║
  ║  8. Settings       — Configure LLM and connection           ║
  ║  0. Quit                                                   ║
  ╚══════════════════════════════════════════════════════════════╝
```

---

## Working with Lessons

### Viewing Lessons

```bash
# List available lessons
ducky student lessons

# Or use the menu
ducky  # Select "Lessons"
```

### Starting a Lesson

```bash
# Start a specific lesson
ducky student start lesson_01_return_values

# Or use the menu
ducky  # Select "Lessons" → Select lesson
```

This extracts the lesson to your workspace:
```
~/ducky-workspace/
├── lesson_01.py          # Your starter code
└── .ducky-meta.json      # Lesson metadata
```

### Viewing Instructions

```bash
# View current lesson instructions
ducky instructions

# Or use the menu
ducky  # Select "Instructions"
```

### Writing Code

1. Open `~/ducky-workspace/lesson_01.py` in your editor
2. Read the instructions
3. Complete the function:
   ```python
   def add_numbers(a, b):
       # Your solution here
       return a + b
   ```

### Running Tests

```bash
# Run tests on current lesson
ducky test

# Run tests on specific lesson
ducky test --lesson lesson_01_return_values
```

**Example output:**
```
Running tests for: Return Values
────────────────────────────────────────────────────────

  ✓ test_adds_two_numbers (0.01s)
  ✓ test_adds_negative_numbers (0.01s)
  ✓ test_adds_zero (0.01s)

  Results: 3 passed, 0 failed
  Time: 0.03s

  All tests passed! Great work.
```

### Getting Hints

If tests fail, Ducky provides hints:

```bash
# Get hint for last failure
ducky hint
```

**Example output:**
```
Hint Level 1:
  Your function is printing the result instead of returning it.
  Remember: print() displays text, return sends a value back.

Hint Level 2:
  Change: print(a + b)
  To:     return(a + b)

Hint Level 3:
  The function should use the return statement to send the sum
  back to the caller. The return value is what the test checks.
```

---

## Connecting to Teacher

### Getting the Access Code

Your teacher will provide a **9-digit access code** and their **IP address**.

### Connecting

```bash
# Connect to teacher server
ducky student connect \
  --code 123456789 \
  --name "Your Name" \
  --url http://TEACHER_IP:8420
```

**Example:**
```bash
ducky student connect \
  --code 123456789 \
  --name "Ada Lovelace" \
  --url http://192.168.1.100:8420
```

### Pulling New Lessons

```bash
# Get new lessons from teacher
ducky student update
```

### Submitting Work

```bash
# Submit current lesson to teacher
ducky student submit
```

### Checking Connection Status

```bash
# Check if connected to teacher
ducky student status
```

---

## CLI Commands

### Student Commands

| Command | Description |
|---------|-------------|
| `ducky` | Open interactive menu |
| `ducky test` | Run tests on current lesson |
| `ducky test --lesson <id>` | Run tests on specific lesson |
| `ducky hint` | Show hint for last failure |
| `ducky instructions` | View lesson instructions |
| `ducky open` | Open lesson in editor |
| `ducky init [dir]` | Initialize workspace |
| `ducky student update` | Pull lessons your teacher shared |
| `ducky setup-llm` | Configure LLM hints (optional) |
| `ducky student connect` | Connect to teacher server |
| `ducky student update` | Pull new lessons |
| `ducky student submit` | Submit work to teacher |
| `ducky student lessons` | List available lessons |
| `ducky student start <id>` | Start a lesson |
| `ducky student status` | Check connection status |

### Convenience Commands

```bash
# Quick test (shorthand)
ducky test

# View instructions
ducky instructions

# Open in editor
ducky open
```

---

## Workspace Structure

### Default Locations

```
~/ducky-workspace/                 # Active lesson files
~/.ducky/
├── config.json                    # Configuration
├── packs/                         # Downloaded lesson packs
├── reports/                       # Test run reports
├── progress.db                    # Progress tracking
└── workspace/                     # Additional workspace data
```

### Lesson Files

When you start a lesson, files are extracted to your workspace:

```
~/ducky-workspace/
├── lesson_01.py                   # Your starter code (edit this)
├── test_lesson_01.py              # Test cases (don't edit)
└── .ducky-meta.json               # Lesson metadata
```

### Progress Tracking

Ducky tracks your progress automatically:

```bash
# View progress for a lesson
ducky progress --lesson python_basics.lesson_01
```

**Example output:**
```
Lesson: python_basics.lesson_01
  Total:   3
  Passed:  2
  Failed:  1
```

---

## LLM Hints (Optional)

Ducky works great without any LLM. Built-in rule-based diagnostics detect:
- Syntax errors
- Missing functions
- Wrong return types
- Print vs return issues
- Function signature mismatches

### Setting Up Gemini (Free)

1. Get API key at https://aistudio.google.com/apikey
2. Run:
   ```bash
   ducky setup-llm
   ```
3. Choose option 1 (Gemini) and paste your key

### Setting Up Ollama (Local)

1. Install Ollama: https://ollama.com
2. Pull a model:
   ```bash
   ollama pull qwen2.5-coder:1.5b
   ```
3. Configure:
   ```bash
   ducky config set --llm-backend ollama
   ```

### Teacher-Provided LLM

When connected to a teacher server, you automatically get AI hints through the teacher's LLM — no setup needed!

---

## Importing Assignments

### From .duckypack File

```bash
# Drop the .duckypack into your assignments/ folder (created by `ducky init`),
# then re-run `ducky` — it appears in your lesson list. Or pull from your teacher:
ducky student update
```

---

## Auto-Update

Ducky checks for updates automatically on every launch.

### Enable/Disable

```bash
# Enable auto-update (default)
ducky config set --auto-update-app true

# Disable auto-update
ducky config set --auto-update-app false
```

### Manual Update

```bash
# Check for updates
ducky student update
```

---

## Reports

### Viewing Reports

```bash
# List recent reports
ducky report list

# View latest report
ducky report latest

# View specific report
ducky report view REPORT_ID
```

### Report Format

```json
{
  "lesson_id": "python_basics.lesson_01",
  "title": "Return Values",
  "passed": false,
  "tests_total": 3,
  "tests_passed": 2,
  "tests_failed": 1,
  "duration_seconds": 0.03,
  "failures": [
    {
      "name": "test_adds_two_numbers",
      "diagnosis": "prints_instead_of_returns",
      "expected": 5,
      "actual": null,
      "hint": "Your function prints the result instead of returning it."
    }
  ]
}
```

---

## Troubleshooting

### "No lessons found"

```bash
# Check workspace
ls ~/ducky-workspace/

# Re-initialize
ducky init

# Check packs
ls ~/.ducky/packs/
```

### "Tests not running"

```bash
# Verify pytest is installed
pip install pytest

# Check lesson files
ls ~/ducky-workspace/

# Run with verbose output
ducky test --verbose
```

### "Can't connect to teacher"

```bash
# Verify teacher IP and port
ping TEACHER_IP

# Check access code
echo "Code: 123456789"

# Test connection
curl http://TEACHER_IP:8420/api/status
```

### "Hints not showing"

```bash
# Check LLM configuration
ducky config show

# Test with rule-based hints
ducky hint

# Verify the lesson pack downloaded correctly
ducky student update
```

### "Finder windows opening"

This is a known issue. If tests open Finder windows:
1. Check for missing mocks in test files
2. Report to teacher
3. Workaround: Close Finder windows manually

---

## Tips for Success

### Writing Code

1. **Read instructions first** — Understand what's expected
2. **Start simple** — Get basic functionality working
3. **Test often** — Run `ducky test` after each change
4. **Read hints** — They're designed to help you learn
5. **Ask teacher** — Use the hint system, then ask for help

### Common Mistakes

1. **print vs return** — Use `return` to send values back
2. **Indentation** — Python uses spaces, not tabs
3. **Function names** — Match the exact name in tests
4. **Case sensitivity** — `MyFunction` ≠ `myfunction`
5. **Import paths** — Use relative imports in lessons

### Getting Help

1. **Built-in hints** — Run `ducky hint`
2. **Instructions** — Run `ducky instructions`
3. **Teacher** — Ask your teacher for help
4. **Documentation** — Read this guide

---

## Example Workflow

### Complete Lesson Workflow

```bash
# 1. Initialize workspace
ducky init

# 2. Start lesson
ducky student start lesson_01_return_values

# 3. View instructions
ducky instructions

# 4. Write code in editor
# Open ~/ducky-workspace/lesson_01.py
# Complete the function

# 5. Run tests
ducky test

# 6. If tests fail, get hints
ducky hint

# 7. Fix code and re-test
ducky test

# 8. When all tests pass, submit
ducky student submit

# 9. Move to next lesson
ducky student start lesson_02_conditionals
```

### Connected to Teacher

```bash
# 1. Connect to teacher
ducky student connect --code 123456789 --name "Ada" --url http://192.168.1.100:8420

# 2. Pull new lessons
ducky student update

# 3. Start lesson
ducky student start lesson_01_return_values

# 4. Complete and submit
ducky student submit

# 5. Check for new lessons
ducky student update
```

---

## Commands Reference

### Core Commands

| Command | Description |
|---------|-------------|
| `ducky` | Open interactive menu |
| `ducky test` | Run tests on current lesson |
| `ducky hint` | Show hint for last failure |
| `ducky instructions` | View lesson instructions |
| `ducky open` | Open lesson in editor |

### Workspace Commands

| Command | Description |
|---------|-------------|
| `ducky init [dir]` | Initialize workspace |
| `ducky student update` | Pull lessons from teacher |
| `ducky progress` | View progress |

### Teacher Connection

| Command | Description |
|---------|-------------|
| `ducky student connect` | Connect to teacher |
| `ducky student update` | Pull new lessons |
| `ducky student submit` | Submit work |
| `ducky student status` | Check status |

### Configuration

| Command | Description |
|---------|-------------|
| `ducky config show` | Show configuration |
| `ducky config set` | Update configuration |
| `ducky setup-llm` | Configure LLM hints |

### Reports

| Command | Description |
|---------|-------------|
| `ducky report list` | List reports |
| `ducky report latest` | View latest report |
| `ducky report view` | View specific report |
