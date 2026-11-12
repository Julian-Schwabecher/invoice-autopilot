"""One place that turns any failure into a plain-English message for the person using the app."""
import logging

import anthropic
import httpx

log = logging.getLogger("invoice_autopilot")

NOT_CONNECTED = "This app isn't connected yet. Click Change my keys at the bottom of the page to add your keys."
PAYPAL_KEYS = "PayPal didn't accept the keys. Click Change my keys at the bottom of the page and paste them again."
PAYPAL_BUSY = "PayPal is busy. Wait a minute and try again."
PAYPAL_DOWN = "Couldn't reach PayPal. Check your internet and try again."
PAYPAL_REJECT = "PayPal wouldn't accept this invoice. Check the email address and amounts."
PAYPAL_EMAIL = "The email address doesn't look right."
CLAUDE_KEY = "The Claude key isn't working. Click Change my keys at the bottom of the page and paste a new one."
CLAUDE_BUSY = "The AI is busy right now. Try again in a moment."
CLAUDE_CREDIT = ("Your Claude account has no credit left. Add a few dollars at console.anthropic.com "
                 "(Plans & Billing), then try again.")
CLAUDE_MODEL = ("The Claude model this app asks for isn't available to your account. "
                "Remove ANTHROPIC_MODEL from the .env file, or set it to a model you can use.")
CLAUDE_REJECT = "Claude couldn't handle that request. Try a shorter or simpler note."
CLAUDE_DOWN = "Couldn't reach Claude. Check your internet and try again."
NO_INVOICE = "I couldn't find an invoice in that note. Try adding who it's for, what you did and the price."
GENERIC = "Something went wrong on our side. Nothing was sent. Please try again."
SANDBOX_ONLY = "That only works in PayPal's practice (sandbox) mode, so it was not done."
BAD_INVOICE_ID = "That invoice link doesn't look right."
REMIND_LIMIT = ("To stay within PayPal's daily reminder limit, this app sends at most two reminders for the same "
                "invoice in one day, and you've already sent two today. Try again tomorrow.")


class UserError(Exception):
    """An error whose message is already safe and friendly to show."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.message = message
        self.status = status


class PayPalError(Exception):
    """PayPal answered with an error. Technical detail stays here and goes to the log only."""

    def __init__(self, status: int, body=None, token: bool = False):
        self.status = status
        self.body = body if body is not None else {}
        self.token = token  # True when the failure was while getting an access token
        name = self.body.get("name") if isinstance(self.body, dict) else None
        super().__init__(f"PayPal HTTP {status} {name or ''}".strip())

    def fields(self) -> str:
        out = []
        if isinstance(self.body, dict):
            for d in self.body.get("details") or []:
                if isinstance(d, dict):
                    out.append(str(d.get("field", "")))
        return " ".join(out).lower()


def map_exception(e: Exception, context: str | None = None) -> tuple[int, str]:
    """Return (http status, friendly message). Logs the raw exception server-side."""
    if isinstance(e, UserError):
        return e.status, e.message
    if isinstance(e, PayPalError):
        log.error("PayPal error: %s body=%s", e, str(e.body)[:600])
        s = e.status
        if e.token and s < 500 and s != 429:
            return 502, PAYPAL_KEYS
        if s == 401:
            return 502, PAYPAL_KEYS
        if s == 429:
            return 429, PAYPAL_BUSY
        if s >= 500:
            return 502, PAYPAL_DOWN
        if s == 404:
            return 422, "PayPal couldn't find that invoice."
        if s in (400, 422):
            if context == "remind":
                return 422, ("PayPal wouldn't send that reminder. The invoice may already be paid or cancelled, "
                             "or you may have already sent two reminders for it today (PayPal documents a limit of two a day).")
            if context == "record_payment":
                return 422, ("PayPal wouldn't record that practice payment. "
                             "The invoice may already be paid, cancelled or not sent yet.")
            if context == "qr":
                return 422, "PayPal couldn't make a QR code for that invoice. It may not have been sent yet."
            return 422, PAYPAL_EMAIL if "email" in e.fields() else PAYPAL_REJECT
        if s == 403:
            return 502, "PayPal didn't allow that. Check that Invoicing is turned on for your PayPal app."
        return 502, PAYPAL_DOWN
    if isinstance(e, httpx.TransportError):
        log.error("Network error talking to PayPal: %s", type(e).__name__)
        return 502, PAYPAL_DOWN
    if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        log.error("Claude key rejected: %s", type(e).__name__)  # message may echo part of the key
        return 502, CLAUDE_KEY
    if isinstance(e, anthropic.APIConnectionError):  # includes timeouts
        log.error("Network error talking to Claude: %s", type(e).__name__)
        return 502, CLAUDE_DOWN
    if isinstance(e, anthropic.APIStatusError):
        log.error("Claude API error: HTTP %s %s", e.status_code, str(e)[:300])
        s = e.status_code
        if s == 402 or (s == 400 and "credit balance" in str(e).lower()):
            return 502, CLAUDE_CREDIT
        if s == 404:
            return 502, CLAUDE_MODEL
        if s == 429 or s == 529 or s >= 500:
            return 502, CLAUDE_BUSY
        if 400 <= s < 500:
            return 502, CLAUDE_REJECT
        return 502, CLAUDE_BUSY
    if isinstance(e, anthropic.APIError):
        log.exception("Claude API error")
        return 502, CLAUDE_BUSY
    if isinstance(e, StopIteration):
        log.exception("No tool_use block in Claude reply")
        return 422, NO_INVOICE
    if isinstance(e, KeyError) and e.args and str(e.args[0]) in (
            "PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "ANTHROPIC_API_KEY"):
        return 503, NOT_CONNECTED
    log.exception("Unexpected error")
    return 500, GENERIC
