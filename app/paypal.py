"""Thin PayPal Invoicing v2 client (sandbox by default)."""
import datetime as dt
import logging
import os
import re
import threading
import time
from decimal import Decimal, ROUND_HALF_UP

import httpx

from .errors import NOT_CONNECTED, PayPalError, UserError

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
    return r.json() if r.content else {}


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


def _create(d: dict, paypal_key: str | None) -> dict:
    cur = d.get("currency") or "USD"
    due_days = d.get("due_days") or 0
    today = dt.date.today()
    due_date = (today + dt.timedelta(days=int(due_days))).isoformat() if due_days > 0 else None
    term = ({"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due_date} if due_date
            else {"term_type": "DUE_ON_RECEIPT"})
    body = {
        "detail": {"currency_code": cur, "note": d.get("note") or "", "payment_term": term},
        "primary_recipients": [{"billing_info": {
            "name": split_name(d["client_name"]), "email_address": d["client_email"]}}],
        "items": [{"name": i["name"], "quantity": _qty_str(i["qty"]),
                   "unit_amount": {"currency_code": cur, "value": money_str(i["unit_price"], cur)}}
                  for i in d["items"]],
    }
    headers = {"PayPal-Request-Id": paypal_key} if paypal_key else None
    inv = _call("POST", "/v2/invoicing/invoices", json=body, headers=headers)
    inv_id = str(inv["href"]).rstrip("/").rsplit("/", 1)[-1]
    try:
        _call("POST", f"/v2/invoicing/invoices/{inv_id}/send",
              json={"send_to_recipient": True, "send_to_invoicer": False})
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
              "total": money_str(invoice_total(d), cur), "due_date": due_date, "view_url": None}
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


def remind(inv_id: str, subject: str, note: str):
    _call("POST", f"/v2/invoicing/invoices/{inv_id}/remind",
          json={"subject": subject, "note": note, "send_to_invoicer": False})
