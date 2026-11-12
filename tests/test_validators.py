"""The request models: what the form may send, and the plain-English message when it is wrong."""
import pytest
from pydantic import ValidationError

from app.main import InvoiceIn, ItemIn, RemindIn, Text

GOOD = {"client_name": "Maya Chen", "client_email": "maya@example.com", "currency": "USD", "due_days": 14,
        "items": [{"name": "Logo design", "qty": 1, "unit_price": "120.00"}]}


def make(**over):
    return InvoiceIn(**{**GOOD, **over})


def message(**over) -> str:
    """The first plain-English message the person would see."""
    with pytest.raises(ValidationError) as e:
        make(**over)
    return e.value.errors()[0]["msg"].removeprefix("Value error, ")


# ---------------------------------------------------------------- deposit_percent

def test_deposit_defaults_to_none():
    assert make().deposit_percent is None


@pytest.mark.parametrize("given,expected", [(None, None), ("", None), (10, 10), (50, 50), (90, 90),
                                            ("50", 50), (50.0, 50), ("90", 90)])
def test_deposit_accepts_whole_percentages_from_10_to_90(given, expected):
    assert make(deposit_percent=given).deposit_percent == expected


@pytest.mark.parametrize("bad", [9, 91, 0, 100, -10, 50.5, "abc", True, False, [], {}, "NaN", "inf"])
def test_deposit_rejects_everything_else_in_plain_english(bad):
    assert message(deposit_percent=bad) == "A deposit must be a whole number between 10 and 90 percent."


def test_deposit_must_leave_something_to_pay_later():
    msg = message(items=[{"name": "Tiny", "qty": 1, "unit_price": "0.01"}], deposit_percent=50)
    assert msg == "That total is too small to ask for a deposit."


def test_deposit_on_zero_decimal_currency():
    inv = make(currency="JPY", items=[{"name": "Logo", "qty": 1, "unit_price": "1000"}], deposit_percent=25)
    assert inv.deposit_percent == 25


# ---------------------------------------------------------------- late_fee_note

def test_late_fee_note():
    assert make().late_fee_note is None
    assert make(late_fee_note="   ").late_fee_note is None
    assert make(late_fee_note="  5% after 14 days ").late_fee_note == "5% after 14 days"
    assert make(late_fee_note="x" * 300).late_fee_note == "x" * 300
    assert message(late_fee_note="x" * 301) == "The late fee note is too long. Keep it to 300 characters or fewer."
    assert message(late_fee_note=42) == "Please check the form and try again."


# ---------------------------------------------------------------- the rest of the form

@pytest.mark.parametrize("email", ["", "maya", "maya@", "@example.com", "maya@example", "a b@example.com", None, 5])
def test_bad_email(email):
    assert message(client_email=email) == "That email address doesn't look right."


@pytest.mark.parametrize("name", ["", "   ", None, "x" * 101])
def test_bad_client_name(name):
    assert message(client_name=name) == "Check the client's name."


@pytest.mark.parametrize("price", [0, "0", "0.00", -5, "", None, "abc", "NaN", True])
def test_zero_or_bad_price(price):
    assert message(items=[{"name": "Logo", "qty": 1, "unit_price": price}]) == "Each item needs a price above zero."


@pytest.mark.parametrize("qty", [0, -1, "", None, "abc"])
def test_zero_or_bad_quantity(qty):
    assert message(items=[{"name": "Logo", "qty": qty, "unit_price": 10}]) == "Each item needs a quantity above zero."


def test_other_money_rules():
    assert message(items=[{"name": "Logo", "qty": 1, "unit_price": "10.005"}]) == \
        "Prices can have at most two decimal places."
    assert message(currency="JPY", items=[{"name": "Logo", "qty": 1, "unit_price": "10.5"}]) == \
        "JPY has no cents, use whole numbers."
    assert message(items=[{"name": "Logo", "qty": 1, "unit_price": "1000001"}]) == "That price is too large."
    assert message(items=[]) == "Add at least one item with a price above zero."
    assert message(currency="XYZ") == "That currency isn't supported."
    assert message(due_days=366) == "The due date must be between 0 and 365 days from today."
    assert message(items=[{"name": " ", "qty": 1, "unit_price": 10}]) == "Each item needs a name."


def test_every_form_message_is_plain_english():
    msgs = [message(client_email="x"), message(deposit_percent=5), message(late_fee_note="x" * 400),
            message(due_days="soon"), message(items=[{"name": "a", "qty": 1, "unit_price": 0}])]
    for m in msgs:
        assert m and m[0].isupper() and m.endswith(".")
        assert not any(bad in m.lower() for bad in ("pydantic", "validation", "field", "traceback", "decimal", "int"))


def test_other_models():
    assert Text(text="  hello ").text == "hello"
    with pytest.raises(ValidationError):
        Text(text="   ")
    assert RemindIn(subject=" Hi ", note=" there ").note == "there"
    with pytest.raises(ValidationError):
        RemindIn(subject="", note="x")
    assert ItemIn(name="a", qty="2", unit_price="1.50").unit_price == 1.5
