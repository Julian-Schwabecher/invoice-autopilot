"""Every failure becomes a plain-English message; technical detail never reaches the person."""
import anthropic
import httpx
import pytest

from app import errors
from app.errors import PayPalError, UserError, map_exception


def pp(status, body=None, token=False):
    return PayPalError(status, body, token=token)


@pytest.mark.parametrize("exc,status,message", [
    (pp(401), 502, errors.PAYPAL_KEYS),
    (pp(400, token=True), 502, errors.PAYPAL_KEYS),
    (pp(429), 429, errors.PAYPAL_BUSY),
    (pp(429, token=True), 429, errors.PAYPAL_BUSY),
    (pp(500), 502, errors.PAYPAL_DOWN),
    (pp(503, token=True), 502, errors.PAYPAL_DOWN),
    (pp(404), 422, "PayPal couldn't find that invoice."),
    (pp(422), 422, errors.PAYPAL_REJECT),
    (pp(422, {"details": [{"field": "/primary_recipients/0/billing_info/email_address"}]}), 422, errors.PAYPAL_EMAIL),
    (pp(403), 502, "PayPal didn't allow that. Check that Invoicing is turned on for your PayPal app."),
    (pp(418), 502, errors.PAYPAL_DOWN),
    (httpx.ConnectError("boom"), 502, errors.PAYPAL_DOWN),
    (httpx.ReadTimeout("slow"), 502, errors.PAYPAL_DOWN),
    (UserError("Already friendly.", 418), 418, "Already friendly."),
    (KeyError("PAYPAL_CLIENT_ID"), 503, errors.NOT_CONNECTED),
    (KeyError("ANTHROPIC_API_KEY"), 503, errors.NOT_CONNECTED),
    (RuntimeError("secret internals"), 500, errors.GENERIC),
    (KeyError("href"), 500, errors.GENERIC),
])
def test_map_exception(exc, status, message):
    assert map_exception(exc) == (status, message)


def test_context_messages():
    assert map_exception(pp(422), "remind")[1].startswith("PayPal wouldn't send that reminder.")
    assert "two reminders" in map_exception(pp(400), "remind")[1]
    assert map_exception(pp(422), "record_payment") == (
        422, "PayPal wouldn't record that practice payment. The invoice may already be paid, cancelled or not sent yet.")
    assert map_exception(pp(400), "qr") == (
        422, "PayPal couldn't make a QR code for that invoice. It may not have been sent yet.")
    # a missing invoice reads the same whatever we were doing
    assert map_exception(pp(404), "qr")[1] == "PayPal couldn't find that invoice."
    # and a busy or broken PayPal is never blamed on the invoice
    assert map_exception(pp(500), "record_payment")[1] == errors.PAYPAL_DOWN


def test_the_reminder_limit_is_described_as_this_apps_rule_not_as_something_paypal_enforces():
    # PayPal's documentation says two a day, but its sandbox accepted more in our tests, so we must not claim
    # that PayPal itself refused the third one.
    assert "this app sends at most two reminders" in errors.REMIND_LIMIT
    assert "PayPal only lets you" not in errors.REMIND_LIMIT
    assert "PayPal documents a limit" in map_exception(pp(400), "remind")[1]


def test_start_bat_uses_the_windows_null_device():
    from pathlib import Path
    bat = (Path(__file__).resolve().parent.parent / "start.bat").read_bytes()
    assert b"/dev/" not in bat                       # cmd.exe has no /dev/null: the redirect fails and `where` never runs
    assert bat.count(b"\r\n") == bat.count(b"\n")    # and the file keeps its Windows line endings


def test_claude_errors():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    assert map_exception(anthropic.APIConnectionError(request=req)) == (502, errors.CLAUDE_DOWN)
    assert map_exception(anthropic.APITimeoutError(request=req)) == (502, errors.CLAUDE_DOWN)

    def status_error(cls, code):
        return cls("x", response=httpx.Response(code, request=req), body=None)
    assert map_exception(status_error(anthropic.AuthenticationError, 401)) == (502, errors.CLAUDE_KEY)
    assert map_exception(status_error(anthropic.RateLimitError, 429)) == (502, errors.CLAUDE_BUSY)
    assert map_exception(status_error(anthropic.NotFoundError, 404)) == (502, errors.CLAUDE_MODEL)
    assert map_exception(status_error(anthropic.BadRequestError, 400)) == (502, errors.CLAUDE_REJECT)
    assert map_exception(status_error(anthropic.InternalServerError, 500)) == (502, errors.CLAUDE_BUSY)


ALL_MESSAGES = [v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str)]


@pytest.mark.parametrize("text", ALL_MESSAGES)
def test_every_message_is_plain_english(text):
    assert text[0].isupper() and text.rstrip().endswith((".", "?"))
    for jargon in ("HTTP", "Traceback", "Exception", "null", "JSON", "API error", "status code", "{", "}"):
        assert jargon not in text


def test_paypal_error_keeps_detail_out_of_the_message():
    e = PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"field": "/detail/x"}]})
    assert "UNPROCESSABLE_ENTITY" in str(e)           # for the log
    assert map_exception(e)[1] == errors.PAYPAL_REJECT   # not for the person
