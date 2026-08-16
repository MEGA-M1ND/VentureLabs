import pytest

from ledgertruth import CurrencyMismatch, Money, inr


def test_rejects_float_amounts():
    with pytest.raises(TypeError):
        Money(10.5, "INR")  # type: ignore[arg-type]


def test_rejects_bool_amounts():
    # bool is an int subclass; a True amount is always a bug.
    with pytest.raises(TypeError):
        Money(True, "INR")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "minor"),
    [("1000", 100_000), ("249.50", 24_950), ("0.07", 7), ("0", 0), ("-5.25", -525)],
)
def test_inr_parses_to_exact_minor_units(text, minor):
    assert inr(text).minor == minor


def test_inr_rejects_sub_paise_precision():
    with pytest.raises(ValueError, match="2 minor digits"):
        inr("10.005")


def test_cross_currency_arithmetic_raises():
    with pytest.raises(CurrencyMismatch):
        Money(100, "INR") + Money(100, "USD")


def test_cross_currency_comparison_raises():
    with pytest.raises(CurrencyMismatch):
        _ = Money(100, "INR") < Money(100, "USD")


def test_repeated_addition_stays_exact():
    # The classic float failure: 0.1 summed 10 times != 1.0
    total = Money.zero("INR")
    for _ in range(10):
        total = total + inr("0.10")
    assert total == inr("1.00")


def test_str_renders_minor_units():
    assert str(inr("249.50")) == "249.50 INR"
    assert str(inr("-5.25")) == "-5.25 INR"
