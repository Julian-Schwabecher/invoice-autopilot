"""Claude does two jobs: parse a messy request into an invoice, and write nudges."""
import json, os, anthropic

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
_c = None


def client():
    global _c
    _c = _c or anthropic.Anthropic()
    return _c


INVOICE_TOOL = {
    "name": "draft_invoice",
    "description": "Record the invoice extracted from the user's message.",
    "input_schema": {
        "type": "object",
        "required": ["client_name", "client_email", "items"],
        "properties": {
            "client_name": {"type": "string"},
            "client_email": {"type": "string"},
            "currency": {"type": "string", "description": "ISO 4217, default USD"},
            "due_days": {"type": "integer", "enum": [10, 15, 30, 45, 60, 90]},
            "note": {"type": "string", "description": "Short friendly note to the client"},
            "items": {"type": "array", "items": {
                "type": "object", "required": ["name", "qty", "unit_price"],
                "properties": {"name": {"type": "string"}, "qty": {"type": "number"},
                               "unit_price": {"type": "number"}}}},
            "missing": {"type": "array", "items": {"type": "string"},
                        "description": "Facts needed but not in the message (never invent an email)"},
        },
    },
}


def draft_from_text(text: str) -> dict:
    r = client().messages.create(
        model=MODEL, max_tokens=1024, tools=[INVOICE_TOOL],
        tool_choice={"type": "auto"},
        messages=[{"role": "user", "content":
                   "Call the draft_invoice tool with an invoice extracted from this freelancer's note. Do not invent an email "
                   "address or prices; list anything absent under `missing`.\n\n" + text}])
    return next(b.input for b in r.content if b.type == "tool_use")


def write_nudge(inv: dict, days_overdue: int) -> dict:
    tone = ("a warm, no-pressure heads-up" if days_overdue <= 3 else
            "polite but clear" if days_overdue <= 14 else "firm and professional, mention next steps")
    r = client().messages.create(
        model=MODEL, max_tokens=500,
        messages=[{"role": "user", "content":
                   f"Write a payment reminder email ({tone}). Invoice facts: "
                   f"{json.dumps(inv)[:1500]}. Days overdue: {days_overdue}. "
                   'Reply as JSON only: {"subject": "...", "note": "..."} (note under 400 chars, no placeholders).'}])
    txt = r.content[0].text.strip().strip("`")
    txt = txt[txt.find("{"):txt.rfind("}") + 1]
    return json.loads(txt)
