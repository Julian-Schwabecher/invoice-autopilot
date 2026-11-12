"""Thin PayPal Invoicing v2 client (sandbox by default)."""
import os, time, httpx

BASE = os.getenv("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
_tok = {"v": None, "exp": 0.0}


def _token() -> str:
    if _tok["v"] and time.time() < _tok["exp"] - 30:
        return _tok["v"]
    cid, sec = os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]
    r = httpx.post(f"{BASE}/v1/oauth2/token", auth=(cid, sec),
                   data={"grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    _tok.update(v=j["access_token"], exp=time.time() + j["expires_in"])
    return _tok["v"]


def _call(method: str, path: str, **kw):
    r = httpx.request(method, BASE + path, timeout=30,
                      headers={"Authorization": f"Bearer {_token()}"}, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"PayPal {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def create_invoice(d: dict) -> dict:
    """d: client_name, client_email, currency, due_days, note, items[{name,qty,unit_price}]"""
    body = {
        "detail": {
            "currency_code": d.get("currency", "USD"),
            "note": d.get("note", ""),
            "payment_term": {"term_type": "NET_" + str(d.get("due_days", 14))
                             if d.get("due_days") in (10, 15, 30, 45, 60, 90) else "DUE_ON_RECEIPT"},
        },
        "primary_recipients": [{"billing_info": {
            "name": {"given_name": d["client_name"]},
            "email_address": d["client_email"]}}],
        "items": [{"name": i["name"], "quantity": str(i["qty"]),
                   "unit_amount": {"currency_code": d.get("currency", "USD"),
                                   "value": f'{float(i["unit_price"]):.2f}'}}
                  for i in d["items"]],
    }
    inv = _call("POST", "/v2/invoicing/invoices", json=body)
    inv_id = inv["href"].rsplit("/", 1)[-1]
    _call("POST", f"/v2/invoicing/invoices/{inv_id}/send",
          json={"send_to_recipient": True, "send_to_invoicer": False})
    return get_invoice(inv_id)


def get_invoice(inv_id: str) -> dict:
    return _call("GET", f"/v2/invoicing/invoices/{inv_id}")


def list_invoices() -> list[dict]:
    r = _call("POST", "/v2/invoicing/search-invoices?page=1&page_size=25&total_required=true",
              json={})
    return r.get("items", [])


def remind(inv_id: str, subject: str, note: str):
    _call("POST", f"/v2/invoicing/invoices/{inv_id}/remind",
          json={"subject": subject, "note": note, "send_to_invoicer": False})
