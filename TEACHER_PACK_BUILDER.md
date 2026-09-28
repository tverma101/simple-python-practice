# Teacher / Pack Builder Guide

## Creating a Lesson

1. Create a folder structure:

```
my_lesson/
  starter/
    lesson_01.py
  tests/
    test_lesson_01.py
  quizzes/
    quiz.json
```

2. The starter file should contain the function signature with a placeholder:

```python
def add_numbers(a, b):
    # Replace this with your solution.
    pass
```

3. The test file should import from the starter and test the function:

```python
import unittest
from lesson_01 import add_numbers

class TestAddNumbers(unittest.TestCase):
    def test_adds_two_numbers(self):
        self.assertEqual(add_numbers(2, 3), 5)
```

## Packaging into .duckypack

```bash
ducky pack create \
  --source my_lesson/ \
  --output my_lesson.duckypack \
  --lesson-id python_basics.lesson_01 \
  --title "Return Values" \
  --entry-file lesson_01.py \
  --tags "functions,return_vs_print"
```

`ducky pack create` discovers `my_lesson/quizzes/*.json` automatically. For a
shared teacher content folder, place `<lesson-id>.json` in a sibling
`quizzes/` directory and use `ducky teacher scan --source assignments`; the
resulting pack contains one validated `quizzes.json` alongside the assignment.

The student-facing quiz format is deterministic JSON. It supports
`multiple_choice`, `true_false`, and `predict_output`, plus optional
`concept`, `difficulty`, `shuffle_questions`, `source`, and `review_mode`
metadata. `quizzes/python_basics_15.json` is a compatible sample converted
from the PythonQuiz platform's Unit 1 seed bank.

## Inspecting a Pack

```bash
ducky pack inspect my_lesson.duckypack
```

## Manifest Format

```json
{
  "pack_version": "1.0",
  "lesson_id": "python_basics.lesson_01",
  "title": "Return Values",
  "language": "python",
  "entry_file": "lesson_01.py",
  "test_command": "python -m pytest tests/",
  "concept_tags": ["functions", "return_vs_print"],
  "diagnostic_cases": [
    {
      "name": "adds two numbers",
      "function_name": "add_numbers",
      "args": [2, 3],
      "expected": 5
    }
  ]
}
```

## Diagnostic Cases

Diagnostic cases help Ducky give better hints. Each case specifies:

- `name`: description of the test case
- `function_name`: the function to call
- `args`: arguments to pass
- `expected`: expected return value

When a student's code fails, Ducky runs these cases separately to diagnose the specific problem (e.g., print instead of return, wrong type, missing function).
