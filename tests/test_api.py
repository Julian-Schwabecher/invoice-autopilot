"""The running app through FastAPI's TestClient: the localhost guard, safe mode, the send log, webhooks."""
import json

import pytest

from app import live, paypal
from app.errors import PayPalError
from helpers import PNG_B64, day, make_invoice

ID = "INV2-AAAA-BBBB-CCCC-DDDD"
INV_PATH = f"/v2/invoicing/invoices/{ID}"
EVIL = {"host": "evil.example.com"}

SEND = {"client_name": "Maya Chen", "client_email": "maya@example.com", "currency": "USD", "due_days": 14,
        "note": "Thanks!", "items": [{"name": "Logo design", "qty": 1, "unit_price": "120.00"}]}


# ---------------------------------------------------------------- the localhost guard

def test_health_works_from_localhost(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    j = r.json()
    assert j["safe_mode"] is False and j["webhooks_ready"] is False and j["sandbox"] is True and j["ready"] is True


def test_hostile_host_is_blocked_on_health(client):
    r = client.get("/api/health", headers=EVIL)
    assert r.status_code == 403 and r.json() == {"message": "That isn't allowed here."}


def test_hostile_origin_is_blocked(client):
    r = client.get("/api/health", headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert client.get("/api/health", headers={"origin": "http://localhost:8460"}).status_code == 200


PROTECTED = [
    ("GET", "/api/health"), ("GET", "/api/invoices"), ("GET", "/api/nudges"), ("GET", "/api/log"),
    ("GET", "/api/events"), (f"GET", f"/api/invoices/{ID}/qr"),
    ("POST", "/api/setup"), ("POST", "/api/draft"), ("POST", "/api/send"), ("POST", "/api/safe-mode"),
    ("POST", "/api/demo/seed"), ("POST", f"/api/invoices/{ID}/remind"), ("POST", f"/api/invoices/{ID}/draft-nudge"),
    ("POST", f"/api/invoices/{ID}/record-test-payment"),
    # only the exact webhook address is exempt, nothing near it
    ("POST", "/api/webhooks/paypal/"), ("POST", "/api/webhooks/paypal/extra"), ("POST", "/api/webhooks"),
    ("GET", "/api/webhooks/paypal/"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_every_api_route_except_the_webhook_blocks_a_hostile_host(client, no_pp, method, path):
    r = client.request(method, path, headers=EVIL, json={})
    assert r.status_code == 403 and r.json() == {"message": "That isn't allowed here."}
    assert no_pp.calls == []


def test_the_webhook_address_is_exempt_from_the_host_check_but_says_not_set_up(client, no_pp):
    r = client.post("/api/webhooks/paypal", headers=EVIL, json={"event_type": "INVOICING.INVOICE.PAID"})
    assert r.status_code == 503 and r.json() == {"message": "Webhooks aren't set up"}
    assert no_pp.calls == []
    assert live.events_since(0) == ([], 0)


def test_the_webhook_is_still_not_reachable_with_a_different_method(client):
    assert client.get("/api/webhooks/paypal", headers=EVIL).status_code == 405


def test_safe_mode_can_only_be_changed_from_this_computer(client, remote_client):
    assert remote_client.post("/api/safe-mode", json={"enabled": True}).status_code == 403
    assert live.safe_mode() is False
    assert client.post("/api/safe-mode", json={"enabled": True}).json() == {"ok": True, "safe_mode": True}
    assert client.get("/api/health").json()["safe_mode"] is True
    assert client.post("/api/safe-mode", json={"enabled": False}).json() == {"ok": True, "safe_mode": False}
    assert client.get("/api/health").json()["safe_mode"] is False
    assert client.post("/api/safe-mode", json={"enabled": "yes"}).status_code == 422
    assert client.post("/api/safe-mode", json={}).status_code == 422


def test_an_invoice_id_with_odd_characters_is_refused_before_paypal_is_asked(client, no_pp):
    for bad in ("a%3Fb%3Dc", "a%20b", "x" * 80):
        r = client.post(f"/api/invoices/{bad}/record-test-payment")
        assert r.status_code == 422 and r.json() == {"message": "That invoice link doesn't look right."}
    assert no_pp.calls == []


# ---------------------------------------------------------------- safe mode: previews, no PayPal writes

def test_safe_mode_send_is_a_preview_and_calls_paypal_not_at_all(client, no_pp):
    live.set_safe_mode(True)
    r = client.post("/api/send", json={**SEND, "deposit_percent": 50, "late_fee_note": "5% after 14 days"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["preview"] is True and j["kind"] == "send" and j["status"] == "PREVIEW"
    assert no_pp.calls == []                                           # not a single PayPal call
    assert "Safe mode is on, so nothing was sent." in j["message"]
    assert "$120.00" in j["message"] and "maya@example.com" in j["message"] and "$60.00" in j["message"]
    create = j["payload"]["create_invoice"]
    assert create["configuration"]["partial_payment"]["minimum_amount_due"]["value"] == "60.00"
    assert create["detail"]["terms_and_conditions"] == "5% after 14 days"
    assert create["items"] == [{"name": "Logo design", "quantity": "1",
                                "unit_amount": {"currency_code": "USD", "value": "120.00"}}]
    assert j["payload"]["send_invoice"] == {"send_to_recipient": True, "send_to_invoicer": False}
    assert (j["invoice_id"], j["number"], j["view_url"]) == (None, None, None)
    assert j["total"] == "120.00" and j["deposit_due"] == "60.00" and j["currency"] == "USD"
    assert client.get("/api/log").json() == {"entries": []}            # nothing really went out


def test_safe_mode_does_not_change_the_checks_on_the_form(client, no_pp):
    live.set_safe_mode(True)
    r = client.post("/api/send", json={**SEND, "client_email": "nope"})
    assert r.status_code == 422 and r.json() == {"message": "That email address doesn't look right."}


def test_safe_mode_remind_is_a_preview_and_sends_nothing(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(due=day(-3)))
    live.set_safe_mode(True)
    r = client.post(f"/api/invoices/{ID}/remind", json={"subject": "Reminder", "note": "Please pay."})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["preview"] and j["kind"] == "remind" and j["client_email"] == "maya@example.com"
    assert j["payload"] == {"subject": "Reminder", "note": "Please pay.", "send_to_invoicer": False}
    assert "nothing was sent" in j["message"] and "Maya Chen" in j["message"]
    assert fake_pp.writes() == []                                      # only the read of the invoice
    assert client.get("/api/log").json()["entries"] == []


def test_without_safe_mode_the_demo_seed_is_real_and_only_runs_once(client, fake_pp):
    fake_pp.route("POST", "/v2/invoicing/invoices", {"href": f"https://x/v2/invoicing/invoices/{ID}"})
    fake_pp.route("POST", INV_PATH + "/send", {})
    fake_pp.route("GET", INV_PATH, make_invoice(total="120.00", due=day(14), number="0042"))
    live.set_safe_mode(True)
    preview = client.post("/api/demo/seed").json()
    assert preview["preview"] is True and fake_pp.calls == []          # safe mode: preview, nothing cached
    live.set_safe_mode(False)
    first = client.post("/api/demo/seed")
    second = client.post("/api/demo/seed")
    assert first.status_code == 200 and first.json() == second.json()
    j = first.json()
    assert j["ok"] and j["invoice_id"] == ID and j["total"] == "120.00" and j["number"] == "0042"
    assert len(fake_pp.to("POST", "/v2/invoicing/invoices")) == 1       # one invoice, not two
    body = fake_pp.to("POST", "/v2/invoicing/invoices")[0]["json"]
    assert body["items"][0]["name"] == "Demo: logo design" and body["items"][0]["unit_amount"]["value"] == "120.00"
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "sb-wwhgs53882719@personal.example.com"


def test_the_demo_seed_refuses_to_run_on_live_paypal(client, no_pp, monkeypatch):
    monkeypatch.setattr(paypal, "BASE", "https://api-m.paypal.com")
    r = client.post("/api/demo/seed")
    assert r.status_code == 403 and "practice (sandbox)" in r.json()["message"]
    assert no_pp.calls == []


# ---------------------------------------------------------------- send + remind for real, and the log

def test_a_real_send_is_logged_once(client, fake_pp):
    fake_pp.route("POST", "/v2/invoicing/invoices", {"href": f"https://x/v2/invoicing/invoices/{ID}"})
    fake_pp.route("POST", INV_PATH + "/send", {})
    fake_pp.route("GET", INV_PATH, make_invoice(total="120.00", due=day(14), number="0042"))
    body = {**SEND, "request_id": "r-1", "deposit_percent": 25}
    a, b = client.post("/api/send", json=body), client.post("/api/send", json=body)
    assert a.status_code == 200 and a.json() == b.json() and a.json()["deposit_due"] == "30.00"
    (entry,) = client.get("/api/log").json()["entries"]
    assert entry["kind"] == "send" and entry["invoice_id"] == ID and entry["number"] == "0042"
    assert entry["client_name"] == "Maya Chen" and entry["time"]


def test_a_failed_send_is_a_plain_message_cleans_up_and_is_not_logged(client, fake_pp):
    fake_pp.route("POST", "/v2/invoicing/invoices", {"href": f"https://x/v2/invoicing/invoices/{ID}"})
    fake_pp.route("POST", INV_PATH + "/send", PayPalError(500, {"name": "INTERNAL_SERVER_ERROR"}))
    fake_pp.route("DELETE", INV_PATH, {})
    r = client.post("/api/send", json=SEND)
    assert r.status_code == 502 and r.json() == {"message": "Couldn't reach PayPal. Check your internet and try again."}
    assert len(fake_pp.to("DELETE", ID)) == 1
    assert client.get("/api/log").json()["entries"] == []


def test_a_real_remind_goes_out_is_logged_and_stops_at_two_a_day(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(due=day(-3), number="0007"))
    fake_pp.route("POST", INV_PATH + "/remind", {})
    note = {"subject": "Reminder", "note": "Please pay."}
    for _ in range(2):
        r = client.post(f"/api/invoices/{ID}/remind", json=note)
        assert r.status_code == 200 and r.json() == {"ok": True, "client_email": "maya@example.com"}
    assert fake_pp.to("POST", "/remind")[0]["json"] == {**note, "send_to_invoicer": False}
    r = client.post(f"/api/invoices/{ID}/remind", json=note)
    assert r.status_code == 422 and "two reminders" in r.json()["message"]
    assert len(fake_pp.to("POST", "/remind")) == 2
    entries = client.get("/api/log").json()["entries"]
    assert [e["kind"] for e in entries] == ["remind", "remind"] and entries[0]["number"] == "0007"


def test_remind_refuses_an_invoice_that_is_paid(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(status="PAID"))
    r = client.post(f"/api/invoices/{ID}/remind", json={"subject": "Hi", "note": "Pay"})
    assert r.status_code == 422 and "nothing to remind" in r.json()["message"]
    assert fake_pp.writes() == []


def test_remind_still_needs_a_subject_and_a_message(client, no_pp):
    r = client.post(f"/api/invoices/{ID}/remind", json={"subject": " ", "note": "x"})
    assert r.status_code == 422 and r.json() == {"message": "Write a subject and a message first."}


def test_the_log_keeps_the_last_50_newest_first():
    for i in range(60):
        live.log_send("remind", f"INV{i}", str(i), "C")
    entries = live.get_log()
    assert len(entries) == 50 and entries[0]["number"] == "59" and entries[-1]["number"] == "10"
    assert set(entries[0]) == {"time", "kind", "invoice_id", "number", "client_name"}


# ---------------------------------------------------------------- practice payment + QR through the API

def test_record_test_payment_endpoint(client, fake_pp):
    fake_pp.route("GET", INV_PATH, [make_invoice(), make_invoice(status="MARKED_AS_PAID", due_amount="0.00")])
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-9"})
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["payment_id"] == "PAY-9" and j["amount_paid"] == "600.00" and j["status"] == "MARKED_AS_PAID"
    assert j["invoice"]["kind"] == "paid"


def test_record_test_payment_on_a_paid_invoice_is_a_plain_refusal(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(status="PAID", due_amount="0.00"))
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 422
    assert r.json() == {"message": "That invoice is already paid, so there is nothing left to mark as paid."}
    assert fake_pp.writes() == []


def test_record_test_payment_when_paypal_says_no(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice())
    fake_pp.route("POST", INV_PATH + "/payments", PayPalError(422, {"name": "UNPROCESSABLE_ENTITY"}))
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 422 and "practice payment" in r.json()["message"]
    assert "UNPROCESSABLE" not in r.text


def test_record_test_payment_is_refused_on_live_paypal(client, no_pp, monkeypatch):
    monkeypatch.setattr(paypal, "BASE", "https://api-m.paypal.com")
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 403 and "practice (sandbox)" in r.json()["message"] and no_pp.calls == []


def test_safe_mode_record_test_payment_is_a_preview_and_posts_nothing(client, fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(status="PARTIALLY_PAID", total="300.00", due_amount="120.00",
                                                due=day(-30), number="0003", name=("Priya", "Rao")))
    live.set_safe_mode(True)
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["preview"] is True and j["kind"] == "payment" and j["invoice_id"] == ID
    assert "Safe mode is on, so nothing was sent." in j["message"]
    assert "$120.00" in j["message"] and "Priya Rao" in j["message"] and "invoice 0003" in j["message"]
    assert j["payload"]["method"] == "CASH" and j["payload"]["amount"] == {"currency_code": "USD", "value": "120.00"}
    assert "invoice" not in j and "amount_paid" not in j and "payment_id" not in j   # nothing that looks like a payment
    assert fake_pp.writes() == []                                                    # only the read of the invoice
    assert client.get("/api/log").json() == {"entries": []}


@pytest.mark.parametrize("status,words", [("PAID", "already paid"), ("DRAFT", "hasn't been sent"), ("CANCELLED", "cancelled")])
def test_safe_mode_payment_preview_keeps_the_same_plain_refusals(client, fake_pp, status, words):
    fake_pp.route("GET", INV_PATH, make_invoice(status=status, due_amount="0.00" if status == "PAID" else "same"))
    live.set_safe_mode(True)
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 422 and words in r.json()["message"] and fake_pp.writes() == []


def test_safe_mode_payment_preview_is_still_refused_on_live_paypal(client, no_pp, monkeypatch):
    monkeypatch.setattr(paypal, "BASE", "https://api-m.paypal.com")
    live.set_safe_mode(True)
    r = client.post(f"/api/invoices/{ID}/record-test-payment")
    assert r.status_code == 403 and no_pp.calls == []


def test_turning_safe_mode_off_makes_the_payment_real_again(client, fake_pp):
    fake_pp.route("GET", INV_PATH, [make_invoice(), make_invoice(), make_invoice(status="MARKED_AS_PAID", due_amount="0.00")])
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-1"})
    live.set_safe_mode(True)
    assert client.post(f"/api/invoices/{ID}/record-test-payment").json()["preview"] is True
    live.set_safe_mode(False)
    j = client.post(f"/api/invoices/{ID}/record-test-payment").json()
    assert j["payment_id"] == "PAY-1" and "preview" not in j and len(fake_pp.to("POST", "/payments")) == 1


def test_qr_endpoint(client, fake_pp):
    raw = (b'--b\r\nContent-Disposition: form-data; name="image"\r\nContent-Type: text/plain\r\n\r\n'
           + PNG_B64.encode() + b"\r\n--b--")
    fake_pp.route("POST", INV_PATH + "/generate-qr-code", {"_raw": raw, "_content_type": "multipart/form-data; boundary=b"})
    r = client.get(f"/api/invoices/{ID}/qr")
    assert r.status_code == 200 and r.json() == {"image": "data:image/png;base64," + PNG_B64}


def test_qr_endpoint_failure_is_a_plain_message(client, fake_pp):
    fake_pp.route("POST", INV_PATH + "/generate-qr-code", PayPalError(400, {"name": "INVALID_REQUEST"}))
    r = client.get(f"/api/invoices/{ID}/qr")
    assert r.status_code == 422
    assert r.json() == {"message": "PayPal couldn't make a QR code for that invoice. It may not have been sent yet."}


# ---------------------------------------------------------------- the PayPal webhook

PP_HEADERS = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.paypal.com/cert",
              "paypal-transmission-id": "t-1", "paypal-transmission-sig": "sig",
              "paypal-transmission-time": "2026-11-13T10:00:00Z"}
VERIFY = "/v1/notifications/verify-webhook-signature"


def event(n=1, type_="INVOICING.INVOICE.PAID", invoice_id=ID):
    return {"id": f"WH-EVT-{n}", "event_type": type_, "create_time": f"2026-11-13T10:00:{n % 60:02d}Z",
            "resource": {"invoice": {"id": invoice_id, "status": "PAID"}}}


def post_event(client, ev, headers=None):
    return client.post("/api/webhooks/paypal", content=json.dumps(ev), headers={
        "content-type": "application/json", **(PP_HEADERS if headers is None else headers)})


@pytest.fixture
def hook(monkeypatch, fake_pp):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-123")
    fake_pp.route("POST", VERIFY, {"verification_status": "SUCCESS"})
    return fake_pp


def test_webhooks_ready_shows_in_health(client, monkeypatch):
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-123")
    assert client.get("/api/health").json()["webhooks_ready"] is True


def test_a_verified_invoice_event_is_stored_and_listed(client, hook):
    r = post_event(client, event(1))
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": True}
    sent = hook.calls[0]["json"]
    assert sent["webhook_id"] == "WH-123" and sent["webhook_event"]["id"] == "WH-EVT-1"
    assert sent["transmission_id"] == "t-1" and sent["cert_url"] == "https://api.paypal.com/cert"
    assert client.get("/api/events").json() == {"last_seq": 1, "events": [
        {"seq": 1, "type": "INVOICING.INVOICE.PAID", "invoice_id": ID, "time": "2026-11-13T10:00:01Z"}]}


def test_an_unverified_event_is_rejected_and_never_stored(client, hook):
    hook.routes.clear()
    hook.route("POST", VERIFY, {"verification_status": "FAILURE"})
    r = post_event(client, event(1))
    assert r.status_code == 400 and r.json() == {"message": "That notification could not be verified."}
    assert client.get("/api/events").json() == {"events": [], "last_seq": 0}


def test_an_event_without_paypals_signature_headers_never_reaches_paypal(client, hook):
    r = post_event(client, event(1), headers={"paypal-transmission-id": "t-1"})
    assert r.status_code == 400 and hook.calls == []
    assert client.get("/api/events").json()["events"] == []


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "", '"text"'])
def test_a_body_that_is_not_an_event_is_rejected(client, hook, content):
    r = client.post("/api/webhooks/paypal", content=content, headers=PP_HEADERS)
    assert r.status_code == 400 and r.json() == {"message": "That doesn't look like a PayPal notification."}
    assert hook.calls == []


def test_a_huge_body_is_refused(client, hook):
    r = client.post("/api/webhooks/paypal", content="x" * 300_000, headers=PP_HEADERS)
    assert r.status_code == 413 and hook.calls == []


def test_a_body_that_says_it_is_huge_is_refused_before_any_of_it_is_read(client, hook, monkeypatch):
    """Content-Length over the limit: 413 without touching the body (so a 200 MB upload is never held in memory)."""
    from starlette.requests import Request

    async def never(self):
        raise AssertionError("the body must not be read when Content-Length is already over the limit")
        yield b""
    monkeypatch.setattr(Request, "stream", never)
    r = client.post("/api/webhooks/paypal", content=b"x" * 300_000, headers=PP_HEADERS)
    assert r.status_code == 413 and r.json() == {"message": "That notification is too big."}
    assert r.headers.get("connection") == "close" and hook.calls == []


def test_a_garbage_content_length_is_refused_too(client, hook):
    r = client.post("/api/webhooks/paypal", content=b"{}", headers={**PP_HEADERS, "content-length": "lots"})
    assert r.status_code == 413 and hook.calls == []


def test_a_chunked_body_is_cut_off_at_the_limit_not_read_to_the_end(client, hook, monkeypatch):
    """No Content-Length (chunked): reading stops as soon as the limit is passed."""
    from starlette.requests import Request
    from app import main

    seen = []

    async def endless(self):
        for _ in range(10_000):
            seen.append(1)
            yield b"x" * 10_000
    monkeypatch.setattr(Request, "stream", endless)
    r = client.post("/api/webhooks/paypal", content=b"{}", headers={**PP_HEADERS, "content-length": "2"})
    assert r.status_code == 413 and hook.calls == []
    assert len(seen) * 10_000 <= main.WEBHOOK_MAX_BYTES + 10_000      # stopped right after the limit, not at 100 MB


def test_a_body_just_under_the_limit_is_still_read_and_checked(client, hook):
    ev = json.dumps({**event(1), "padding": "x" * 200_000})
    r = client.post("/api/webhooks/paypal", content=ev, headers={"content-type": "application/json", **PP_HEADERS})
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": True}


def test_other_verified_events_are_accepted_but_not_kept(client, hook):
    r = post_event(client, event(1, type_="PAYMENT.SALE.COMPLETED"))
    assert r.status_code == 200 and r.json() == {"ok": True, "stored": False}
    assert client.get("/api/events").json()["events"] == []


def test_a_retried_delivery_is_stored_once(client, hook):
    assert post_event(client, event(1)).json()["stored"] is True
    assert post_event(client, event(1)).json()["stored"] is False
    assert len(client.get("/api/events").json()["events"]) == 1


def test_events_since_and_the_last_50(client, hook):
    for n in range(1, 56):
        post_event(client, event(n))
    all_ = client.get("/api/events").json()
    assert all_["last_seq"] == 55 and len(all_["events"]) == 50 and all_["events"][0]["seq"] == 6
    newer = client.get("/api/events?since=53").json()
    assert [e["seq"] for e in newer["events"]] == [54, 55] and newer["last_seq"] == 55
    assert client.get("/api/events?since=55").json() == {"events": [], "last_seq": 55}
    assert len(client.get("/api/events?since=9999").json()["events"]) == 50  # the app restarted: start over
    assert client.get("/api/events?since=abc").status_code == 422
    assert client.get("/api/events?since=-1").status_code == 422


def test_the_invoice_id_is_found_in_either_event_shape(client, hook):
    post_event(client, {"id": "A", "event_type": "INVOICING.INVOICE.CANCELLED", "resource": {"id": ID}})
    post_event(client, {"id": "B", "event_type": "INVOICING.INVOICE.UPDATED", "resource": {}})
    post_event(client, {"id": "C", "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": "bad id!"}}})
    assert [e["invoice_id"] for e in client.get("/api/events").json()["events"]] == [ID, None, None]


def test_when_paypal_cannot_check_the_signature(client, hook):
    hook.routes.clear()
    hook.route("POST", VERIFY, PayPalError(500))
    assert post_event(client, event(1)).status_code == 503              # PayPal will deliver it again
    hook.routes.clear()
    hook.route("POST", VERIFY, PayPalError(400, {"name": "VALIDATION_ERROR"}))
    assert post_event(client, event(2)).status_code == 400
    hook.routes.clear()
    hook.route("POST", VERIFY, RuntimeError("boom"))
    assert post_event(client, event(3)).status_code == 503
    assert client.get("/api/events").json()["events"] == []


def test_the_webhook_cannot_be_used_to_make_us_hammer_paypal(client, hook, monkeypatch):
    monkeypatch.setattr(live, "VERIFY_PER_MINUTE", 2)
    codes = [post_event(client, event(n)).status_code for n in range(1, 5)]
    assert codes == [200, 200, 429, 429]
    assert len(hook.calls) == 2


# ---------------------------------------------------------------- the existing single-invoice reminder draft still works

@pytest.mark.parametrize("body", [None, {"tone": "firm", "sender_name": "Julian"}])
def test_draft_nudge_still_works_with_and_without_a_body(client, fake_pp, monkeypatch, body):
    from app import ai
    fake_pp.route("GET", INV_PATH, make_invoice(due=day(-5), number="0009"))
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: {"subject": "Reminder", "note": "Hi Maya, a reminder for invoice 0009."})
    r = client.post(f"/api/invoices/{ID}/draft-nudge", **({} if body is None else {"json": body}))
    assert r.status_code == 200
    j = r.json()
    assert j["subject"] == "Reminder" and j["days_overdue"] == 5 and j["is_overdue"] is True
    assert fake_pp.writes() == []
