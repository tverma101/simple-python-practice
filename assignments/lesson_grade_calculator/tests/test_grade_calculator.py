import ast
import builtins
import inspect

import grade_calculator


REQUIRED_FUNCTIONS = [
    "get_student_name",
    "get_grade",
    "is_valid_grade",
    "all_grades_valid",
    "calculate_average",
    "get_letter_grade",
    "get_pass_status",
    "get_result_message",
    "get_summer_school_classes",
    "print_report",
    "main",
]


def test_required_functions_exist():
    for function_name in REQUIRED_FUNCTIONS:
        assert hasattr(grade_calculator, function_name), f"Missing function: {function_name}"
        assert callable(getattr(grade_calculator, function_name)), f"{function_name} must be a function"


def test_no_while_or_try_used():
    source = inspect.getsource(grade_calculator)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        assert not isinstance(node, ast.While), "Do not use while loops in this assignment"
        assert not isinstance(node, ast.Try), "Do not use try/except in this assignment"


def test_main_guard_used():
    source = inspect.getsource(grade_calculator)
    assert 'if __name__ == "__main__"' in source or "if __name__ == '__main__'" in source, (
        "Use a main guard at the bottom: if __name__ == '__main__': main()"
    )


def test_is_valid_grade():
    assert grade_calculator.is_valid_grade(0) is True
    assert grade_calculator.is_valid_grade(59) is True
    assert grade_calculator.is_valid_grade(100) is True
    assert grade_calculator.is_valid_grade(-1) is False
    assert grade_calculator.is_valid_grade(101) is False


def test_all_grades_valid():
    assert grade_calculator.all_grades_valid(90, 80, 70) is True
    assert grade_calculator.all_grades_valid(0, 50, 100) is True
    assert grade_calculator.all_grades_valid(-1, 80, 70) is False
    assert grade_calculator.all_grades_valid(90, 101, 70) is False
    assert grade_calculator.all_grades_valid(90, 80, 150) is False


def test_calculate_average():
    assert grade_calculator.calculate_average(90, 80, 70) == 80.0
    assert grade_calculator.calculate_average(100, 100, 100) == 100.0
    assert grade_calculator.calculate_average(0, 0, 0) == 0.0
    assert grade_calculator.calculate_average(55, 45, 50) == 50.0


def test_get_letter_grade():
    assert grade_calculator.get_letter_grade(100) == "A"
    assert grade_calculator.get_letter_grade(90) == "A"
    assert grade_calculator.get_letter_grade(89) == "B"
    assert grade_calculator.get_letter_grade(80) == "B"
    assert grade_calculator.get_letter_grade(79) == "C"
    assert grade_calculator.get_letter_grade(70) == "C"
    assert grade_calculator.get_letter_grade(69) == "D"
    assert grade_calculator.get_letter_grade(60) == "D"
    assert grade_calculator.get_letter_grade(59) == "F"
    assert grade_calculator.get_letter_grade(0) == "F"


def test_get_pass_status():
    assert grade_calculator.get_pass_status("A") == "Pass"
    assert grade_calculator.get_pass_status("B") == "Pass"
    assert grade_calculator.get_pass_status("C") == "Pass"
    assert grade_calculator.get_pass_status("D") == "Pass"
    assert grade_calculator.get_pass_status("F") == "Fail"


def test_get_result_message():
    assert grade_calculator.get_result_message("Pass") == "You passed. You can go to Disney World!"
    assert grade_calculator.get_result_message("Fail") == "You failed. You must go to summer school."


def test_get_summer_school_classes():
    assert grade_calculator.get_summer_school_classes(95, "Pass") == 0
    assert grade_calculator.get_summer_school_classes(60, "Pass") == 0
    assert grade_calculator.get_summer_school_classes(59, "Fail") == 1
    assert grade_calculator.get_summer_school_classes(50, "Fail") == 1
    assert grade_calculator.get_summer_school_classes(49, "Fail") == 2
    assert grade_calculator.get_summer_school_classes(40, "Fail") == 2
    assert grade_calculator.get_summer_school_classes(39, "Fail") == 3
    assert grade_calculator.get_summer_school_classes(30, "Fail") == 3
    assert grade_calculator.get_summer_school_classes(29, "Fail") == 4
    assert grade_calculator.get_summer_school_classes(0, "Fail") == 4


def test_get_student_name(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: "Maya")
    assert grade_calculator.get_student_name() == "Maya"


def test_get_grade(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda prompt: "88")
    assert grade_calculator.get_grade("Enter grade: ") == 88


def test_print_report_passing_student(capsys):
    grade_calculator.print_report(
        "Maya",
        95,
        88,
        91,
        91.33333333333333,
        "A",
        "Pass",
        "You passed. You can go to Disney World!",
        0,
    )

    output = capsys.readouterr().out

    assert "Maya" in output
    assert "95" in output
    assert "88" in output
    assert "91" in output
    assert "91.33333333333333" in output
    assert "A" in output
    assert "Pass" in output
    assert "You passed. You can go to Disney World!" in output
    assert "0" in output


def test_print_report_failing_student(capsys):
    grade_calculator.print_report(
        "Jordan",
        55,
        45,
        50,
        50.0,
        "F",
        "Fail",
        "You failed. You must go to summer school.",
        1,
    )

    output = capsys.readouterr().out

    assert "Jordan" in output
    assert "55" in output
    assert "45" in output
    assert "50" in output
    assert "50.0" in output
    assert "F" in output
    assert "Fail" in output
    assert "You failed. You must go to summer school." in output
    assert "1" in output


def test_main_passing_flow(monkeypatch, capsys):
    answers = iter(["Maya", "95", "88", "91"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    grade_calculator.main()
    output = capsys.readouterr().out

    assert "Maya" in output
    assert "A" in output
    assert "Pass" in output
    assert "Disney World" in output


def test_main_failing_flow(monkeypatch, capsys):
    answers = iter(["Jordan", "55", "45", "50"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    grade_calculator.main()
    output = capsys.readouterr().out

    assert "Jordan" in output
    assert "F" in output
    assert "Fail" in output
    assert "summer school" in output
    assert "1" in output


def test_main_invalid_flow(monkeypatch, capsys):
    answers = iter(["Chris", "90", "105", "80"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(answers))

    grade_calculator.main()
    output = capsys.readouterr().out

    assert "Invalid grade" in output
