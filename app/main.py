import datetime as dt
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import ai, paypal

app = FastAPI(title="Invoice Autopilot")


class Text(BaseModel):
    text: str


class Nudge(BaseModel):
    subject: str
    note: str


def _wrap(fn, *a):
    try:
        return fn(*a)
    except KeyError as e:
        raise HTTPException(500, f"Missing env var or field: {e}")
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/draft")
def draft(t: Text):
    return _wrap(ai.draft_from_text, t.text)


@app.post("/api/send")
def send(d: dict):
    return _wrap(paypal.create_invoice, d)


@app.get("/api/invoices")
def invoices():
    return _wrap(paypal.list_invoices)


@app.post("/api/invoices/{inv_id}/draft-nudge")
def draft_nudge(inv_id: str):
    def go():
        inv = paypal.get_invoice(inv_id)
        due = inv["detail"].get("payment_term", {}).get("due_date") or inv["detail"]["invoice_date"]
        days = max(0, (dt.date.today() - dt.date.fromisoformat(due)).days)
        slim = {"number": inv["detail"].get("invoice_number"), "status": inv.get("status"),
                "due": due, "amount": inv.get("amount"), "to": inv["primary_recipients"][0]["billing_info"]["name"]}
        return {**ai.write_nudge(slim, days), "days_overdue": days}
    return _wrap(go)


@app.post("/api/invoices/{inv_id}/remind")
def remind(inv_id: str, n: Nudge):
    _wrap(paypal.remind, inv_id, n.subject, n.note)
    return {"ok": True}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")
