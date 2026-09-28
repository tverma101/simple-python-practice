# Lesson 01: Return Values

## Goal
Write a function that **returns** a value instead of printing it.

## Instructions

In this lesson you will learn the difference between printing and returning.

### What to do

1. Open the file `solution.py` in your editor
2. Find the `add(a, b)` function
3. Change it to **return** the result instead of printing it

### Before (broken)
```python
def add(a, b):
    print(a + b)
```

### After (correct)
```python
def add(a, b):
    return a + b
```

### Why returning matters

- **Printed output** goes to the screen and disappears
- **Returned values** can be saved, reused, and tested
- Functions that return values are more useful and testable

## Testing

Run your tests with:
```bash
ducky check examples/lesson_01_return_values.duckypack
```

Or if connected to a teacher:
```bash
ducky test
```

## Hints

If you get stuck:
- Look at the test file in `tests/test_solution.py`
- Use `ducky tutor <your-report.json>` for hints
