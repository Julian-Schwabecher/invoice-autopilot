# Invoice Autopilot

An AI agent for freelancers that turns a messy note into a real PayPal invoice, then chases it when it goes late.

**PayPal:** Invoicing v2 API (sandbox): create, send, search, remind.
**AI:** Claude (Anthropic API). Forced tool-use extracts a structured invoice from free text and refuses to invent missing data. It also writes reminder emails whose tone scales with days overdue. A human approves every send.

## Run it
1. Create a PayPal Developer sandbox app (https://developer.paypal.com) and copy its Client ID and Secret.
2. `cp .env.example .env`, fill in the three values (`ANTHROPIC_API_KEY` too).
3. `uv sync && set -a && . ./.env && set +a && uv run uvicorn app.main:app --port 8460`
4. Open http://localhost:8460. Paste a note, click Draft, then Send. The invoice appears in the list. Use a sandbox buyer email so you can see it in the sandbox inbox.

## Tools used
Claude Sonnet via the Anthropic SDK (extraction + reminder copywriting); PayPal Invoicing v2 REST API; FastAPI.
