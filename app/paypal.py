"""Thin PayPal Invoicing v2 client (sandbox by default)."""
import base64
import binascii
import datetime as dt
import logging
import os
import re
import threading
import time
from decimal import Decimal, ROUND_HALF_UP

import httpx

from .errors import NOT_CONNECTED, SANDBOX_ONLY, PayPalError, UserError

log = logging.getLogger("invoice_autopilot")

BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
_tok = {"v": None, "exp": 0.0}

ZERO_DECIMAL = {"JPY", "HUF", "TWD"}
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$",
           "INR": "₹", "CNY": "CN¥", "PHP": "₱", "ILS": "₪"}


# ---------------------------------------------------------------- low level

def _creds() -> tuple[str, str]:
    cid = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    sec = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        raise UserError(NOT_CONNECTED, 503)
    return cid, sec


def reset_token() -> None:
    _tok.update(v=None, exp=0.0)


def _token_request(cid: str, sec: str) -> httpx.Response:
    return httpx.post(f"{BASE}/v1/oauth2/token", auth=(cid, sec),
                      data={"grant_type": "client_credentials"}, timeout=20)


def _token() -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 30:
        return _tok["v"]
    cid, sec = _creds()
    r = _token_request(cid, sec)
    if r.status_code >= 400:
        raise PayPalError(r.status_code, _body(r), token=True)
    j = r.json()
    _tok.update(v=j["access_token"], exp=time.time() + float(j.get("expires_in", 300)))
    return _tok["v"]


def check_credentials(cid: str, sec: str) -> bool:
    """True if PayPal accepts this Client ID and Secret, False if it rejects them.
    Network trouble and PayPal outages raise, so the caller can say so."""
    r = _token_request(cid, sec)
    if r.status_code == 200 and "access_token" in (_body(r) or {}):
        return True
    if r.status_code in (400, 401, 403):
        return False
    raise PayPalError(r.status_code, _body(r), token=True)


def _body(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"text": r.text[:400]}


def _call(method: str, path: str, *, headers: dict | None = None, **kw):
    h = {"Authorization": f"Bearer {_token()}"}
    h.update(headers or {})
    r = httpx.request(method, BASE + path, timeout=30, headers=h, **kw)
    if r.status_code >= 400:
        raise PayPalError(r.status_code, _body(r))
    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError:  # not JSON (the QR code comes back as a multipart body)
        return {"_raw": r.content, "_content_type": r.headers.get("content-type", "")}


def is_sandbox() -> bool:
    return "sandbox" in BASE


# ---------------------------------------------------------------- money helpers

def _dec(v, default: Decimal | None = Decimal("0")) -> Decimal | None:
    try:
        return Decimal(str(v))
    except Exception:
        return default


def money_str(v, currency: str) -> str:
    """Decimal -> '600.00' (or '600' for currencies with no cents)."""
    d = _dec(v) or Decimal("0")
    q = Decimal("1") if currency in ZERO_DECIMAL else Decimal("0.01")
    return format(d.quantize(q, rounding=ROUND_HALF_UP), "f")


def fmt_money(v, currency: str) -> str:
    """Friendly '$600.00' / 'CAD 600.00' for labels and emails."""
    s = money_str(v, currency)
    sym = SYMBOLS.get(currency)
    return f"{sym}{s}" if sym else f"{currency} {s}"


def _qty_str(q) -> str:
    d = _dec(q) or Decimal("0")
    s = format(d.quantize(Decimal("0.0001")).normalize(), "f")
    return s


def split_name(full: str) -> dict:
    parts = (full or "").split()
    if len(parts) <= 1:
        return {"given_name": parts[0] if parts else ""}
    return {"given_name": parts[0], "surname": " ".join(parts[1:])}


def invoice_total(d: dict) -> Decimal:
    cur = d.get("currency", "USD")
    q = Decimal("1") if cur in ZERO_DECIMAL else Decimal("0.01")
    return sum(((_dec(i["qty"]) * _dec(i["unit_price"])).quantize(q, rounding=ROUND_HALF_UP)
                for i in d["items"]), Decimal("0"))


def deposit_amount(total: Decimal, percent: int, cur: str) -> Decimal:
    """The smallest first payment when a client may pay a deposit: percent of the total, rounded to the
    currency's smallest unit (whole units for currencies with no cents)."""
    q = Decimal("1") if cur in ZERO_DECIMAL else Decimal("0.01")
    return (total * Decimal(int(percent)) / Decimal(100)).quantize(q, rounding=ROUND_HALF_UP)


def deposit_due(d: dict) -> str | None:
    pct = d.get("deposit_percent")
    if not pct:
        return None
    cur = d.get("currency") or "USD"
    return money_str(deposit_amount(invoice_total(d), pct, cur), cur)


# ---------------------------------------------------------------- create / send

_lock = threading.Lock()
_results: dict[str, dict] = {}   # request_id -> first successful response
_attempts: dict[str, int] = {}   # request_id -> failed attempts (so a retry gets a fresh PayPal key)


def create_invoice(d: dict) -> dict:
    """d: client_name, client_email, currency, due_days, note, items[{name,qty,unit_price}], request_id.
    Creates, sends, and returns the flat success shape. Nothing is left behind if sending fails."""
    rid = d.get("request_id")
    if not rid:
        return _create(d, None)
    with _lock:  # one at a time, so a double click can't create twice
        if rid in _results:
            return _results[rid]
        res = _create(d, f"{rid}-{_attempts.get(rid, 0)}")
        _results[rid] = res
        if len(_results) > 500:
            _results.pop(next(iter(_results)))
        return res


def build_invoice_body(d: dict, today: dt.date | None = None) -> tuple[dict, str | None]:
    """The exact PayPal 'create invoice' payload for d, and the due date (None = due on receipt). Pure."""
    cur = d.get("currency") or "USD"
    due_days = d.get("due_days") or 0
    today = today or dt.date.today()
    due_date = (today + dt.timedelta(days=int(due_days))).isoformat() if due_days > 0 else None
    term = ({"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due_date} if due_date
            else {"term_type": "DUE_ON_RECEIPT"})
    detail = {"currency_code": cur, "note": d.get("note") or "", "payment_term": term}
    late_fee_note = (d.get("late_fee_note") or "").strip()
    if late_fee_note:  # PayPal never adds late fees by itself; this only states the terms on the invoice
        detail["terms_and_conditions"] = late_fee_note
    body = {
        "detail": detail,
        "primary_recipients": [{"billing_info": {
            "name": split_name(d["client_name"]), "email_address": d["client_email"]}}],
        "items": [{"name": i["name"], "quantity": _qty_str(i["qty"]),
                   "unit_amount": {"currency_code": cur, "value": money_str(i["unit_price"], cur)}}
                  for i in d["items"]],
    }
    dep = deposit_due(d)
    if dep is not None:
        body["configuration"] = {"partial_payment": {
            "allow_partial_payment": True,
            "minimum_amount_due": {"currency_code": cur, "value": dep}}}
    return body, due_date


SEND_BODY = {"send_to_recipient": True, "send_to_invoicer": False}


def _create(d: dict, paypal_key: str | None) -> dict:
    cur = d.get("currency") or "USD"
    body, due_date = build_invoice_body(d)
    headers = {"PayPal-Request-Id": paypal_key} if paypal_key else None
    inv = _call("POST", "/v2/invoicing/invoices", json=body, headers=headers)
    inv_id = str(inv["href"]).rstrip("/").rsplit("/", 1)[-1]
    try:
        _call("POST", f"/v2/invoicing/invoices/{inv_id}/send", json=dict(SEND_BODY))
    except Exception:
        if d.get("request_id"):
            _attempts[d["request_id"]] = _attempts.get(d["request_id"], 0) + 1
        try:
            delete_invoice(inv_id)  # don't leave an orphan draft behind
        except Exception:
            log.exception("Could not delete orphan draft %s", inv_id)
        raise
    result = {"ok": True, "invoice_id": inv_id, "number": None, "status": "SENT",
              "client_name": d["client_name"], "client_email": d["client_email"], "currency": cur,
              "total": money_str(invoice_total(d), cur), "due_date": due_date, "view_url": None,
              "deposit_due": deposit_due(d)}
    try:
        got = get_invoice(inv_id)
        n = normalize_invoice(got)
        result.update(number=n["number"], status=got.get("status") or "SENT", total=n["total"] or result["total"],
                      due_date=n["due_date"] or due_date, view_url=n["view_url"])
        if n["client_name"]:
            result["client_name"] = n["client_name"]
    except Exception:
        log.exception("Invoice %s was sent but reading it back failed", inv_id)
    return result


def get_invoice(inv_id: str) -> dict:
    return _call("GET", f"/v2/invoicing/invoices/{inv_id}")


def delete_invoice(inv_id: str) -> None:
    _call("DELETE", f"/v2/invoicing/invoices/{inv_id}")


# ---------------------------------------------------------------- list / normalize

def _g(o, *path, default=None):
    """Defensive nested get: never raises on missing keys or wrong types."""
    for p in path:
        if isinstance(o, dict):
            o = o.get(p)
        elif isinstance(o, list) and isinstance(p, int) and -len(o) <= p < len(o):
            o = o[p]
        else:
            return default
        if o is None:
            return default
    return o


def recipient_name(inv: dict) -> str:
    name = _g(inv, "primary_recipients", 0, "billing_info", "name", default={})
    if isinstance(name, str):
        return name.strip()
    if not isinstance(name, dict):
        return ""
    full = str(name.get("full_name") or "").strip()
    return full or " ".join(str(x) for x in (name.get("given_name"), name.get("surname")) if x).strip()


def _date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _view_url(inv: dict) -> str | None:
    """PayPal's customer link. The search endpoint leaves it out, so list rows get null; a single
    GET (send result) has it. When it is missing, build it from the invoice id, which PayPal does the
    same way: INV2-AAAA-BBBB-CCCC-DDDD -> <site>/invoice/p/#AAAABBBBCCCCDDDD. Drafts have no customer page."""
    url = _g(inv, "detail", "metadata", "recipient_view_url")
    if url:
        return str(url)
    m = re.fullmatch(r"INV2-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})", str(inv.get("id") or ""))
    if m and inv.get("status") not in (None, "", "DRAFT"):
        site = "https://www.sandbox.paypal.com" if "sandbox" in BASE else "https://www.paypal.com"
        return f"{site}/invoice/p/#{''.join(m.groups())}"
    return None


def normalize_invoice(inv: dict, today: dt.date | None = None) -> dict:
    """One PayPal invoice -> the flat shape the UI shows. Pure: no network, never raises on missing keys."""
    today = today or dt.date.today()
    status = str(inv.get("status") or "")
    cur = str(_g(inv, "amount", "currency_code") or _g(inv, "detail", "currency_code") or "USD")
    total = money_str(_g(inv, "amount", "value", default="0"), cur)
    due_amt = _g(inv, "due_amount", "value")
    amount_due = money_str(due_amt, cur) if due_amt is not None else total
    due = _date(_g(inv, "detail", "payment_term", "due_date"))
    days = (due - today).days if due else None
    created = _date(_g(inv, "detail", "metadata", "create_time")) or _date(_g(inv, "detail", "invoice_date"))
    late = days is not None and days < 0
    dep = _g(inv, "configuration", "partial_payment", "minimum_amount_due", "value")
    deposit = (money_str(dep, cur) if dep is not None
               and _g(inv, "configuration", "partial_payment", "allow_partial_payment") is True else None)

    def overdue_label():
        n = -days
        return f"Overdue by {n} day{'' if n == 1 else 's'}"

    if status == "DRAFT":
        label, kind = "Not sent yet", "draft"
    elif status == "SCHEDULED":
        label, kind = "Scheduled to send", "draft"
    elif status in ("SENT", "UNPAID"):
        label, kind = (overdue_label(), "overdue") if late else ("Waiting for payment", "waiting")
    elif status == "PARTIALLY_PAID":
        if late:
            label, kind = overdue_label(), "overdue"
        else:
            label, kind = f"Partly paid, {fmt_money(amount_due, cur)} still owed", "partial"
    elif status == "PAYMENT_PENDING":
        label, kind = "Payment on its way", "waiting"
    elif status in ("PAID", "MARKED_AS_PAID"):
        label, kind = "Paid", "paid"
    elif status == "CANCELLED":
        label, kind = "Cancelled", "cancelled"
    elif status in ("REFUNDED", "PARTIALLY_REFUNDED", "MARKED_AS_REFUNDED"):
        label, kind = "Refunded", "refunded"
    else:
        label, kind = "Needs attention", "other"

    return {
        "id": str(inv.get("id") or ""),
        "number": _g(inv, "detail", "invoice_number"),
        "client_name": recipient_name(inv),
        "client_email": str(_g(inv, "primary_recipients", 0, "billing_info", "email_address", default="")),
        "currency": cur, "total": total, "amount_due": amount_due,
        "created": created.isoformat() if created else None,
        "due_date": due.isoformat() if due else None,
        "days_to_due": days, "status": status, "label": label, "kind": kind,
        "deposit_due": deposit,
        "can_remind": status in ("SENT", "UNPAID", "PARTIALLY_PAID"),
        "view_url": _view_url(inv),
    }


_KIND_GROUP = {"overdue": 0, "waiting": 1, "partial": 1, "draft": 2, "other": 2, "cancelled": 2,
               "refunded": 2, "paid": 3}


def sort_invoices(rows: list[dict]) -> list[dict]:
    """Overdue first (most overdue first), then waiting/partial by due date, then others, then paid."""
    def key(r):
        g = _KIND_GROUP.get(r["kind"], 2)
        if g == 0:
            return (0, r["days_to_due"] if r["days_to_due"] is not None else 0, "")
        if g == 1:
            return (1, 0, r["due_date"] or "9999-99-99")
        # newest first within the last two groups
        return (g, 0, "".join(chr(255 - ord(c)) for c in (r["created"] or "")))
    return sorted(rows, key=key)


def list_invoices() -> dict:
    r = _call("POST", "/v2/invoicing/search-invoices?page=1&page_size=25&total_required=true", json={})
    items = r.get("items") if isinstance(r, dict) else None
    rows = sort_invoices([normalize_invoice(i) for i in (items or []) if isinstance(i, dict)])
    total = r.get("total_items") if isinstance(r, dict) else None
    return {"invoices": rows, "shown": len(rows), "total": total if isinstance(total, int) else len(rows)}


def days_overdue(row: dict) -> int:
    """Whole days past the due date for a normalized row (0 when not late or when there is no due date)."""
    d = row.get("days_to_due")
    return max(0, -d) if isinstance(d, int) else 0


def awaiting_payment(rows: list[dict]) -> list[dict]:
    """The rows where a reminder makes sense (sent, not paid, not cancelled, not a draft, and something still
    owed), most overdue first, then soonest due. Pure."""
    keep = [r for r in rows if r.get("can_remind") and r.get("kind") in ("overdue", "waiting", "partial")
            and (_dec(r.get("amount_due")) or Decimal("0")) > 0]
    return sorted(keep, key=lambda r: (-days_overdue(r), r.get("due_date") or "9999-12-31"))


def remind_body(subject: str, note: str) -> dict:
    return {"subject": subject, "note": note, "send_to_invoicer": False}


def remind(inv_id: str, subject: str, note: str):
    _call("POST", f"/v2/invoicing/invoices/{inv_id}/remind", json=remind_body(subject, note))


# ---------------------------------------------------------------- practice payment (sandbox only)

_NOT_PAYABLE = {
    "PAID": "That invoice is already paid, so there is nothing left to mark as paid.",
    "MARKED_AS_PAID": "That invoice is already paid, so there is nothing left to mark as paid.",
    "DRAFT": "That invoice hasn't been sent yet, so it can't be marked as paid.",
    "SCHEDULED": "That invoice hasn't been sent yet, so it can't be marked as paid.",
    "CANCELLED": "That invoice was cancelled, so it can't be marked as paid.",
}


def _payment_date(inv: dict) -> str:
    """Today, as early as any time zone has it, so PayPal never sees a date in the future. Never before
    the invoice's own date."""
    day = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)).date()
    inv_day = _date(_g(inv, "detail", "invoice_date"))
    return max(day, inv_day).isoformat() if inv_day else day.isoformat()


def plan_payment(inv_id: str) -> tuple[dict, dict]:
    """Practice only: what 'mark as paid' would send, as (normalized invoice, PayPal payments body): the whole
    amount still owed, as cash. Reads the invoice but changes nothing. Refuses outside PayPal's sandbox, and when
    the invoice cannot be paid or has nothing left to pay."""
    if not is_sandbox():
        raise UserError(SANDBOX_ONLY, 403)
    got = get_invoice(inv_id)
    n = normalize_invoice(got)
    if n["status"] in _NOT_PAYABLE:
        raise UserError(_NOT_PAYABLE[n["status"]], 422)
    if n["status"] not in ("SENT", "UNPAID", "PARTIALLY_PAID"):
        raise UserError("PayPal doesn't let you mark that invoice as paid right now.", 422)
    owed = _dec(n["amount_due"]) or Decimal("0")
    if owed <= 0:
        raise UserError(_NOT_PAYABLE["PAID"], 422)
    cur = n["currency"]
    body = {"method": "CASH", "payment_date": _payment_date(got),
            "amount": {"currency_code": cur, "value": money_str(owed, cur)}, "note": "Marked paid for demo"}
    return n, body


def record_payment(inv_id: str) -> dict:
    """Practice only: mark the whole amount still owed as paid (cash), as if the client had paid."""
    n, body = plan_payment(inv_id)
    cur, value = n["currency"], body["amount"]["value"]
    pay = _call("POST", f"/v2/invoicing/invoices/{inv_id}/payments", json=body)
    result = {"ok": True, "invoice_id": inv_id, "payment_id": (pay or {}).get("payment_id"),
              "amount_paid": value, "currency": cur, "status": "MARKED_AS_PAID", "invoice": None}
    try:
        after = get_invoice(inv_id)
        result["status"] = after.get("status") or "MARKED_AS_PAID"
        result["invoice"] = normalize_invoice(after)
    except Exception:
        log.exception("Payment on %s was recorded but reading the invoice back failed", inv_id)
    return result


# ---------------------------------------------------------------- QR code

_PNG = b"\x89PNG\r\n\x1a\n"


def _png_from(blob: bytes) -> bytes | None:
    """PNG bytes from raw PNG, base64 text, or a data: URL. None if it is none of those."""
    blob = blob.strip()
    if blob.startswith(_PNG):
        return blob
    text = blob.decode("ascii", "ignore")
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        return None
    return data if data.startswith(_PNG) else None


def qr_image(resp) -> str | None:
    """Turn whatever PayPal's QR endpoint returned into a 'data:image/png;base64,...' string, or None.
    Live sandbox answers with a multipart body whose 'image' part is base64 text; plain PNG bytes, a bare
    base64 string and a JSON field are accepted too."""
    raw, ctype = None, ""
    if isinstance(resp, dict) and isinstance(resp.get("_raw"), (bytes, bytearray)):
        raw, ctype = bytes(resp["_raw"]), str(resp.get("_content_type") or "")
    elif isinstance(resp, (bytes, bytearray)):
        raw = bytes(resp)
    elif isinstance(resp, str):
        raw = resp.encode("utf-8", "ignore")
    elif isinstance(resp, dict):
        for k in ("image", "qr_code", "qr"):
            if isinstance(resp.get(k), str):
                raw = resp[k].encode("utf-8", "ignore")
                break
    if not raw:
        return None
    candidates = [raw]
    m = re.search(r'boundary="?([^";\s]+)"?', ctype)
    if m or raw.startswith(b"--"):
        boundary = (m.group(1).encode() if m else raw.split(b"\r\n", 1)[0][2:])
        candidates = []
        for part in raw.split(b"--" + boundary):
            head, sep, body = part.partition(b"\r\n\r\n")
            if sep and body.strip(b"\r\n- "):
                candidates.append(body.rstrip(b"\r\n"))
    for c in candidates:
        png = _png_from(c)
        if png:
            return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    return None


def generate_qr(inv_id: str) -> str:
    """A 'data:image/png;base64,...' QR code that opens the invoice's payment page. Needs a sent invoice."""
    resp = _call("POST", f"/v2/invoicing/invoices/{inv_id}/generate-qr-code", json={"width": 300, "height": 300})
    image = qr_image(resp)
    if not image:
        log.error("QR response for %s was not a PNG", inv_id)
        raise UserError("PayPal sent back a QR code we couldn't read. Please try again.", 502)
    return image


# ---------------------------------------------------------------- webhooks

def verify_webhook(headers, event: dict, webhook_id: str) -> bool:
    """Ask PayPal whether this notification really came from PayPal. headers: the request headers."""
    h = {str(k).lower(): v for k, v in headers.items()}
    body = {"auth_algo": h.get("paypal-auth-algo"), "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id, "webhook_event": event}
    r = _call("POST", "/v1/notifications/verify-webhook-signature", json=body)
    return isinstance(r, dict) and r.get("verification_status") == "SUCCESS"
