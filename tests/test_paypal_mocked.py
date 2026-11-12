"""PayPal calls with paypal._call replaced: what we ask PayPal for, and what we do when it says no."""
import datetime as dt
import re

import pytest

from app import paypal
from app.errors import PayPalError, UserError
from helpers import PNG_B64, day, make_invoice

ID = "INV2-AAAA-BBBB-CCCC-DDDD"
INV_PATH = f"/v2/invoicing/invoices/{ID}"


# ---------------------------------------------------------------- record_payment

def test_record_payment_pays_what_is_owed(fake_pp):
    fake_pp.route("GET", INV_PATH, [make_invoice(due=day(-3)),
                                    make_invoice(status="MARKED_AS_PAID", due_amount="0.00", due=day(-3))])
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-1"})
    out = paypal.record_payment(ID)

    (call,) = fake_pp.to("POST", "/payments")
    body = call["json"]
    assert body["method"] == "CASH" and body["note"] == "Marked paid for demo"
    assert body["amount"] == {"currency_code": "USD", "value": "600.00"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["payment_date"])
    assert dt.date.fromisoformat(body["payment_date"]) <= dt.datetime.now(dt.timezone.utc).date()
    assert not fake_pp.to("POST", "/record-payment")  # the path is /payments, not /record-payment
    assert out["ok"] and out["payment_id"] == "PAY-1" and out["amount_paid"] == "600.00"
    assert out["status"] == "MARKED_AS_PAID" and out["invoice"]["kind"] == "paid"


def test_record_payment_pays_only_the_part_still_owed(fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(status="PARTIALLY_PAID", total="120.00", due_amount="60.00"))
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-2"})
    assert paypal.record_payment(ID)["amount_paid"] == "60.00"
    assert fake_pp.to("POST", "/payments")[0]["json"]["amount"]["value"] == "60.00"


def test_record_payment_uses_the_invoice_currency_and_its_precision(fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(cur="JPY", total="12000"))
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-3"})
    paypal.record_payment(ID)
    assert fake_pp.to("POST", "/payments")[0]["json"]["amount"] == {"currency_code": "JPY", "value": "12000"}


def test_record_payment_still_works_if_reading_back_fails(fake_pp):
    fake_pp.route("GET", INV_PATH, [make_invoice(), PayPalError(500)])
    fake_pp.route("POST", INV_PATH + "/payments", {"payment_id": "PAY-4"})
    out = paypal.record_payment(ID)
    assert out["ok"] and out["status"] == "MARKED_AS_PAID" and out["invoice"] is None


@pytest.mark.parametrize("status,words", [
    ("PAID", "already paid"), ("MARKED_AS_PAID", "already paid"), ("DRAFT", "hasn't been sent"),
    ("SCHEDULED", "hasn't been sent"), ("CANCELLED", "cancelled"), ("PAYMENT_PENDING", "right now"),
    ("REFUNDED", "right now")])
def test_record_payment_refuses_invoices_that_cannot_be_paid(fake_pp, status, words):
    fake_pp.route("GET", INV_PATH, make_invoice(status=status))
    with pytest.raises(UserError) as e:
        paypal.record_payment(ID)
    assert words in e.value.message and e.value.status == 422
    assert fake_pp.writes() == []


def test_record_payment_refuses_when_nothing_is_owed(fake_pp):
    fake_pp.route("GET", INV_PATH, make_invoice(due_amount="0.00"))
    with pytest.raises(UserError, match="already paid"):
        paypal.record_payment(ID)
    assert fake_pp.writes() == []


def test_record_payment_is_sandbox_only(fake_pp, monkeypatch):
    monkeypatch.setattr(paypal, "BASE", "https://api-m.paypal.com")
    with pytest.raises(UserError) as e:
        paypal.record_payment(ID)
    assert e.value.status == 403 and "practice (sandbox)" in e.value.message
    assert fake_pp.calls == []  # not even a read


def test_payment_date_is_never_in_the_future_or_before_the_invoice():
    now = dt.datetime.now(dt.timezone.utc)
    assert paypal._payment_date({}) <= (now.date() + dt.timedelta(days=0)).isoformat()
    assert dt.date.fromisoformat(paypal._payment_date({})) >= (now - dt.timedelta(hours=36)).date()
    future = {"detail": {"invoice_date": day(+3)}}
    assert paypal._payment_date(future) == day(+3)


# ---------------------------------------------------------------- QR code

def test_generate_qr_asks_for_a_300_pixel_code_and_returns_a_data_url(fake_pp):
    raw = (b'--xyz\r\nContent-Disposition: form-data; name="image"\r\nContent-Type: text/plain\r\n\r\n'
           + PNG_B64.encode() + b"\r\n--xyz--")
    fake_pp.route("POST", INV_PATH + "/generate-qr-code",
                  {"_raw": raw, "_content_type": "multipart/form-data; boundary=xyz"})
    image = paypal.generate_qr(ID)
    assert image == "data:image/png;base64," + PNG_B64
    assert fake_pp.calls[0]["json"] == {"width": 300, "height": 300}


def test_generate_qr_with_an_unreadable_answer_is_a_plain_error(fake_pp):
    fake_pp.route("POST", INV_PATH + "/generate-qr-code", {"_raw": b"<html>", "_content_type": "text/html"})
    with pytest.raises(UserError) as e:
        paypal.generate_qr(ID)
    assert e.value.status == 502 and "QR code" in e.value.message


# ---------------------------------------------------------------- create / deposit / terms

PAYLOAD = {"client_name": "Maya Chen", "client_email": "maya@example.com", "currency": "USD", "due_days": 14,
           "note": "Thanks!", "items": [{"name": "Logo design", "qty": 1, "unit_price": "120.00"}]}


def route_create(fake_pp, *, send=None, got=None):
    fake_pp.route("POST", "/v2/invoicing/invoices", {"href": f"https://api.sandbox.paypal.com/v2/invoicing/invoices/{ID}"})
    fake_pp.route("POST", INV_PATH + "/send", {} if send is None else send)
    fake_pp.route("GET", INV_PATH, got or make_invoice(total="120.00", due=day(14)))
    fake_pp.route("DELETE", INV_PATH, {})


def test_create_invoice_without_a_deposit_has_no_partial_payment(fake_pp):
    route_create(fake_pp)
    out = paypal.create_invoice(dict(PAYLOAD))
    body = fake_pp.to("POST", "/v2/invoicing/invoices")[0]["json"]
    assert "configuration" not in body and "terms_and_conditions" not in body["detail"]
    assert out["deposit_due"] is None and out["ok"] and out["invoice_id"] == ID and out["status"] == "SENT"
    assert fake_pp.to("POST", "/send")[0]["json"] == {"send_to_recipient": True, "send_to_invoicer": False}


def test_create_invoice_with_a_deposit(fake_pp):
    route_create(fake_pp)
    out = paypal.create_invoice({**PAYLOAD, "deposit_percent": 50})
    body = fake_pp.to("POST", "/v2/invoicing/invoices")[0]["json"]
    assert body["configuration"] == {"partial_payment": {
        "allow_partial_payment": True, "minimum_amount_due": {"currency_code": "USD", "value": "60.00"}}}
    assert out["deposit_due"] == "60.00" and out["total"] == "120.00"


def test_deposit_in_a_currency_with_no_cents(fake_pp):
    route_create(fake_pp, got=make_invoice(cur="JPY", total="999"))
    out = paypal.create_invoice({**PAYLOAD, "currency": "JPY", "items": [{"name": "Logo", "qty": 1, "unit_price": "999"}],
                                 "deposit_percent": 15})
    body = fake_pp.to("POST", "/v2/invoicing/invoices")[0]["json"]
    assert body["configuration"]["partial_payment"]["minimum_amount_due"] == {"currency_code": "JPY", "value": "150"}
    assert out["deposit_due"] == "150"


def test_late_fee_note_goes_into_the_terms(fake_pp):
    route_create(fake_pp)
    paypal.create_invoice({**PAYLOAD, "late_fee_note": "A 5% fee applies after 14 days."})
    body = fake_pp.to("POST", "/v2/invoicing/invoices")[0]["json"]
    assert body["detail"]["terms_and_conditions"] == "A 5% fee applies after 14 days."


def test_body_has_a_due_date_or_due_on_receipt():
    body, due = paypal.build_invoice_body({**PAYLOAD}, today=dt.date(2026, 11, 13))
    assert due == "2026-11-27" and body["detail"]["payment_term"] == {
        "term_type": "DUE_ON_DATE_SPECIFIED", "due_date": "2026-11-27"}
    body, due = paypal.build_invoice_body({**PAYLOAD, "due_days": None})
    assert due is None and body["detail"]["payment_term"] == {"term_type": "DUE_ON_RECEIPT"}
    assert body["items"][0]["quantity"] == "1" and body["items"][0]["unit_amount"]["value"] == "120.00"
    assert body["primary_recipients"][0]["billing_info"]["name"] == {"given_name": "Maya", "surname": "Chen"}


def test_send_failure_deletes_the_draft_and_the_retry_gets_a_fresh_key(fake_pp):
    route_create(fake_pp, send=PayPalError(500))
    d = {**PAYLOAD, "request_id": "req-1"}
    with pytest.raises(PayPalError):
        paypal.create_invoice(d)
    assert len(fake_pp.to("DELETE", ID)) == 1            # no orphan draft left behind
    assert paypal._attempts["req-1"] == 1
    assert "req-1" not in paypal._results                 # a failure is not remembered as a success
    with pytest.raises(PayPalError):
        paypal.create_invoice(d)
    keys = [c["headers"]["PayPal-Request-Id"] for c in fake_pp.to("POST", "/v2/invoicing/invoices")]
    assert keys == ["req-1-0", "req-1-1"]


def test_send_failure_still_raises_the_original_error_if_cleanup_fails(fake_pp):
    route_create(fake_pp, send=PayPalError(503))
    fake_pp.routes = [r for r in fake_pp.routes if r[0] != "DELETE"] + [("DELETE", INV_PATH, PayPalError(500))]
    with pytest.raises(PayPalError) as e:
        paypal.create_invoice(dict(PAYLOAD))
    assert e.value.status == 503


def test_a_repeated_request_id_returns_the_first_result_without_a_second_invoice(fake_pp):
    route_create(fake_pp)
    d = {**PAYLOAD, "request_id": "req-2"}
    first = paypal.create_invoice(d)
    assert paypal.create_invoice(d) == first
    assert len(fake_pp.to("POST", "/v2/invoicing/invoices")) == 1


def test_a_sent_invoice_is_still_reported_if_reading_it_back_fails(fake_pp):
    route_create(fake_pp, got=PayPalError(500))
    out = paypal.create_invoice(dict(PAYLOAD))
    assert out["ok"] and out["number"] is None and out["total"] == "120.00" and out["status"] == "SENT"


# ---------------------------------------------------------------- webhook signature check

def test_verify_webhook_sends_paypals_headers_and_the_event(fake_pp):
    fake_pp.route("POST", "/v1/notifications/verify-webhook-signature", {"verification_status": "SUCCESS"})
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
               "PAYPAL-TRANSMISSION-ID": "t-1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "now"}
    assert paypal.verify_webhook(headers, {"id": "WH-EVT"}, "WH-123") is True
    assert fake_pp.calls[0]["json"] == {
        "auth_algo": "SHA256withRSA", "cert_url": "https://api.paypal.com/cert", "transmission_id": "t-1",
        "transmission_sig": "sig", "transmission_time": "now", "webhook_id": "WH-123", "webhook_event": {"id": "WH-EVT"}}


@pytest.mark.parametrize("answer", [{"verification_status": "FAILURE"}, {}, {"verification_status": "success"}, "SUCCESS"])
def test_verify_webhook_is_false_unless_paypal_says_success(fake_pp, answer):
    fake_pp.route("POST", "/v1/notifications/verify-webhook-signature", answer)
    assert paypal.verify_webhook({}, {}, "WH-123") is False


# ---------------------------------------------------------------- the list path

def test_list_invoices_normalizes_and_sorts(fake_pp):
    fake_pp.route("POST", "/v2/invoicing/search-invoices*", {"total_items": 3, "items": [
        make_invoice(id="paid", status="PAID"), make_invoice(id="late", due=day(-4)), "junk",
        make_invoice(id="soon", due=day(3))]})
    out = paypal.list_invoices()
    assert [r["id"] for r in out["invoices"]] == ["late", "soon", "paid"] and out["total"] == 3
