"""Pure functions: no network, no server."""
import base64
import datetime as dt
from decimal import Decimal

import pytest

from app import ai, paypal
from helpers import PNG, PNG_B64, day, make_invoice

TODAY = dt.date(2026, 11, 13)


def norm(**kw):
    return paypal.normalize_invoice(make_invoice(**kw), today=TODAY)


# ---------------------------------------------------------------- normalize_invoice

def test_normalize_waiting():
    n = norm(due="2026-11-18")
    assert (n["kind"], n["label"], n["days_to_due"], n["can_remind"]) == ("waiting", "Waiting for payment", 5, True)
    assert n["client_name"] == "Maya Chen" and n["client_email"] == "maya@example.com"
    assert n["total"] == "600.00" and n["amount_due"] == "600.00" and n["number"] == "0001"


def test_normalize_overdue_wording():
    assert norm(due="2026-11-01")["label"] == "Overdue by 12 days"
    assert norm(due="2026-11-12")["label"] == "Overdue by 1 day"
    assert norm(due="2026-11-01")["kind"] == "overdue"


@pytest.mark.parametrize("status,kind,can_remind", [
    ("DRAFT", "draft", False), ("SCHEDULED", "draft", False), ("PAID", "paid", False),
    ("MARKED_AS_PAID", "paid", False), ("CANCELLED", "cancelled", False), ("REFUNDED", "refunded", False),
    ("PAYMENT_PENDING", "waiting", False), ("UNPAID", "waiting", True), ("SOMETHING_NEW", "other", False)])
def test_normalize_statuses(status, kind, can_remind):
    n = norm(status=status, due="2026-11-18")
    assert n["kind"] == kind and n["can_remind"] is can_remind


def test_normalize_partly_paid_shows_what_is_left():
    n = norm(status="PARTIALLY_PAID", total="120.00", due_amount="60.00", due="2026-11-18")
    assert n["kind"] == "partial" and n["amount_due"] == "60.00" and "$60.00 still owed" in n["label"]
    assert n["can_remind"] is True


def test_normalize_partly_paid_and_late_is_called_overdue_but_still_shows_less_owed_than_the_total():
    # The label says "overdue", not "partial", so the page must decide "partly paid" from the numbers.
    n = norm(status="PARTIALLY_PAID", total="300.00", due_amount="120.00", due="2026-10-14")
    assert n["kind"] == "overdue" and n["label"] == "Overdue by 30 days"
    assert n["total"] == "300.00" and n["amount_due"] == "120.00" and n["can_remind"] is True


def test_normalize_never_raises_on_missing_keys():
    n = paypal.normalize_invoice({}, today=TODAY)
    assert n["kind"] == "other" and n["total"] == "0.00" and n["due_date"] is None and n["days_to_due"] is None
    assert paypal.normalize_invoice({"status": "SENT", "primary_recipients": "junk", "detail": []},
                                    today=TODAY)["client_name"] == ""


def test_normalize_reads_the_deposit_only_when_partial_payment_is_allowed():
    cfg = {"partial_payment": {"allow_partial_payment": True, "minimum_amount_due": {"value": "60"}}}
    assert norm(configuration=cfg)["deposit_due"] == "60.00"
    cfg["partial_payment"]["allow_partial_payment"] = False
    assert norm(configuration=cfg)["deposit_due"] is None
    assert norm()["deposit_due"] is None


def test_normalize_zero_decimal_currency():
    n = norm(cur="JPY", total="12000")
    assert n["total"] == "12000" and n["currency"] == "JPY"


# ---------------------------------------------------------------- money

@pytest.mark.parametrize("value,cur,expected", [
    ("600", "USD", "600.00"), (Decimal("2.675"), "USD", "2.68"), ("1000.5", "JPY", "1001"),
    (None, "USD", "0.00"), ("abc", "USD", "0.00"), ("15.38", "USD", "15.38"), (0.1, "EUR", "0.10")])
def test_money_str(value, cur, expected):
    assert paypal.money_str(value, cur) == expected


def test_invoice_total_rounds_each_line():
    d = {"currency": "USD", "items": [{"qty": 3, "unit_price": "19.99"}, {"qty": "1.5", "unit_price": "0.33"}]}
    assert paypal.invoice_total(d) == Decimal("59.97") + Decimal("0.50")  # 0.495 rounds up


def test_invoice_total_zero_decimal():
    d = {"currency": "JPY", "items": [{"qty": 2, "unit_price": 500}, {"qty": "1.5", "unit_price": 333}]}
    assert paypal.invoice_total(d) == Decimal("1500")  # 1000 + 499.5 -> 500


def test_fmt_money():
    assert paypal.fmt_money("600", "USD") == "$600.00"
    assert paypal.fmt_money("600", "CHF") == "CHF 600.00"


@pytest.mark.parametrize("total,pct,cur,expected", [
    ("120.00", 50, "USD", "60.00"), ("100.00", 33, "USD", "33.00"), ("99.99", 10, "USD", "10.00"),
    ("999", 15, "JPY", "150"), ("0.01", 90, "USD", "0.01")])
def test_deposit_amount(total, pct, cur, expected):
    assert paypal.money_str(paypal.deposit_amount(Decimal(total), pct, cur), cur) == expected


def test_deposit_due_is_none_without_a_deposit():
    d = {"currency": "USD", "items": [{"qty": 1, "unit_price": "100"}]}
    assert paypal.deposit_due(d) is None
    assert paypal.deposit_due({**d, "deposit_percent": 25}) == "25.00"


# ---------------------------------------------------------------- sort + view url

def test_sort_invoices_order():
    rows = [norm(id="paid", status="PAID", due="2026-11-01"),
            norm(id="later", due="2026-12-01"),
            norm(id="late3", due="2026-11-10"),
            norm(id="draft", status="DRAFT"),
            norm(id="late10", due="2026-11-03"),
            norm(id="soon", due="2026-11-14")]
    assert [r["id"] for r in paypal.sort_invoices(rows)] == ["late10", "late3", "soon", "later", "draft", "paid"]


def test_view_url_prefers_what_paypal_sent():
    inv = make_invoice()
    inv["detail"]["metadata"]["recipient_view_url"] = "https://www.sandbox.paypal.com/invoice/p/#ABC"
    assert paypal._view_url(inv) == "https://www.sandbox.paypal.com/invoice/p/#ABC"


def test_view_url_built_from_the_id():
    assert paypal._view_url(make_invoice(id="INV2-AAAA-BBBB-CCCC-DDDD")) == \
        "https://www.sandbox.paypal.com/invoice/p/#AAAABBBBCCCCDDDD"


def test_view_url_none_for_drafts_and_odd_ids():
    assert paypal._view_url(make_invoice(status="DRAFT")) is None
    assert paypal._view_url(make_invoice(id="not-an-invoice-id")) is None


def test_view_url_on_the_live_site(monkeypatch):
    monkeypatch.setattr(paypal, "BASE", "https://api-m.paypal.com")
    assert paypal._view_url(make_invoice()).startswith("https://www.paypal.com/invoice/p/#")


# ---------------------------------------------------------------- tone ladder, overdue days

@pytest.mark.parametrize("days,tone", [
    (-5, "friendly"), (0, "friendly"), (1, "polite"), (7, "polite"), (8, "firm"), (21, "firm"),
    (22, "final"), (90, "final")])
def test_tone_ladder(days, tone):
    assert ai.nudge_tone(days) == tone


def test_days_overdue_and_awaiting_payment():
    assert paypal.days_overdue({"days_to_due": -12}) == 12
    assert paypal.days_overdue({"days_to_due": 4}) == 0
    assert paypal.days_overdue({"days_to_due": None}) == 0
    rows = [paypal.normalize_invoice(make_invoice(id=i, status=s, due=d), today=dt.date.today())
            for i, s, d in [("paid", "PAID", day(-9)), ("draft", "DRAFT", None), ("cancel", "CANCELLED", day(-9)),
                            ("soon", "SENT", day(2)), ("late5", "SENT", day(-5)), ("late20", "UNPAID", day(-20)),
                            ("pending", "PAYMENT_PENDING", day(-1))]]
    assert [r["id"] for r in paypal.awaiting_payment(rows)] == ["late20", "late5", "soon"]


def test_awaiting_payment_skips_invoices_with_nothing_owed():
    rows = [paypal.normalize_invoice(make_invoice(id="zero", total="0.00", due=day(-2)), today=dt.date.today()),
            paypal.normalize_invoice(make_invoice(id="settled", status="SENT", total="50.00", due_amount="0.00"),
                                     today=dt.date.today()),
            paypal.normalize_invoice(make_invoice(id="owed", total="50.00"), today=dt.date.today())]
    assert [r["id"] for r in paypal.awaiting_payment(rows)] == ["owed"]


# ---------------------------------------------------------------- clean_draft

def test_clean_draft_drops_an_email_that_is_not_in_the_note():
    out = ai.clean_draft({"client_name": "Maya Chen", "client_email": "made.up@example.com",
                          "items": [{"name": "Logo", "qty": 1, "unit_price": 450}]}, "Logo for Maya, $450")
    assert out["client_email"] == ""
    assert any("email" in q.lower() and "Maya" in q for q in out["questions"])


def test_clean_draft_keeps_an_email_that_is_in_the_note():
    out = ai.clean_draft({"client_name": "Maya", "client_email": "maya@example.com",
                          "items": [{"name": "Logo", "qty": 1, "unit_price": 450}]},
                         "Logo for Maya maya@example.com $450")
    assert out["client_email"] == "maya@example.com" and out["questions"] == []
    assert out["items"] == [{"name": "Logo", "qty": 1, "unit_price": 450}]


def test_clean_draft_zero_quantity_becomes_a_question():
    out = ai.clean_draft({"client_name": "Maya", "client_email": "",
                          "items": [{"name": "Logo", "qty": 0, "unit_price": 450}]})
    assert out["items"] == [] and any("How many" in q for q in out["questions"])


def test_clean_draft_removes_links_from_the_note():
    out = ai.clean_draft({"note": "Pay at http://evil.example now", "items": []})
    assert out["note"] == "" and any("link or email" in q for q in out["questions"])


@pytest.mark.parametrize("due,expected", [(0, None), (14, 14), (400, None), (-3, None), (None, None)])
def test_clean_draft_due_days(due, expected):
    assert ai.clean_draft({"due_days": due, "items": []})["due_days"] == expected


def test_clean_draft_bad_currency_becomes_usd():
    assert ai.clean_draft({"currency": "dollars", "items": []})["currency"] == "USD"
    assert ai.clean_draft({"currency": "eur", "items": []})["currency"] == "EUR"


# ---------------------------------------------------------------- nudge wording rules

IT = {"ref": "1", "number": "0001", "client_name": "Maya Chen", "currency": "USD", "amount_raw": "600.00",
      "amount_text": "$600.00", "total_text": "$600.00", "due_date": "2026-11-01", "days_overdue": 12,
      "days_to_due": -12, "tone": "firm"}


def test_clean_nudge_text_accepts_honest_wording():
    ok = "Hi Maya, invoice 0001 for $600.00 was due on November 1, 2026 and is 12 days overdue."
    assert ai.clean_nudge_text(ok, IT, 400) == ok
    assert ai.clean_nudge_text("12 days late, 600 USD", IT, 80) == "12 days late, 600 USD"


@pytest.mark.parametrize("bad", [
    "Hi Maya, please pay $900.00 today.",                       # an amount that is not on the invoice
    "Hi Maya, you owe $600.00 plus 15 dollars.",                # an extra number
    "Pay at https://example.com/pay",                           # a link
    "Pay at www.example.com",
    "Reply to me at maya@example.com",                          # an email address
    "Hi [Your name], pay up.",                                  # a placeholder
    "Hi {{name}}, pay up.",
    "A late fee of the usual kind applies.",                    # a threat
    "We may start collection steps.",
    "It was due on October 1.",                                 # the wrong month
    "x" * 500,                                                  # too long
    "", "   ", None, 42])
def test_clean_nudge_text_rejects_invented_or_unsafe_wording(bad):
    assert ai.clean_nudge_text(bad, IT, 400) is None


def test_clean_nudge_text_never_calls_a_future_invoice_late():
    soon = {**IT, "days_overdue": 0, "days_to_due": 5, "due_date": "2026-11-18", "tone": "friendly"}
    assert ai.clean_nudge_text("Just a heads-up, it is due in 5 days.", soon, 400)
    assert ai.clean_nudge_text("This invoice is overdue.", soon, 400) is None
    assert ai.clean_nudge_text("Do not be late with it.", soon, 400) is None


def test_clean_nudge_text_allows_thousands_separators():
    big = {**IT, "amount_text": "$1,200.00", "total_text": "$1,200.00", "amount_raw": "1200.00"}
    assert ai.clean_nudge_text("It is $1,200.00, 12 days late.", big, 100)
    assert ai.clean_nudge_text("It is $1,300.00.", big, 100) is None


PARTLY = {**IT, "amount_raw": "100.00", "amount_text": "$100.00", "total_text": "$900.00"}   # $100 of $900 still owed


@pytest.mark.parametrize("bad,it", [
    # a digit that happens to be in an invoice fact is not a licence to say any amount
    ("Hi Maya, please pay just $1 for invoice 0001.", IT),
    ("Hi Maya, please pay just $4 for invoice 0004.", {**IT, "number": "0004"}),
    ("Hi Maya, please pay just $6 for invoice 0006.", {**IT, "number": "0006"}),
    ("Hi Maya, you owe $11 on invoice 0099.", {**IT, "number": "0099", "due_date": "2026-11-11", "days_overdue": 12}),
    ("Hi Maya, you owe 12 USD for invoice 0001.", IT),                     # the number of days, used as money
    ("Hi Maya, you owe 12 dollars.", IT),
    ("Hi Maya, please pay 1 for invoice 0001.", IT),                       # a bare number with no role
    ("Hi Maya, please pay 0001 today.", IT),                               # the invoice number used as an amount
    ("Hi Maya, please pay $0.01.", IT),
    # the total of a partly paid invoice is not what is owed
    ("Hi Maya, please pay $900.00 for invoice 0001.", PARTLY),
    ("Hi Maya, please pay 900 USD.", PARTLY),
    # amounts in words
    ("Hi Maya, please pay one dollar.", IT),
    ("Hi Maya, please pay a dollar.", IT),
    ("Hi Maya, fifty bucks will do.", IT),
    ("Hi Maya, please pay one hundred dollars.", PARTLY),
    # other invented facts
    ("Hi Maya, please pay within 7 days.", IT),
    ("Hi Maya, this has been open since 2025.", IT),
    ("Hi Maya, invoice 0001 was due on November 2.", IT),
    ("Hi Maya, invoice 0001 was due on 2026-11-02.", IT),
    ("Hi Maya, invoice 0001 was due on 02/11/2026 and again on 03/11/2026.", IT),
])
def test_clean_nudge_text_refuses_a_wrong_amount_even_when_its_digits_are_an_invoice_fact(bad, it):
    assert ai.clean_nudge_text(bad, it, 400) is None


@pytest.mark.parametrize("good,it", [
    ("Hi Maya, invoice 0001 for $100.00 was due on November 1, 2026 and is 12 days overdue.", PARTLY),
    ("Hi Maya, invoice #0001 for $100 was due Nov 1 and is 12 days overdue.", PARTLY),
    ("Hi Maya, invoice 0001: USD 100.00 is 12 days overdue.", PARTLY),
    ("Hi Maya, 100 USD was due on 1 November and is 12 days late.", PARTLY),
    ("Hi Maya, 100.00 is still owed (due 2026-11-01).", PARTLY),
    ("Hi Maya, it was due on 11/01/2026 and on the 1st it was still unpaid.", PARTLY),
    ("Hi Maya, invoice 0001 is a 12-day-old overdue payment of $100.00.", PARTLY),
])
def test_clean_nudge_text_still_accepts_the_invoices_own_facts_written_naturally(good, it):
    assert ai.clean_nudge_text(good, it, 400) == good


@pytest.mark.parametrize("tone,text,ok", [
    ("friendly", "Hi Maya, this is your final warning. Pay immediately or else.", False),
    ("friendly", "Hi Maya, this is your final reminder.", False),
    ("friendly", "Hi Maya, please pay immediately.", False),
    ("polite", "Hi Maya, this is the last reminder before I take further action.", False),
    ("polite", "Hi Maya, please pay asap.", False),
    ("firm", "Hi Maya, this is your final notice.", False),
    ("firm", "Hi Maya, there will be consequences.", False),
    ("final", "Hi Maya, there will be consequences.", False),        # no threats, even in the final reminder
    ("final", "Hi Maya, pay or else.", False),
    ("firm", "Hi Maya, no rush, whenever you can.", False),           # too soft for a firm reminder
    ("final", "Hi Maya, no pressure at all.", False),
    ("polite", "Hi Maya, no rush at all.", False),
    ("final", "Hi Maya, this is a final reminder: please pay today.", True),
    ("firm", "Hi Maya, please arrange payment as soon as you can.", True),
    ("friendly", "Hi Maya, no rush, just a friendly heads-up.", True),
])
def test_clean_nudge_text_holds_the_tone_the_code_chose(tone, text, ok):
    it = {**IT, "tone": tone}
    assert (ai.clean_nudge_text(text, it, 400) == text) is ok


def test_the_why_line_is_checked_for_facts_but_not_for_tone():
    friendly = {**IT, "days_overdue": 0, "days_to_due": 4, "due_date": "2026-11-17", "tone": "friendly",
                "amount_raw": "15.00"}
    assert ai.clean_nudge_text("Due in 4 days, 15 USD", friendly, 80, client_facing=False)
    assert ai.clean_nudge_text("Final chance, due in 4 days, 15 USD", friendly, 80, client_facing=False)   # for you, not the client
    assert ai.clean_nudge_text("Final chance, due in 4 days, 15 USD", friendly, 80) is None
    assert ai.clean_nudge_text("Due in 4 days, 150 USD", friendly, 80, client_facing=False) is None


def test_why_text():
    assert ai.why_text(12, -12, "600.00", "USD") == "12 days late, 600 USD"
    assert ai.why_text(1, -1, "15.38", "USD") == "1 day late, 15.38 USD"
    assert ai.why_text(0, 0, "15.30", "USD") == "Due today, 15.30 USD"
    assert ai.why_text(0, 3, "50.00", "USD") == "Due in 3 days, 50 USD"
    assert ai.why_text(0, None, "50.00", "USD") == "Waiting for payment, 50 USD"


def test_fallback_reminder_follows_the_tone_ladder():
    facts = {"number": "0001", "due": "2026-11-01", "amount": "$600.00", "to": "Maya Chen", "days_overdue": 12}
    firm = ai.fallback_reminder(facts, True, tone="firm")
    final = ai.fallback_reminder({**facts, "days_overdue": 30}, True, tone="final")
    polite = ai.fallback_reminder(facts, True, tone="polite")
    friendly = ai.fallback_reminder(facts, False, tone="friendly")
    assert "12 days overdue" in firm["note"] and firm["subject"].startswith("Payment overdue")
    assert "final reminder" in final["note"] and "30 days overdue" in final["note"]
    assert polite["subject"] == "Reminder: invoice 0001 is overdue"
    assert "overdue" not in friendly["note"].lower() and friendly["note"].startswith("Hi Maya,")
    for r in (firm, final, polite, friendly):
        assert "[" not in r["note"] and "fee" not in r["note"].lower()


def test_fallback_reminder_without_a_tone_is_unchanged():
    facts = {"number": "0001", "due": "2026-11-01", "amount": "$600.00", "to": "Maya Chen"}
    assert ai.fallback_reminder(facts, True)["note"] == (
        "Hi Maya, a quick reminder that invoice 0001 for $600.00 was due on November 1, 2026 and is still unpaid. "
        "If you've already paid, thank you and please ignore this note.")


# ---------------------------------------------------------------- QR code parsing

def _multipart(body: bytes, boundary="b0und") -> dict:
    raw = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"\r\nContent-Type: text/plain\r\n\r\n'
           ).encode() + body + f"\r\n--{boundary}--".encode()
    return {"_raw": raw, "_content_type": f"multipart/form-data; boundary={boundary}"}


def _png_of(data_url: str) -> bytes:
    assert data_url.startswith("data:image/png;base64,")
    return base64.b64decode(data_url.split(",", 1)[1])


def test_qr_image_from_the_multipart_body_paypal_sends():
    assert _png_of(paypal.qr_image(_multipart(PNG_B64.encode()))) == PNG


def test_qr_image_from_other_shapes():
    assert _png_of(paypal.qr_image({"_raw": PNG, "_content_type": "image/png"})) == PNG
    assert _png_of(paypal.qr_image(PNG_B64)) == PNG
    assert _png_of(paypal.qr_image("data:image/png;base64," + PNG_B64)) == PNG
    assert _png_of(paypal.qr_image({"image": PNG_B64})) == PNG


@pytest.mark.parametrize("junk", [{}, "", "not base64 at all!", {"_raw": b"<html>oops</html>", "_content_type": "text/html"},
                                  _multipart(b"hello"), _multipart(base64.b64encode(b"not a png")), None, 5])
def test_qr_image_rejects_anything_that_is_not_a_png(junk):
    assert paypal.qr_image(junk) is None
