import logging
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator, model_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import ENV_PATH, ai, paypal
from .errors import GENERIC, UserError, map_exception

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


class SetupIn(BaseModel):
    paypal_client_id: Optional[str] = None
    paypal_client_secret: Optional[str] = None
    anthropic_api_key: Optional[str] = None


# ---------------------------------------------------------------- health + setup

def _status() -> dict:
    pp = bool(os.environ.get("PAYPAL_CLIENT_ID", "").strip() and os.environ.get("PAYPAL_CLIENT_SECRET", "").strip())
    an = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    return {"paypal_ready": pp, "anthropic_ready": an, "ready": pp and an, "sandbox": "sandbox" in paypal.BASE}


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


@app.middleware("http")
async def _only_this_computer(request: Request, call_next):
    """Every /api route answers only to pages served from this computer (localhost)."""
    if request.url.path.startswith("/api") and not _host_origin_ok(request):
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

@app.post("/api/draft")
def draft(t: Text):
    return _wrap(ai.draft_from_text, t.text)


@app.post("/api/send")
def send(inv: InvoiceIn):
    d = inv.model_dump()
    return _wrap(paypal.create_invoice, d)


@app.get("/api/invoices")
def invoices():
    return _wrap(paypal.list_invoices)


@app.post("/api/invoices/{inv_id}/draft-nudge")
def draft_nudge(inv_id: str, req: Optional[NudgeReq] = None):
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


@app.post("/api/invoices/{inv_id}/remind")
def remind(inv_id: str, n: RemindIn):
    def go():
        email = paypal.normalize_invoice(paypal.get_invoice(inv_id))["client_email"] or None
        paypal.remind(inv_id, n.subject, n.note)
        return email
    return {"ok": True, "client_email": _wrap(go, context="remind")}


# ---------------------------------------------------------------- pages

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
