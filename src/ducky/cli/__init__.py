"""Ducky CLI — modular command-line interface."""

from .main import main
from .student_commands import main_student
from .teacher_commands import main_teacher

__all__ = ["main", "main_student", "main_teacher"]
