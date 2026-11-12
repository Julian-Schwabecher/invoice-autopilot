"""Shared test helpers: a fake PayPal and a builder for PayPal-shaped invoices."""
import base64
import copy
import datetime as dt

PNG = b"\x89PNG\r\n\x1a\n" + b"not-really-pixels"
PNG_B64 = base64.b64encode(PNG).decode()


def day(offset: int) -> str:
    """ISO date `offset` days from today (negative = in the past)."""
    return (dt.date.today() + dt.timedelta(days=offset)).isoformat()


def make_invoice(id="INV2-AAAA-BBBB-CCCC-DDDD", status="SENT", total="600.00", due_amount="same", cur="USD",
                 due=None, number="0001", name=("Maya", "Chen"), email="maya@example.com", **extra) -> dict:
    """An invoice shaped like PayPal's answer. due: ISO date, or None for 'due on receipt'."""
    inv = {
        "id": id, "status": status,
        "detail": {"invoice_number": number, "currency_code": cur, "invoice_date": day(-30),
                   "metadata": {"create_time": day(-30) + "T10:00:00Z"}},
        "primary_recipients": [{"billing_info": {
            "name": {"given_name": name[0], "surname": name[1]}, "email_address": email}}],
        "amount": {"currency_code": cur, "value": total},
    }
    if due:
        inv["detail"]["payment_term"] = {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due}
    if status != "DRAFT":
        inv["due_amount"] = {"currency_code": cur, "value": total if due_amount == "same" else due_amount}
    inv.update(extra)
    return inv


class FakePayPal:
    """Stands in for paypal._call. A route answers a (method, path); a path ending in * matches by prefix.
    The answer can be a dict, an Exception (raised), a callable(call) or a list (one answer per call, the last
    one repeating)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.routes: list[tuple] = []

    def route(self, method: str, path: str, response):
        self.routes.append((method, path, response))
        return self

    def __call__(self, method, path, *, headers=None, **kw):
        call = {"method": method, "path": path, "json": copy.deepcopy(kw.get("json")), "headers": headers}
        self.calls.append(call)
        for m, p, r in self.routes:
            if m == method and (path == p or (p.endswith("*") and path.startswith(p[:-1]))):
                if isinstance(r, list):
                    r = r.pop(0) if len(r) > 1 else r[0]
                if callable(r):
                    r = r(call)
                if isinstance(r, Exception):
                    raise r
                return copy.deepcopy(r)
        raise AssertionError(f"Unexpected PayPal call: {method} {path}")

    def writes(self) -> list[dict]:
        """Calls that would change something at PayPal (everything but reads, search and QR codes)."""
        return [c for c in self.calls if c["method"] != "GET"
                and not c["path"].startswith("/v2/invoicing/search-invoices")
                and not c["path"].endswith("/generate-qr-code")]

    def to(self, method: str, suffix: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method and c["path"].endswith(suffix)]
