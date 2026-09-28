from solution import is_positive


def test_positive():
    assert is_positive(5) is True


def test_negative():
    assert is_positive(-3) is False


def test_zero():
    assert is_positive(0) is False
