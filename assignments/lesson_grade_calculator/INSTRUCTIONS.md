# Advanced Grade Calculator

## Goal

Create a Python program named:

```text
grade_calculator.py
```

Your program will calculate one student's final result using multiple functions.

The program should ask the user for:

```text
Student name
Grade 1
Grade 2
Grade 3
```

Then it should calculate the average, find the letter grade, decide if the student passed or failed, and print a final report.

---

## Important Restrictions

You may **not** use:

```python
while
try
except
```

This assignment should be completed using:

```text
functions
input()
int()
if / elif / else
return
f-strings
```

---

## Grade Scale

Use this grade scale:

```text
90-100 = A
80-89  = B
70-79  = C
60-69  = D
0-59   = F
```

Important rule:

```text
Only use >
Do not use >=
```

So your letter grade logic should use:

```text
average > 89
average > 79
average > 69
average > 59
else
```

---

## Pass / Fail Rules

```text
A, B, C, D = Pass
F = Fail
```

If the student passes, the result message should be:

```text
You passed. You can go to Disney World!
```

If the student fails, the result message should be:

```text
You failed. You must go to summer school.
```

---

## Summer School Rules

Only students with an F need summer school.

```text
50-59 = 1 summer school class required
40-49 = 2 summer school classes required
30-39 = 3 summer school classes required
0-29  = 4 summer school classes required
```

Because you are only using `>`, use:

```text
average > 49
average > 39
average > 29
else
```

If the student passed, summer school classes should be:

```text
0
```

---

## Required Functions

Your program must include these exact function names.

### 1. `get_student_name()`

Ask the user for the student's name and return the name.

### 2. `get_grade(prompt)`

Ask the user for one grade, convert it into an integer, and return the grade.

### 3. `is_valid_grade(grade)`

Return `True` if the grade is from 0 to 100. Return `False` if the grade is below 0 or above 100.

### 4. `all_grades_valid(grade1, grade2, grade3)`

Return `True` only if all three grades are valid. Return `False` if at least one grade is invalid.

### 5. `calculate_average(grade1, grade2, grade3)`

Calculate and return the average of the three grades.

### 6. `get_letter_grade(average)`

Convert the average into a letter grade.

Rules:

```text
average > 89 -> A
average > 79 -> B
average > 69 -> C
average > 59 -> D
else         -> F
```

### 7. `get_pass_status(letter_grade)`

Return `"Pass"` if the student passed. Return `"Fail"` if the student failed.

### 8. `get_result_message(status)`

Return the correct result message.

Rules:

```text
Pass -> You passed. You can go to Disney World!
Fail -> You failed. You must go to summer school.
```

### 9. `get_summer_school_classes(average, status)`

Return the number of summer school classes required.

Rules:

```text
If status is Pass -> 0

If status is Fail:
average > 49 -> 1
average > 39 -> 2
average > 29 -> 3
else         -> 4
```

### 10. `print_report(name, grade1, grade2, grade3, average, letter_grade, status, result_message, summer_classes)`

Print a clean final report.

The report should include:

```text
Student name
Grade 1
Grade 2
Grade 3
Average
Letter Grade
Status
Result message
Summer school classes required
```

### 11. `main()`

Control the full program.

The `main()` function should:

```text
1. Get the student name.
2. Get grade 1.
3. Get grade 2.
4. Get grade 3.
5. Check if all grades are valid.
6. If a grade is invalid, print an invalid-grade message.
7. If all grades are valid, calculate the average.
8. Get the letter grade.
9. Get the pass/fail status.
10. Get the result message.
11. Get the summer school class count.
12. Print the final report.
```

At the bottom of your file, use a main guard so the tests can import your functions without running the whole program:

```python
if __name__ == "__main__":
    main()
```

---

## Example Passing Run

```text
Enter student name: Maya
Enter grade 1: 95
Enter grade 2: 88
Enter grade 3: 91

===== Final Grade Report =====
Student: Maya
Grade 1: 95
Grade 2: 88
Grade 3: 91
Average: 91.33333333333333
Letter Grade: A
Status: Pass
Result: You passed. You can go to Disney World!
Summer School Classes Required: 0
==============================
```

---

## Example Failing Run

```text
Enter student name: Jordan
Enter grade 1: 55
Enter grade 2: 45
Enter grade 3: 50

===== Final Grade Report =====
Student: Jordan
Grade 1: 55
Grade 2: 45
Grade 3: 50
Average: 50.0
Letter Grade: F
Status: Fail
Result: You failed. You must go to summer school.
Summer School Classes Required: 1
==============================
```

---

## Example Invalid Run

```text
Enter student name: Chris
Enter grade 1: 90
Enter grade 2: 105
Enter grade 3: 80

Invalid grade. Grades must be from 0 to 100.
```

---

## How To Run Your Program

In the VS Code terminal:

```bash
python grade_calculator.py
```

---

## How To Run The Tests

Install pytest:

```bash
pip install pytest
```

Run the tests:

```bash
pytest
```
