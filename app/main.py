import json
import logging
import os
import re
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import ENV_PATH, ai, live, paypal
from .errors import BAD_INVOICE_ID, GENERIC, REMIND_LIMIT, SANDBOX_ONLY, PayPalError, UserError, map_exception

log = logging.getLogger("invoice_autopilot")
STATIC = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Invoice Autopilot")

CURRENCIES = {"USD", "EUR", "GBP", "CAD", "AUD", "NZD", "JPY", "CHF", "SEK", "NOK", "DKK", "SGD", "HKD", "MXN",
              "BRL", "INR", "PLN", "CZK", "HUF", "ILS", "PHP", "THB", "TWD", "MYR", "CNY"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_TOTAL = Decimal("1000000")


# ---------------------------------------------------------------- errors: always {"message": ...}

def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"message": message}, status_code=status)


@app.exception_handler(UserError)
async def _user_error(request: Request, exc: UserError):
    return _err(exc.status, exc.message)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    msgs = {404: "That page doesn't exist.", 405: "That isn't allowed here.", 403: "That isn't allowed here."}
    status = exc.status_code if exc.status_code in (400, 401, 403, 404, 405, 422, 429, 500, 502, 503) else 500
    return _err(status, msgs.get(exc.status_code, GENERIC))


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    msg = "Please check the form and try again."
    for e in exc.errors():
        if e.get("type") == "value_error":  # our own plain-English validators
            msg = str(e.get("msg", msg)).removeprefix("Value error, ")
            break
    return _err(422, msg)


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    log.error("Unhandled error on %s", request.url.path, exc_info=exc)
    return _err(500, GENERIC)


def _wrap(fn, *a, context: str | None = None):
    try:
        return fn(*a)
    except UserError:
        raise
    except Exception as e:
        status, message = map_exception(e, context)
        raise UserError(message, status) from None


# ---------------------------------------------------------------- request models

def _to_decimal(v, bad: str) -> Decimal:
    if isinstance(v, bool) or v is None or v == "":
        raise ValueError(bad)
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(bad) from None
    if not d.is_finite():
        raise ValueError(bad)
    return d


class Text(BaseModel):
    text: str

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, v):
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            raise ValueError("Type or paste a note first.")
        if len(v) > 4000:
            raise ValueError("That note is too long. Try a shorter one.")
        return v


class ItemIn(BaseModel):
    name: str
    qty: Decimal
    unit_price: Decimal

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            raise ValueError("Each item needs a name.")
        if len(v) > 200:
            raise ValueError("An item name is too long.")
        return v

    @field_validator("qty", mode="before")
    @classmethod
    def _qty(cls, v):
        d = _to_decimal(v, "Each item needs a quantity above zero.")
        if d <= 0:
            raise ValueError("Each item needs a quantity above zero.")
        if d > 100000:
            raise ValueError("That quantity is too large.")
        if d != d.quantize(Decimal("0.0001")):
            raise ValueError("Quantities can have at most 4 decimal places.")
        return d

    @field_validator("unit_price", mode="before")
    @classmethod
    def _price(cls, v):
        d = _to_decimal(v, "Each item needs a price above zero.")
        if d <= 0:
            raise ValueError("Each item needs a price above zero.")
        if d > 1000000:
            raise ValueError("That price is too large.")
        return d


class InvoiceIn(BaseModel):
    client_name: str
    client_email: str
    currency: str = "USD"
    due_days: Optional[int] = None
    note: str = ""
    items: list[ItemIn]
    request_id: Optional[str] = None
    deposit_percent: Optional[int] = None
    late_fee_note: Optional[str] = None

    @field_validator("client_name", mode="before")
    @classmethod
    def _name(cls, v):
        v = v.strip() if isinstance(v, str) else ""
        if not v or len(v) > 100:
            raise ValueError("Check the client's name.")
        return v

    @field_validator("client_email", mode="before")
    @classmethod
    def _email(cls, v):
        v = v.strip() if isinstance(v, str) else ""
        if not EMAIL_RE.match(v) or len(v) > 254:
            raise ValueError("That email address doesn't look right.")
        return v

    @field_validator("currency", mode="before")
    @classmethod
    def _cur(cls, v):
        v = v.strip().upper() if isinstance(v, str) else ""
        if v not in CURRENCIES:
            raise ValueError("That currency isn't supported.")
        return v

    @field_validator("due_days", mode="before")
    @classmethod
    def _due(cls, v):
        if v is None or v == "":
            return None
        try:
            if isinstance(v, bool) or float(v) != int(float(v)):
                raise ValueError
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("The due date must be between 0 and 365 days from today.") from None
        if not 0 <= n <= 365:
            raise ValueError("The due date must be between 0 and 365 days from today.")
        return n

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, v):
        v = v.strip() if isinstance(v, str) else ""
        if len(v) > 1000:
            raise ValueError("The message to your client is too long.")
        return v

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, v):
        if not isinstance(v, list) or not v:
            raise ValueError("Add at least one item with a price above zero.")
        if len(v) > 20:
            raise ValueError("That's too many items. Keep it to 20 or fewer.")
        return v

    @field_validator("request_id", mode="before")
    @classmethod
    def _rid(cls, v):
        if v is None or v == "":
            return None
        if not isinstance(v, str) or len(v) > 64:
            raise ValueError("Please check the form and try again.")
        return v

    @field_validator("deposit_percent", mode="before")
    @classmethod
    def _deposit(cls, v):
        bad = "A deposit must be a whole number between 10 and 90 percent."
        if v is None or v == "":
            return None
        try:
            if isinstance(v, bool) or float(v) != int(float(v)):
                raise ValueError
            n = int(float(v))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(bad) from None
        if not 10 <= n <= 90:
            raise ValueError(bad)
        return n

    @field_validator("late_fee_note", mode="before")
    @classmethod
    def _late_fee(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError("Please check the form and try again.")
        v = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", v).strip()
        if len(v) > 300:
            raise ValueError("The late fee note is too long. Keep it to 300 characters or fewer.")
        return v or None

    @model_validator(mode="after")
    def _money(self):
        if self.currency in paypal.ZERO_DECIMAL:
            for i in self.items:
                if i.unit_price != i.unit_price.to_integral_value():
                    raise ValueError(f"{self.currency} has no cents, use whole numbers.")
        else:
            for i in self.items:
                if i.unit_price != i.unit_price.quantize(Decimal("0.01")):
                    raise ValueError("Prices can have at most two decimal places.")
        total = paypal.invoice_total({"currency": self.currency, "items": [i.model_dump() for i in self.items]})
        if total > MAX_TOTAL:
            raise ValueError("The total is too large.")
        if total <= 0:
            raise ValueError("Add at least one item with a price above zero.")
        if self.deposit_percent:
            dep = paypal.deposit_amount(total, self.deposit_percent, self.currency)
            if dep <= 0 or dep >= total:
                raise ValueError("That total is too small to ask for a deposit.")
        return self


class NudgeReq(BaseModel):
    tone: Optional[Literal["friendly", "neutral", "firm"]] = None
    sender_name: Optional[str] = None

    @field_validator("sender_name", mode="before")
    @classmethod
    def _sender(cls, v):
        if v is None:
            return None
        v = v.strip() if isinstance(v, str) else ""
        if len(v) > 80:
            raise ValueError("That name is too long.")
        return v or None


class RemindIn(BaseModel):
    subject: str
    note: str

    @field_validator("subject", "note", mode="before")
    @classmethod
    def _text(cls, v, info):
        v = v.strip() if isinstance(v, str) else ""
        if not v:
            raise ValueError("Write a subject and a message first.")
        if len(v) > (200 if info.field_name == "subject" else 1000):
            raise ValueError("That reminder is too long. Shorten it and try again.")
        return v


class SafeModeIn(BaseModel):
    enabled: StrictBool


class SetupIn(BaseModel):
    paypal_client_id: Optional[str] = None
    paypal_client_secret: Optional[str] = None
    anthropic_api_key: Optional[str] = None


# ---------------------------------------------------------------- health + setup

def _status() -> dict:
    pp = bool(os.environ.get("PAYPAL_CLIENT_ID", "").strip() and os.environ.get("PAYPAL_CLIENT_SECRET", "").strip())
    an = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return {"paypal_ready": pp, "anthropic_ready": an, "ready": pp and an, "sandbox": paypal.is_sandbox(),
            "safe_mode": live.safe_mode(),
            "webhooks_ready": bool(os.environ.get("PAYPAL_WEBHOOK_ID", "").strip())}


@app.get("/api/health")
def health():
    return _status()


_LOCAL_NAMES = {"localhost", "127.0.0.1", "::1"}


def _host_origin_ok(request: Request) -> bool:
    """True when the Host header (and Origin, if sent) name this computer. Blocks DNS-rebinding pages."""
    try:
        hdr = urlsplit("//" + request.headers.get("host", "")).hostname or ""
        origin = request.headers.get("origin")
        if origin is not None and (urlsplit(origin).hostname or "") not in _LOCAL_NAMES:
            return False
        return hdr in _LOCAL_NAMES
    except ValueError:
        return False


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"} and _host_origin_ok(request)


WEBHOOK_PATH = "/api/webhooks/paypal"


@app.middleware("http")
async def _only_this_computer(request: Request, call_next):
    """Every /api route answers only to pages served from this computer (localhost), except PayPal's webhook
    address. PayPal calls that one from the internet, so it is protected by PayPal's signature check instead,
    and nothing else is exempt."""
    path = request.url.path
    if path.startswith("/api") and path != WEBHOOK_PATH and not _host_origin_ok(request):
        return _err(403, "That isn't allowed here.")
    return await call_next(request)


def _clean_key(v) -> str:
    return (v or "").strip().strip("\"'").strip() if isinstance(v, str) else ""


def _write_env(updates: dict) -> None:
    try:
        lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        lines = []
    seen = set()
    for n, line in enumerate(lines):
        k = line.strip().removeprefix("export ").partition("=")[0].strip()
        if k in updates and "=" in line and not line.strip().startswith("#"):
            lines[n] = f"{k}={updates[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    tmp = ENV_PATH.with_name(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ENV_PATH)
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


@app.post("/api/setup")
def setup(request: Request, s: SetupIn):
    if not _is_local(request):
        raise UserError("Setup can only be changed from the computer running this app.", 403)
    cid, sec = _clean_key(s.paypal_client_id), _clean_key(s.paypal_client_secret)
    key = _clean_key(s.anthropic_api_key)
    if not (cid or sec or key):
        raise UserError("Paste at least one key first.", 422)

    def go():
        updates = {}
        if cid or sec:
            c = cid or os.environ.get("PAYPAL_CLIENT_ID", "").strip()
            sc = sec or os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
            if not (c and sc and paypal.check_credentials(c, sc)):
                raise UserError("PayPal didn't accept that Client ID and Secret. "
                                "Make sure you copied both from the Sandbox tab.", 422)
            updates["PAYPAL_CLIENT_ID"], updates["PAYPAL_CLIENT_SECRET"] = c, sc
        if key:
            if not ai.check_key(key):
                raise UserError("Claude didn't accept that key.", 422)
            updates["ANTHROPIC_API_KEY"] = key
        _write_env(updates)
        os.environ.update(updates)
        ai.reset_client()
        paypal.reset_token()
        return {"ok": True, **_status()}
    return _wrap(go)


# ---------------------------------------------------------------- invoices

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_id(inv_id: str) -> str:
    """An invoice id goes into a PayPal address, so allow only the characters real ids have."""
    if not _ID_RE.match(inv_id):
        raise UserError(BAD_INVOICE_ID, 422)
    return inv_id


InvoiceId = Annotated[str, Depends(_check_id)]


@app.post("/api/draft")
def draft(t: Text):
    return _wrap(ai.draft_from_text, t.text)


def _preview_send(d: dict) -> dict:
    """What /api/send would do, said in plain English with the exact PayPal payload. Calls nothing."""
    body, due_date = paypal.build_invoice_body(d)
    cur = d["currency"]
    total = paypal.money_str(paypal.invoice_total(d), cur)
    dep = paypal.deposit_due(d)
    says = [f"PayPal would email an invoice for {paypal.fmt_money(total, cur)} to {d['client_name']} "
            f"at {d['client_email']}, " + (f"due on {ai.pretty_date(due_date)}." if due_date else "due on receipt.")]
    if dep:
        says.append(f"The client could pay a {d['deposit_percent']}% deposit of {paypal.fmt_money(dep, cur)} first.")
    if d.get("late_fee_note"):
        says.append(f'The invoice would state this under its terms: "{d["late_fee_note"]}"')
    return {"ok": True, "preview": True, "kind": "send",
            "message": "Safe mode is on, so nothing was sent. " + " ".join(says),
            "payload": {"create_invoice": body, "send_invoice": dict(paypal.SEND_BODY)},
            "invoice_id": None, "number": None, "status": "PREVIEW", "client_name": d["client_name"],
            "client_email": d["client_email"], "currency": cur, "total": total, "due_date": due_date,
            "view_url": None, "deposit_due": dep}


def _send_one(inv: InvoiceIn) -> dict:
    d = inv.model_dump()
    if live.safe_mode():
        return _preview_send(d)
    res = _wrap(paypal.create_invoice, d)
    live.log_send("send", res.get("invoice_id"), res.get("number"), res.get("client_name"))
    return res


@app.post("/api/send")
def send(inv: InvoiceIn):
    return _send_one(inv)


@app.get("/api/invoices")
def invoices():
    return _wrap(paypal.list_invoices)


@app.get("/api/invoices/{inv_id}/qr")
def invoice_qr(inv_id: InvoiceId):
    return {"image": _wrap(paypal.generate_qr, inv_id, context="qr")}


def _preview_payment(inv_id: str) -> dict:
    """What 'mark as paid' would do, in plain English with the exact PayPal payload. Reads the invoice, writes nothing."""
    n, body = paypal.plan_payment(inv_id)
    who = n["client_name"] or "your client"
    which = f"invoice {n['number']}" if n["number"] else "the invoice"
    amount = paypal.fmt_money(body["amount"]["value"], n["currency"])
    return {"ok": True, "preview": True, "kind": "payment", "invoice_id": inv_id, "client_name": n["client_name"],
            "message": (f"Safe mode is on, so nothing was sent. PayPal would record a practice payment of {amount} "
                        f"from {who} on {which}, and PayPal would show it as paid."),
            "payload": body}


@app.post("/api/invoices/{inv_id}/record-test-payment")
def record_test_payment(inv_id: InvoiceId):
    """Practice only: pretend the client paid, so the page can show the status change. In safe mode it only
    shows what it would do."""
    if live.safe_mode():
        return _wrap(_preview_payment, inv_id, context="record_payment")
    return _wrap(paypal.record_payment, inv_id, context="record_payment")


@app.post("/api/invoices/{inv_id}/draft-nudge")
def draft_nudge(inv_id: InvoiceId, req: Optional[NudgeReq] = None):
    req = req or NudgeReq()

    def go():
        raw = paypal.get_invoice(inv_id)
        n = paypal.normalize_invoice(raw)
        due = n["due_date"]
        days_to_due = n["days_to_due"]
        days_overdue = max(0, -days_to_due) if days_to_due is not None else 0
        is_overdue = days_overdue > 0
        tone = req.tone or ai.default_tone(days_overdue)
        facts = {"number": n["number"], "due": ai.pretty_date(due) if due else None, "amount": paypal.fmt_money(n["amount_due"], n["currency"]),
                 "to": n["client_name"]}
        out = ai.write_nudge(facts, days_overdue, days_to_due, tone, req.sender_name, is_overdue)
        return {"subject": out["subject"], "note": out["note"], "days_overdue": days_overdue,
                "days_to_due": days_to_due, "is_overdue": is_overdue, "due_date": due,
                "client_name": n["client_name"], "client_email": n["client_email"], "tone": out["tone"]}
    return _wrap(go)


@app.get("/api/nudges")
def nudges(refresh: bool = False):
    """Every invoice that is waiting for payment, ranked by Claude (1 = chase first), each with a drafted
    reminder. Nothing is sent: the person still approves each one, and sending goes through /remind."""
    def go():
        rows = paypal.awaiting_payment(paypal.list_invoices()["invoices"])
        items = []
        for i, r in enumerate(rows, 1):
            late, cur = paypal.days_overdue(r), r["currency"]
            items.append({"ref": str(i), "number": r["number"], "client_name": r["client_name"], "currency": cur,
                          "amount_raw": r["amount_due"], "amount_text": paypal.fmt_money(r["amount_due"], cur),
                          "total_text": paypal.fmt_money(r["total"], cur), "due_date": r["due_date"],
                          "days_overdue": late, "days_to_due": r["days_to_due"], "tone": ai.nudge_tone(late)})
        drafts = ai.write_nudges(items, refresh=refresh)
        out = []
        for r, it, dr in zip(rows, items, drafts):
            out.append({"invoice_id": r["id"], "number": r["number"], "client_name": r["client_name"],
                        "total": r["total"], "amount_due": r["amount_due"], "currency": r["currency"],
                        "days_overdue": it["days_overdue"], "tone": it["tone"], "priority": dr["priority"],
                        "why": dr["why"], "subject": dr["subject"], "note": dr["note"],
                        "drafted_by": dr["drafted_by"],
                        "already_reminded_today": live.reminders_today(r["id"]) > 0})
        out.sort(key=lambda n: n["priority"])
        return out
    return {"nudges": _wrap(go), "safe_mode": live.safe_mode()}


@app.post("/api/invoices/{inv_id}/remind")
def remind(inv_id: InvoiceId, n: RemindIn):
    def go():
        inv = paypal.normalize_invoice(paypal.get_invoice(inv_id))
        if not inv["can_remind"]:
            raise UserError("That invoice is already paid, cancelled or not sent yet, "
                            "so there is nothing to remind anyone about.", 422)
        if live.reminders_today(inv_id) >= 2:
            raise UserError(REMIND_LIMIT, 422)
        email = inv["client_email"] or None
        if live.safe_mode():
            who = inv["client_name"] or "your client"
            return {"ok": True, "preview": True, "kind": "remind", "invoice_id": inv_id,
                    "client_name": inv["client_name"], "client_email": email,
                    "subject": n.subject, "note": n.note,
                    "message": (f"Safe mode is on, so nothing was sent. PayPal would email {who}"
                                f"{' at ' + email if email else ''} a reminder titled \"{n.subject}\"."),
                    "payload": paypal.remind_body(n.subject, n.note)}
        paypal.remind(inv_id, n.subject, n.note)
        live.log_send("remind", inv_id, inv["number"], inv["client_name"])
        return {"ok": True, "client_email": email}
    return _wrap(go, context="remind")


# ---------------------------------------------------------------- demo data, safe mode, log, live events

DEMO_EMAIL = "sb-wwhgs53882719@personal.example.com"
_demo_lock = threading.Lock()
_demo: dict = {"result": None}


@app.post("/api/demo/seed")
def demo_seed():
    """Practice only: send one clearly-labelled demo invoice to the sandbox buyer. The first real result is
    kept, so pressing it again (or two people pressing it) never makes a second invoice."""
    if not paypal.is_sandbox():
        raise UserError(SANDBOX_ONLY, 403)
    with _demo_lock:
        if _demo["result"] is not None:
            return _demo["result"]
        res = _send_one(InvoiceIn(
            client_name="Demo Client", client_email=DEMO_EMAIL, currency="USD", due_days=14,
            note="This is a demo invoice from Invoice Autopilot. It is practice only, no real money.",
            items=[{"name": "Demo: logo design", "qty": 1, "unit_price": 120}], request_id="demo-seed"))
        if not res.get("preview"):
            _demo["result"] = res
        return res


@app.post("/api/safe-mode")
def set_safe_mode(request: Request, s: SafeModeIn):
    if not _is_local(request):
        raise UserError("Safe mode can only be changed from the computer running this app.", 403)
    return {"ok": True, "safe_mode": live.set_safe_mode(s.enabled)}


@app.get("/api/log")
def send_log():
    return {"entries": live.get_log()}


@app.get("/api/events")
def events(since: Annotated[int, Query(ge=0)] = 0):
    found, last = live.events_since(since)
    return {"events": found, "last_seq": last}


def _event_invoice_id(event: dict) -> str | None:
    res = event.get("resource")
    res = res if isinstance(res, dict) else {}
    inner = res.get("invoice") if isinstance(res.get("invoice"), dict) else {}
    found = inner.get("id") or res.get("id")
    return found if isinstance(found, str) and _ID_RE.match(found) else None


_WEBHOOK_HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                    "paypal-transmission-sig", "paypal-transmission-time")
WEBHOOK_MAX_BYTES = 256_000


async def _read_limited(request: Request, limit: int) -> bytes | None:
    """The request body, or None when it is bigger than `limit`. The size is checked BEFORE anything is read
    (Content-Length) and again while reading (a chunked body has no Content-Length), so a huge upload is
    dropped after `limit` bytes instead of being held in memory first."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


@app.post(WEBHOOK_PATH)
async def paypal_webhook(request: Request):
    """PayPal tells us an invoice changed. This address is open to the internet, so nothing is believed until
    PayPal itself confirms the signature; unverified calls are rejected and never stored."""
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "").strip()
    if not webhook_id:
        return _err(503, "Webhooks aren't set up")
    raw = await _read_limited(request, WEBHOOK_MAX_BYTES)
    if raw is None:   # too big: answer without reading the rest, and close the connection so it is not kept
        return JSONResponse({"message": "That notification is too big."}, status_code=413,
                            headers={"Connection": "close"})
    try:
        event = json.loads(raw)
    except ValueError:
        event = None
    if not isinstance(event, dict) or not all(request.headers.get(h) for h in _WEBHOOK_HEADERS):
        return _err(400, "That doesn't look like a PayPal notification.")
    if not live.allow_verify():
        return _err(429, "Too many notifications right now. Please try again in a minute.")
    try:
        verified = await run_in_threadpool(paypal.verify_webhook, request.headers, event, webhook_id)
    except PayPalError as e:
        log.warning("PayPal could not check a webhook signature: HTTP %s", e.status)
        if e.status in (400, 422):
            return _err(400, "That notification could not be verified.")
        return _err(503, "Couldn't check that notification right now. Please try again.")
    except Exception as e:
        log.warning("Webhook check failed: %s", type(e).__name__)
        return _err(503, "Couldn't check that notification right now. Please try again.")
    if not verified:
        return _err(400, "That notification could not be verified.")
    etype = str(event.get("event_type") or "")
    if not etype.startswith("INVOICING.INVOICE."):
        return {"ok": True, "stored": False}
    when = event.get("create_time")
    stored = live.add_event(event.get("id") if isinstance(event.get("id"), str) else None, etype,
                            _event_invoice_id(event), when if isinstance(when, str) else None)
    return {"ok": True, "stored": stored is not None}


# ---------------------------------------------------------------- pages

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
