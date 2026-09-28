from __future__ import annotations

import re
from typing import Any


def _as_text(value: Any) -> str:
    if value is None:
        return "None"
    return str(value).strip()


def _parse_attribute_error(error_text: str) -> tuple[str | None, str | None]:
    """Extract (module_name, missing_attr) from an AttributeError or ImportError message.

    Examples:
        ``module 'solution' has no attribute 'calc_grade'`` → ``('solution', 'calc_grade')``
        ``cannot import name 'calc_grade' from 'solution'`` → ``('solution', 'calc_grade')``
        ``cannot import name 'calc_grade' from partially initialized module 'solution'`` → ``('solution', 'calc_grade')``
    """
    import re

    match = re.search(r"module\s+['\"]([^'\"]+)['\"]\s+has no attribute\s+['\"]([^'\"]+)['\"]", error_text)
    if match:
        return match.group(1), match.group(2)
    match = re.search(
        r"cannot import name\s+['\"]([^'\"]+)['\"]\s+from\s+(?:partially initialized module\s+)?['\"]([^'\"]+)['\"]",
        error_text,
    )
    if match:
        return match.group(2), match.group(1)
    return None, None


def _parse_param_count_error(error_text: str) -> tuple[int | None, int | None]:
    """Extract (student_param_count, test_call_arg_count) from a TypeError.

    Handles the canonical CPython messages::

        ... takes 1 positional argument but 2 were given
        ... takes from 1 to 2 positional arguments but 3 were given
        ... missing 1 required positional argument

    Returns ``(None, None)`` when neither shape is present, so callers can
    fall back to the generic parameter-count hint.
    """
    import re

    # "takes X positional argument(s) but Y were given" or "takes from X to Y positional arguments but Z were given"
    m = re.search(
        r"takes\s+(?:from\s+\d+\s+to\s+)?(\d+)\s+positional argument", error_text
    )
    if m:
        defined = int(m.group(1))
        given = None
        mg = re.search(r"(\d+)\s+were given", error_text)
        if mg:
            given = int(mg.group(1))
        if given is None:
            # "... takes 2 positional arguments but 1 was given" (singular)
            mg = re.search(r"(\d+)\s+was given", error_text)
            if mg:
                given = int(mg.group(1))
        if given is not None:
            return defined, given

    # "missing N required positional argument(s)" only names how many are
    # missing, not how many the function defines, so total arity is not
    # recoverable from this message alone. Fall back to the generic hint.
    if re.search(r"missing\s+\d+\s+required positional argument", error_text):
        return None, None

    return None, None


def classify_failure(
    *,
    expected: Any = None,
    actual: Any = None,
    stdout: str = "",
    error: str = "",
    raw_output: str = "",
    student_code: str = "",
    actual_jsonable: bool | None = None,
) -> str:
    error_str = str(error or "")
    raw_output_str = str(raw_output or "")
    combined = "\n".join([error_str, raw_output_str])

    # ── Syntax / import errors ──────────────────────────
    if "SyntaxError" in combined or "IndentationError" in combined:
        return "syntax_error"

    if "AttributeError" in combined and "module " in combined and "has no attribute" in combined:
        return "function_not_found"

    if "ImportError" in combined and "cannot import name" in combined:
        # "ImportError: cannot import name 'X' from 'Y'" means the module
        # exists but a specific function/class is missing — a naming issue.
        return "function_not_found"

    if "ModuleNotFoundError" in combined or "ImportError" in combined:
        return "import_error"

    if "EOFError" in combined:
        return "interactive_input"

    # ── Parameter / signature errors ────────────────────
    if "TypeError" in combined and ("takes" in combined and ("argument" in combined or "positional" in combined)):
        return "wrong_parameter_count"

    if "TypeError" in combined and ("missing" in combined and "required" in combined and "argument" in combined):
        return "wrong_parameter_count"

    # ── Assertion vs runtime ────────────────────────────
    runtime_markers = (
        "ZeroDivisionError",
        "IndexError",
        "KeyError",
        "NameError",
        "TypeError",
        "AttributeError",
        "ValueError",
        "OverflowError",
        "RecursionError",
        "StopIteration",
        "FileNotFoundError",
        "PermissionError",
    )
    is_assertion_failure = bool(
        error_str
        and ("AssertionError" in error_str or re.search(r"(?m)^\s*assert(?:\s|$)", error_str))
        and not ("AssertionError" not in error_str and any(marker in error_str for marker in runtime_markers))
    )
    if error_str and not is_assertion_failure:
        if "DuckyTimeoutError" in combined or "TimeoutError" in combined:
            return "timeout"
        if "DuckyCpuLimitError" in combined:
            # A CPU-budget kill is a timeout that we can name more precisely, and
            # the caller is expected to say so in the error text. Classifying it
            # as a bare runtime_error would be a regression: a student who wrote
            # `while True: pass` used to get "timeout" and would now get
            # something that points nowhere near their loop.
            return "timeout"
        if "ZeroDivisionError" in combined:
            return "division_by_zero"
        if "IndexError" in combined:
            return "index_out_of_range"
        if "KeyError" in combined:
            return "missing_key"
        if "NameError" in combined:
            return "undefined_variable"
        if "TypeError" in combined:
            return "type_error"
        if "AttributeError" in combined:
            return "attribute_error"
        if "ValueError" in combined:
            return "value_error"
        if "OverflowError" in combined:
            return "overflow_error"
        if "RecursionError" in combined:
            return "recursion_error"
        if "StopIteration" in combined:
            return "stop_iteration"
        if "FileNotFoundError" in combined:
            return "file_not_found"
        if "PermissionError" in combined:
            return "permission_error"
        return "runtime_error"

    # ── Wrong/no return ─────────────────────────────────
    if is_assertion_failure and actual is None:
        return "assertion_failed"
    if actual is None and stdout.strip() and _as_text(expected) == stdout.strip():
        return "prints_instead_of_returns"
    if actual is None:
        return "none_returned"

    if actual_jsonable is False:
        return "unserializable_return"

    if isinstance(expected, bool) and isinstance(actual, bool) and expected != actual:
        return "boolean_logic"

    if isinstance(expected, float) and isinstance(actual, float) and expected != actual:
        if abs(expected - actual) <= max(abs(expected), abs(actual), 1.0) * 1e-9:
            return "numeric_precision"

    if isinstance(expected, dict) and isinstance(actual, dict) and expected.keys() == actual.keys():
        if any(expected[key] != actual[key] for key in expected):
            return "container_value_mismatch"

    if type(expected) is not type(actual) and isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        return "collection_type_mismatch"

    # ── Type / formatting mismatches ────────────────────
    if type(expected).__name__ != type(actual).__name__:
        # Catch int/float mismatches (e.g. 80 vs 80.0) and str vs numeric (e.g. 0 vs '0')
        if _as_text(expected) == _as_text(actual):
            return "wrong_type"
        # Numeric type mismatch: values compare equal as numbers but types differ
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            if expected == actual:
                return "wrong_type"
    if isinstance(expected, str) and isinstance(actual, str):
        if expected.strip() == actual.strip() and expected != actual:
            return "extra_spaces"
        if expected.split() == actual.split() and expected != actual:
            return "extra_spaces"
        if expected.lower() == actual.lower() and expected != actual:
            return "case_mismatch"
    if (
        isinstance(expected, list)
        and isinstance(actual, list)
        and expected != actual
        and len(expected) == len(actual)
        and sorted(map(str, expected)) == sorted(map(str, actual))
        and {type(e) for e in expected} == {type(a) for a in actual}
    ):
        return "list_order_wrong"

    # ── Logic error ─────────────────────────────────────
    return "incorrect_logic"


def hint_for_diagnosis(diagnosis: str, context: dict | None = None, level: int = 1) -> str:
    """Return a student-friendly hint for a diagnosis category.

    Pass an optional *context* dict with extra keys like ``expected_fn``,
    ``closest_match``, ``missing_param_count``, etc. to get a tailored hint.

    The *level* controls hint escalation:

    - **1** (attempts 1–2): Gentle nudge — point to the general area
    - **2** (attempts 3–4): Specific pointer — name the issue and what to change
    - **3** (attempts 5+): Near full guidance — detailed fix with example
    """
    context = context or {}

    # ── Level 1: gentle nudges ─────────────────────────────────────
    _level1 = {
        "syntax_error": (
            "Python could not read your code. Check for missing colons (:), "
            "matching parentheses, and correct indentation."
        ),
        "function_not_found": _build_function_not_found_hint(context, level=1),
        "import_error": (
            "A module name in your code could not be found. Double-check the spelling in your ``import`` statement."
        ),
        "wrong_parameter_count": _build_param_count_hint(context, level=1),
        "division_by_zero": (
            "Your code tried to divide by zero somewhere. "
            "Check any line with ``/`` or ``//`` — what could the denominator be?"
        ),
        "index_out_of_range": (
            "Your code tried to access a position beyond the end of a list or string. "
            "Check your index values — are they within bounds?"
        ),
        "missing_key": (
            "Your code tried to look up a key that doesn't exist in a dictionary. "
            "Check what keys are actually in the dict."
        ),
        "undefined_variable": (
            "You used a name Python doesn't recognize. Did you define the variable before using it? Check for typos."
        ),
        "runtime_error": (
            "Your code hit an error while running. "
            "Look at the first error line — it tells you the file and line number."
        ),
        "type_error": (
            "Python used incompatible types in this operation. Check the values on the error line: "
            "for example, a number cannot be added directly to text."
        ),
        "timeout": (
            "This test did not finish in time. Check for an infinite loop and make sure each loop moves toward its stopping condition."
        ),
        "boolean_logic": (
            "Your function returned the opposite True/False answer. Check the condition, comparison operator, "
            "and boundary values in your ``if`` statement."
        ),
        "numeric_precision": (
            "The calculation is extremely close, but floating-point rounding changed the last digits. "
            "Check whether the result should be rounded before returning it."
        ),
        "container_value_mismatch": (
            "The dictionary has the expected keys, but one or more values are wrong. "
            "Check how each value is calculated or assigned."
        ),
        "collection_type_mismatch": (
            "The items look right, but the collection type is wrong. Check whether the test expects a list or a tuple."
        ),
        "prints_instead_of_returns": (
            "The test expected a **returned** value, but got printed output. "
            "Hint: ``print()`` shows text to you; ``return`` gives a value back to the caller."
        ),
        "none_returned": _build_none_returned_hint(context, level=1),
        "wrong_type": (
            "The result looks close but has the wrong type. "
            "Are you returning a number when a string is expected, or vice versa?"
        ),
        "extra_spaces": (
            "The text is almost correct, but has extra spaces or missing spaces. Check for leading/trailing whitespace."
        ),
        "case_mismatch": (
            "The letters are right but the capitalization is off. Python treats uppercase and lowercase as different."
        ),
        "list_order_wrong": (
            "Your list has the right items but in the wrong order. Check where you append or insert values."
        ),
        "incorrect_logic": _build_incorrect_logic_hint(context, level=1),
    }

    # ── Level 2: specific pointers ─────────────────────────────────
    _level2 = {
        "syntax_error": (
            "Python could not parse your code. The error message above "
            "includes a line number — look at that line and the one before it. "
            "Common causes: missing ``:`` after ``def``/``if``/``for``, "
            "unclosed parentheses, or mismatched indentation."
        ),
        "function_not_found": _build_function_not_found_hint(context, level=2),
        "import_error": (
            "The module you tried to import doesn't exist or is misspelled. "
            "Check the exact spelling in ``import X`` or ``from X import Y``. "
            "If it's a custom file, make sure it's in the same directory."
        ),
        "wrong_parameter_count": _build_param_count_hint(context, level=2),
        "division_by_zero": (
            "Your code divided by zero. Check any division (``/``, ``//``, ``%``) "
            "and make sure the denominator cannot be zero. "
            "If the denominator can be zero, add an ``if`` check first."
        ),
        "index_out_of_range": (
            "Your code tried to access index that doesn't exist. "
            "Remember: indices start at ``0``, so a list of 3 items "
            "has valid indices ``[0]``, ``[1]``, ``[2]`` — index ``[3]`` is out of range."
        ),
        "missing_key": (
            "The key you looked up doesn't exist in the dictionary. "
            "Use ``dict.get(key, default)`` instead of ``dict[key]`` "
            "for safe access, or check ``if key in dict:`` first."
        ),
        "undefined_variable": (
            "You used a variable before it was defined or made a typo in the name. "
            "Check: did you define this variable above where you use it? "
            "Is it spelled exactly the same each time?"
        ),
        "runtime_error": (
            "Your code crashed during the test. The error message above "
            "shows the exact line and problem type. "
            "Focus on that line and the values being used there."
        ),
        "type_error": (
            "The operation received incompatible Python types. Read the error’s two type names, then convert or "
            "validate the value before using ``+``, ``-``, ``*``, or another operation."
        ),
        "timeout": (
            "The test stopped because your code ran too long. Inspect every ``while``/``for`` loop: initialize the "
            "counter before the loop and update it so the condition eventually becomes false."
        ),
        "boolean_logic": (
            "The result is a boolean, but the condition gives the opposite answer. Trace the input through each "
            "comparison and check whether ``>``, ``>=``, ``<``, ``<=``, ``==``, or ``!=`` matches the requirement."
        ),
        "numeric_precision": (
            "The expected and actual floats differ only by rounding. Use ``round(value, places)`` or compare with "
            "a documented tolerance if the lesson allows it; do not change the main calculation blindly."
        ),
        "container_value_mismatch": (
            "The dictionary keys are correct, but these values do not match. Compare each assignment with the "
            "expected values and check the branch that creates them."
        ),
        "collection_type_mismatch": (
            "The elements match, but the return type does not. Return ``[...]`` for a list or ``(...)`` for a tuple "
            "according to the function specification."
        ),
        "prints_instead_of_returns": (
            "You used ``print()`` where you need ``return``. "
            "``print()`` outputs to the terminal; ``return`` sends a value "
            "back to whoever called the function. "
            "Change ``print(result)`` to ``return result`` in your function."
        ),
        "none_returned": _build_none_returned_hint(context, level=2),
        "wrong_type": (
            'The value is the right "content" but the wrong Python type. '
            "For example, returning ``'42'`` (a string) when the test expects ``42`` (an int). "
            "Use ``int()``, ``float()``, or ``str()`` to convert."
        ),
        "extra_spaces": (
            "Your output has spacing differences. Compare the expected and "
            "actual outputs character by character. "
            "Use ``.strip()`` to remove leading/trailing spaces, or check "
            "for extra spaces inside your strings."
        ),
        "case_mismatch": (
            "The case is wrong — e.g., you returned ``'hello'`` but the test "
            "expects ``'Hello'``. Check your capitalization: "
            "use ``.title()``, ``.upper()``, or ``.lower()`` as needed."
        ),
        "list_order_wrong": (
            "Your list has the correct items but in the wrong order. "
            "Check the order you append elements, or if you need to "
            "sort the list before returning it."
        ),
        "incorrect_logic": _build_incorrect_logic_hint(context, level=2),
    }

    # ── Level 3: near full guidance ────────────────────────────────
    _level3 = {
        "syntax_error": (
            "Python cannot read your code due to a syntax error. The exact line "
            "and character are shown in the error above. Common fixes:\n"
            "  • Add ``:`` at the end of ``def``, ``if``, ``elif``, ``else``, "
            "``for``, ``while`` lines\n"
            "  • Make sure parentheses ``()``, brackets ``[]``, and braces ``{}`` "
            "are properly closed\n"
            "  • Use consistent indentation (4 spaces per level)\n"
            "  • Strings must have matching quote marks ``'...'`` or ``\"...\"``"
        ),
        "function_not_found": _build_function_not_found_hint(context, level=3),
        "import_error": (
            "Python cannot find the module you tried to import. "
            "Make sure the module name is spelled correctly (Python is case-sensitive). "
            "Standard library modules like ``math``, ``random``, ``json`` work "
            "without installing. Custom files must be in the same folder."
        ),
        "wrong_parameter_count": _build_param_count_hint(context, level=3),
        "division_by_zero": (
            "Your code divides by zero. Look at every ``/``, ``//``, and ``%`` operation. "
            "Before dividing, check the denominator is not zero:\n"
            "  ``if denominator != 0:\n"
            "      result = numerator / denominator\n"
            "  else:\n"
            "      result = 0  # or handle appropriately``"
        ),
        "index_out_of_range": (
            "Your code tried to access an element beyond the list/string length. "
            "For a list ``items`` with 3 elements, valid indices are ``0, 1, 2``. "
            "To avoid this, check the length first:\n"
            "  ``if index < len(items):\n"
            "      return items[index]``"
        ),
        "missing_key": (
            "The dictionary key you looked up doesn't exist. Instead of ``d[key]``, "
            "use ``d.get(key)`` (returns ``None`` if missing) or "
            "``d.get(key, default_value)`` (returns ``default_value`` if missing)."
        ),
        "undefined_variable": (
            "Python cannot find a variable or function name you used. "
            "Check for typos in the name — even one character wrong causes this. "
            "Also check that you defined the variable/function BEFORE using it "
            "(Python reads top-to-bottom)."
        ),
        "runtime_error": (
            "Your code crashed while running. The error trace shows exactly "
            "which line failed and what went wrong. Common causes:\n"
            "  • Dividing by zero\n"
            "  • Accessing a list index that doesn't exist\n"
            "  • Calling a method on ``None``\n"
            "Read the error type and message carefully — they tell you the fix."
        ),
        "type_error": (
            "Python stopped because an operation received incompatible types. For example, ``1 + 'a'`` fails. "
            "Inspect the values on the failing line and convert them deliberately (such as ``int(text)``) before "
            "performing the operation."
        ),
        "timeout": (
            "The diagnostic case exceeded the time limit, usually because a loop never reaches its exit condition. "
            "Check the loop variable, the condition, and every ``break`` path. Test with a small input first."
        ),
        "boolean_logic": (
            "The function returned the opposite boolean. Write down the required result for the failing input, then "
            "trace every comparison. Pay special attention to boundary operators: ``>`` excludes the boundary, "
            "while ``>=`` includes it."
        ),
        "numeric_precision": (
            "The calculation differs only in floating-point rounding (for example ``0.30000000000000004`` vs ``0.3``). "
            "Round at the required precision before returning, such as ``return round(total, 2)`` when the lesson asks "
            "for two decimal places."
        ),
        "container_value_mismatch": (
            "The dictionary structure is correct, but one or more values differ. Compare each key’s expected value "
            "with the assignment in your code and trace the input that feeds it."
        ),
        "collection_type_mismatch": (
            "The collection contains the right elements but has the wrong type. Check the specification: use a list "
            "literal ``[...]`` or a tuple literal ``(...)`` as required."
        ),
        "prints_instead_of_returns": (
            "You're using ``print()`` when you should use ``return``. "
            "These are different operations:\n"
            "  • ``print(value)`` → shows value on screen, function returns ``None``\n"
            "  • ``return value`` → sends value back to caller, nothing printed\n"
            "Find the ``print(...)`` in your function and change it to ``return ...``."
        ),
        "none_returned": _build_none_returned_hint(context, level=3),
        "wrong_type": (
            "The value you returned is the right content but wrong Python type. "
            "For example:\n"
            "  • ``'42'`` (str) → needs ``int('42')`` or ``float('42')``\n"
            "  • ``42`` (int) → needs ``str(42)`` if a string is expected\n"
            "Convert the return value to the right type before returning."
        ),
        "extra_spaces": (
            "Your string has extra spaces at the start, end, or inside. "
            "Use ``.strip()`` to remove leading/trailing whitespace. "
            "If there are extra spaces inside, check your string building logic "
            "— maybe you're adding spaces between words incorrectly."
        ),
        "case_mismatch": (
            "The capitalization doesn't match. Python treats ``'Hello'`` and "
            "``'hello'`` as different strings.\n"
            "  • ``.title()`` → ``'hello world'`` → ``'Hello World'``\n"
            "  • ``.upper()`` → ``'hello'`` → ``'HELLO'``\n"
            "  • ``.lower()`` → ``'HELLO'`` → ``'hello'``"
        ),
        "list_order_wrong": (
            "The items in your list match but are in the wrong order. "
            "Check: are you appending items in the right sequence? "
            "Does the test expect a sorted list? If so, use ``sorted(your_list)`` "
            "or ``your_list.sort()`` before returning."
        ),
        "incorrect_logic": _build_incorrect_logic_hint(context, level=3),
    }

    levels = {1: _level1, 2: _level2, 3: _level3}
    hints_for_level = levels.get(level, _level1)
    specific_runtime_diagnoses = {
        "assertion_failed",
        "attribute_error",
        "file_not_found",
        "overflow_error",
        "permission_error",
        "recursion_error",
        "stop_iteration",
        "no_tests_run",
        "test_tampering",
        "unserializable_return",
        "value_error",
        "interactive_input",
        "sandbox_violation",
        "output_limit_exceeded",
    }
    if diagnosis in specific_runtime_diagnoses:
        return _build_runtime_specific_hint(diagnosis, level)
    return hints_for_level.get(diagnosis, hints_for_level["incorrect_logic"])


# ── Context-aware hint builders ─────────────────────────────────────


def _build_none_returned_hint(context: dict, level: int = 1) -> str:
    expected = context.get("expected")
    if expected is not None:
        if level >= 3:
            return (
                f"Your function returned ``None`` instead of ``{expected}``. A function without ``return`` "
                f"automatically returns ``None``. Compute the result and finish the relevant branch with "
                f"``return {expected}`` (or return the variable containing that value)."
            )
        if level >= 2:
            return (
                f"Your function returned ``None`` but the test expected ``{expected}``. Find the path that reaches "
                "the end of the function and return the value computed on that path."
            )
        return f"Your function returned ``None``; the test expected ``{expected}``. Check every path for a ``return``."
    if level >= 3:
        return (
            "Your function returns ``None`` because it lacks a value-bearing ``return`` statement. A function "
            "without ``return``, or with a bare ``return``, automatically returns ``None``. Return the value "
            "you computed, such as ``return result``."
        )
    if level >= 2:
        return "Your function returns ``None`` because a code path reaches the end without returning a value. Add a return for that path."
    return "Your function returned ``None``. Check every path for a ``return`` statement."


def _build_runtime_specific_hint(diagnosis: str, level: int) -> str:
    hints = {
        "assertion_failed": (
            "Your function stopped at an ``assert`` statement. Check the condition and the input that makes it "
            "false; return the required result or handle that case instead of asserting it away."
        ),
        "attribute_error": (
            "Your code accessed an attribute or method on the wrong object. Check the value before the dot and "
            "confirm that it is not ``None`` and has the attribute you intended."
        ),
        "file_not_found": (
            "Your code tried to open a file that is not available to the lesson. Check the filename, relative path, "
            "and spelling; use the provided lesson files instead of assuming a local path."
        ),
        "overflow_error": (
            "The calculation became too large for the operation. Check the loop or exponent that grows the value "
            "and stop, scale, or validate it before it overflows."
        ),
        "permission_error": (
            "Your code tried to access a file or resource without permission. Use the lesson workspace and avoid "
            "writing outside the permitted directory."
        ),
        "recursion_error": (
            "The function called itself too many times. Add a base case and make each recursive call move closer "
            "to that base case."
        ),
        "stop_iteration": (
            "Your code asked an iterator for a next item when no items remained. Check the iterator before calling "
            "``next()`` or use a loop that handles the end of the iterator."
        ),
        "no_tests_run": (
            "The grader could not confirm that any lesson tests executed. Check that your file imports cleanly and "
            "does not skip, exit, or replace the test run."
        ),
        "test_tampering": (
            "The grader detected a change under the protected tests/ folder. Only edit your student code; restore the "
            "lesson tests and run the grader again."
        ),
        "unserializable_return": (
            "The function returned a value Ducky cannot compare safely, such as a set, bytes object, or generator. "
            "Return the plain list, dictionary, string, or number required by the lesson."
        ),
        "value_error": (
            "A built-in function received a value in the wrong format. Check the input before converting or parsing "
            "it; for example, only pass numeric text to ``int()``."
        ),
        "interactive_input": (
            "This automated check cannot answer ``input()`` for you. Keep input prompts inside ``main()`` and "
            "return values from the required functions so the tests can call them safely."
        ),
        "sandbox_violation": (
            "Your code was stopped before it ran because it reached for something this exercise does not allow, "
            "such as importing a system module. Solve it with plain Python: the standard library functions you "
            "need are already available, and the lesson does not need file, network, or process access."
        ),
        "output_limit_exceeded": (
            "Your program printed more output than the grader is willing to capture, so the run was stopped. This "
            "usually means a loop is printing on every pass where you meant to print once, after the loop."
        ),
    }
    hint = hints[diagnosis]
    if level >= 2:
        return hint + " Read the final error line for the exact value and operation that failed."
    return hint


def _build_function_not_found_hint(context: dict, level: int = 1) -> str:
    expected_fn = context.get("expected_fn", "")
    closest = context.get("closest_match", "")
    defined = context.get("defined_functions", [])

    if expected_fn and closest:
        if level >= 2:
            return (
                f"The test calls ``{expected_fn}()`` but your code defines "
                f"``{closest}()`` instead. The names are close but don't match exactly.\n"
                f"  Fix: rename your function from ``{closest}`` to ``{expected_fn}``.\n"
                f"  In your code, change ``def {closest}(`` to ``def {expected_fn}(``."
            )
        return (
            f"The test expects a function named ``{expected_fn}()``, "
            f"but your code defines ``{closest}()`` instead. "
            f"**Rename** ``{closest}`` → ``{expected_fn}`` to fix this."
        )
    elif expected_fn:
        if level >= 3 and defined:
            defined_list = ", ".join(f"``{f}()``" for f in defined)
            return (
                f"The test expects a function named ``{expected_fn}()`` "
                f"that was not found in your code. "
                f"Your code defines: {defined_list}.\n"
                f"Add a new function: ``def {expected_fn}(...):`` with the "
                f"logic the test expects."
            )
        elif level >= 2:
            return (
                f"The test expects a function named ``{expected_fn}()`` "
                f"that is missing from your code. "
                f"Add ``def {expected_fn}(...):`` and implement it."
            )
        return (
            f"The test expects a function named ``{expected_fn}()`` "
            f"that was not found in your code. Check the spelling and "
            f"make sure you defined it correctly."
        )
    return (
        "The test could not find the expected function. Check the "
        "function name and spelling — even one character off will fail."
    )


def _build_incorrect_logic_hint(context: dict, level: int = 1) -> str:
    """Build a contextual hint for incorrect_logic with expected/actual values."""
    expected = context.get("expected")
    actual = context.get("actual")

    if expected is not None and actual is not None:
        if level >= 2:
            return (
                f"The code ran but got ``{actual}`` when the test expected ``{expected}``.\n"
                f"Focus on the function that computes this value — check your calculation, "
                f"your condition boundaries, and the order of operations."
            )
        return (
            f"The code ran but the result differs: the test expected ``{expected}`` but got ``{actual}``. "
            f"Trace the failing input through each line, then compare the value at the return statement with the "
            f"required value."
        )

    if level >= 2:
        return (
            "Your code returned the wrong value. The test expected one thing "
            "but got another. Step through your code with the test input "
            "on paper — what does each line produce? "
            "Check your math, conditions (``if``/``elif``), and loop bounds."
        )

    return (
        "The code ran but produced the wrong answer. "
        "Try picking one test case and tracing through what your function does "
        "step by step — what does each line compute?"
    )



def _build_param_count_hint(context: dict, level: int = 1) -> str:
    # Context is populated from the TypeError text by
    # ``tutor._context_from_request`` (via ``_parse_param_count_error``), which
    # extracts the arities from "takes X positional arguments but Y were given".
    student_params = context.get("student_param_count")
    test_call_args = context.get("test_call_arg_count")

    if student_params is not None and test_call_args is not None:
        diff = student_params - test_call_args
        if level >= 2:
            if diff > 0:
                extra = f"Your function has {diff} extra parameter(s)."
            else:
                extra = f"Your function is missing {-diff} parameter(s)."
            return (
                f"Your function defines {student_params} parameter(s) but the "
                f"test calls it with {test_call_args}. {extra}\n"
                f"  Fix: change your function definition — "
                f"``def my_func(param1, param2):`` — so the number of "
                f"parameters matches what the test provides."
            )
        return (
            f"Your function defines {student_params} parameter(s) "
            f"but the test calls it with {test_call_args}. "
            f"Check the function signature: ``def my_func(param1, param2):``"
        )
    return (
        "Your function has the wrong number of parameters. "
        "Check that the number of parameters in your function definition "
        "matches what the tests expect."
    )
