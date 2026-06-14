"""Tests for Boolean activation expression evaluator."""

from aigineering.core.activation import check_activation


def test_single_name_present():
    assert check_activation("data_file", {"data_file"}) is True


def test_single_name_absent():
    assert check_activation("data_file", {"other"}) is False


def test_and_both_present():
    assert check_activation("a AND b", {"a", "b"}) is True


def test_and_one_missing():
    assert check_activation("a AND b", {"a"}) is False


def test_or_either():
    assert check_activation("a OR b", {"a"}) is True
    assert check_activation("a OR b", {"b"}) is True
    assert check_activation("a OR b", set()) is False


def test_not():
    assert check_activation("NOT a", set()) is True
    assert check_activation("NOT a", {"a"}) is False


def test_parentheses():
    assert check_activation("a AND (b OR c)", {"a", "c"}) is True
    assert check_activation("a AND (b OR c)", {"a"}) is False


def test_empty_expression():
    assert check_activation("", {"a"}) is True
    assert check_activation(None, {"a"}) is True


def test_complex_expression():
    expr = "data_file AND citation_db"
    assert check_activation(expr, {"data_file", "citation_db"}) is True
    assert check_activation(expr, {"data_file"}) is False


def test_not_with_parentheses():
    assert check_activation("NOT (a AND b)", {"a"}) is True
    assert check_activation("NOT (a AND b)", {"a", "b"}) is False


def test_invalid_syntax_raises():
    import pytest

    with pytest.raises(ValueError):
        check_activation("a AND", {"a"})


def test_deeply_nested_raises():
    import pytest

    deep = "(" * 60 + "a" + ")" * 60
    with pytest.raises((ValueError, RecursionError)):
        check_activation(deep, {"a"})


def test_long_expression_raises():
    import pytest

    long_expr = " AND ".join(["a"] * 300)
    with pytest.raises((ValueError)):
        check_activation(long_expr, {"a"})
