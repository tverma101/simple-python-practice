import unittest
from lesson_01 import add_numbers


class TestAddNumbers(unittest.TestCase):
    def test_adds_two_numbers(self):
        self.assertEqual(add_numbers(2, 3), 5)

    def test_adds_zero(self):
        self.assertEqual(add_numbers(0, 8), 8)

    def test_adds_negative_numbers(self):
        self.assertEqual(add_numbers(-2, -4), -6)


if __name__ == "__main__":
    unittest.main()
