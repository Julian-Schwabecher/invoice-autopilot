# Invoice Autopilot

**Type what you did in plain words. Claude turns it into a PayPal invoice, you check it, and PayPal emails your client a button to pay.**
**Then Claude tells you which unpaid invoices to chase today and writes each reminder. You approve every one.**
**Built for a freelancer who is not technical: no accounting software, no forms, and nothing is sent without a click.**

Built for the "Build What's Next with PayPal and AI" hackathon. It runs on PayPal's practice (sandbox) system, so no real money moves and no real customers are emailed.

## What it does

1. **Invoice from a sentence.** *"Finished logo for Maya, $450, maya@example.com"* becomes a filled-in invoice. Anything missing or unclear comes back as a question, not a guess.
2. **Check, then send.** You see an editable form and a plain-English confirmation. Optional: ask for a **deposit** (25% or 50%) and add **late payment wording** to the terms. PayPal then emails the invoice and shows a **Pay by QR** code.
3. **Live status.** The page refreshes itself every 8 seconds while it is open. When an invoice is paid you get a short message ("Maya Chen just paid invoice 0001") and the card moves to *Paid and closed*. Anything you were typing stays where it is.
4. **Who to chase today.** Claude ranks the unpaid invoices, says why in a few words, and drafts a reminder for each, firmer the later the payment is. Each one has **Approve & send** and **Skip**. Nothing goes out until you click.
5. **Preview only.** One switch, "Preview only: don't send anything", makes the app show exactly what it would have sent (in plain words, plus the exact PayPal request for the curious) without contacting anyone. It covers every PayPal write: invoices, reminders and the practice payment. (It only reads from PayPal to describe what would happen.)
6. **Activity.** A list of what the app has actually sent.
7. **Practice helpers** (sandbox only): **Try a demo** sends one demo invoice, and **Mark as paid (test)** pretends the client paid so you can watch the status change.

## Where PayPal is used

Everything that touches money or the client goes through PayPal Invoicing v2. The app stores no invoices of its own.

| What the person sees | PayPal call |
|---|---|
| Invoice created and emailed | `POST /v2/invoicing/invoices` then `POST .../send` (the draft is deleted if sending fails) |
| Deposit | `configuration.partial_payment` with `minimum_amount_due` on the invoice |
| Late payment wording | `detail.terms_and_conditions`. PayPal never applies a late fee by itself (see limitations) |
| Your invoices, live status | `POST /v2/invoicing/search-invoices` every 8 seconds, plus `GET .../invoices/{id}` |
| Reminders | `POST .../invoices/{id}/remind`. PayPal's documentation says an API caller can send only 2 reminders a day, so the app sends at most 2 per invoice per day and says so in plain English (see limitations) |
| Pay by QR | `POST .../invoices/{id}/generate-qr-code` |
| Mark as paid (test), sandbox only | `POST .../invoices/{id}/payments` with method `CASH` |
| Instant paid notices (optional) | PayPal webhook to `/api/webhooks/paypal`, checked with `POST /v1/notifications/verify-webhook-signature` |

## Where AI is essential

Claude does the two jobs a form cannot do. The code around it decides what is allowed.

- **Reading a messy note.** Free text in, a structured invoice out (tool use, so the answer is data, not prose). It is told never to invent an email, price or quantity; gaps become questions in a yellow "Check these before sending" box.
- **Deciding who to chase and what to say.** One Claude call looks at every unpaid invoice together, ranks them (1 = chase first), gives a short reason, and writes a subject and message for each.
- **Guardrails written in code, not hoped for in a prompt.**
  - The *tone* comes from the days late (on time: friendly, 1 to 7 days: polite, 8 to 21: firm, more than 21: final). Claude cannot change it.
  - Claude's wording is checked per invoice. It is thrown away if it has a link, an email address or a placeholder; an amount that is not exactly what is still owed on that invoice (a number that merely shares a digit with the invoice number or date does not count, and amounts written in words are refused); a date other than the due date; a number of days other than the real one; a threat of late fees, interest or legal action; wording harsher or softer than the tone the code chose (for example "final warning" on a friendly reminder); or a not-yet-due invoice called late. That invoice then gets a plain built-in reminder instead. Claude's *order* (who to chase first) is its own judgement: the code only makes sure it is a clean 1 to N.
  - If Claude is down or answers badly, every invoice still gets a built-in reminder, so the page never breaks.
  - Invoice and client names are passed as data, and the prompt says to ignore instructions inside them.
- **A person approves everything.** Claude only drafts. Sending is always a click, and it goes through the same check as any other reminder.

## Run it

You need a computer (Windows, Mac or Linux), about 10 minutes, a free PayPal Developer account, and an Anthropic API key (a few cents of use).

1. **Download.** On the GitHub page click the green **Code** button, then **Download ZIP**, and unzip it.
2. **Start it.**
   - **Windows:** double-click **start.bat**.
   - **Mac:** open **Terminal**, type `cd ` (with a space), drag the unzipped folder onto the window, press Enter, then type `./start.sh` and press Enter.
   - **Linux:** open a terminal in the folder and type `./start.sh`.

   The first start installs what the app needs (a helper called uv, which asks before installing) and takes a minute or two. Then your browser opens. Keep the window open while you use the app.
3. **Connect PayPal and Claude.** The page asks for two things and shows how to get each:
   - **PayPal:** [developer.paypal.com](https://developer.paypal.com), log in, **Apps & Credentials**, make sure **Sandbox** is selected, **Create App**, then copy the **Client ID** and **Secret**.
   - **Claude:** [console.anthropic.com](https://console.anthropic.com), **API Keys**, **Create Key**.

   Click **Save and check**. Your keys are saved only on this computer, in a file called `.env`.
4. **Use the PayPal test buyer's email** as your client's email so you can open the result: at developer.paypal.com go to **Apps & Credentials > Sandbox > Accounts** and use the *Personal* account's email. (**Try a demo** already uses it.)

For judges, with Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```
git clone https://github.com/Julian-Schwabecher/invoice-autopilot.git
cd invoice-autopilot
uv run python run.py
```

No uv? Use a virtual environment (works everywhere and avoids the "externally-managed-environment" pip error):

```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Instead of the page asking, you can create `.env` yourself (`cp .env.example .env`, or `copy .env.example .env` on Windows) and fill in `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (a PayPal **sandbox** app) and `ANTHROPIC_API_KEY`.

Optional settings in `.env` (all explained in `.env.example`):

| Setting | What it does |
|---|---|
| `PAYPAL_WEBHOOK_ID` | Optional. Lets PayPal tell the app the moment an invoice is paid. It needs a public HTTPS address that PayPal can reach (see limitations). Without it the page simply checks every 8 seconds, and the webhook address answers "Webhooks aren't set up". |
| `SAFE_MODE=1` | Starts the app with "Preview only" already on. You can also flip it in the page. |
| `ANTHROPIC_MODEL` | Use a different Claude model. The default is `claude-sonnet-5-5`. |

Run the tests (no keys and no network needed):

```
uv run --group dev pytest -q
```

## Demo script (under 3 minutes)

Have the app running and keys saved. The click path:

| Time | Click | What you see |
|---|---|---|
| 0:00 | Open the page | The blue **Practice mode** banner and the **Preview only** switch. **Who to chase today** starts loading by itself. |
| 0:15 | **Try an example**, then **Create my invoice** | In about 5 seconds Claude fills in the invoice: Sam Lee, 3 hours at $50, due in 14 days. |
| 0:35 | Under **Ask for a deposit** click **50%**, type a line in **Late payment wording**, click **Review and send** | "Your client can pay $75.00 first". The confirmation repeats the deposit and the wording. |
| 0:55 | Turn on **Preview only: don't send anything**, click **Show me the preview** | "Preview only: nothing was sent", then what PayPal would have been asked to send, in plain words. |
| 1:15 | Turn the switch off, click **Yes, send it** | **Practice invoice created**, the deposit amount, and a **Pay by QR** code. |
| 1:35 | Scroll to **Who to chase today**, click **Check again** | Unpaid invoices ranked 1, 2, 3, each with a one-line reason and an editable subject and message. Invoices you just made are not late yet, so they show **Friendly tone** and **Not late yet**. The tone gets firmer only once an invoice is past its due date (**Polite**, then **Firm**, then **Final reminder**), and this app cannot back-date an invoice, so you will see the firmer wording only when a real invoice runs late. |
| 2:00 | Edit one message, click **Approve & send** on it, click **Skip** on another | "Reminder emailed to ..." and that card's send button turns off. Reload the page and it says "Already reminded today". |
| 2:20 | Under **Your invoices** click **Mark as paid (test)** on the new invoice, then **Yes, mark it as paid** | A green message "Sam Lee just paid invoice 000X (practice payment)" and the card moves to **Paid and closed**. |
| 2:40 | Scroll to **Activity** | The invoice and the reminder, with times. |

In a hurry? **Try a demo** does the invoice and QR in one click.

## Tools used

- **Claude** through the Anthropic Messages API (Python SDK) with tool use. Default model `claude-sonnet-5-5`.
- **PayPal Invoicing v2** (sandbox) for invoices, reminders, payments, QR codes. **PayPal Webhooks** (optional) for instant status.
- **FastAPI**, **uvicorn** and **httpx** for the local app. **uv** for one-command setup.
- One plain HTML page with vanilla JavaScript: no framework, no build step, no outside scripts or fonts.
- **pytest** (offline: the tests block all network access) and **GitHub Actions** to run them on every push.

More detail: [docs/architecture.md](docs/architecture.md).

## Honest limitations

- **No automatic late fees.** PayPal does not add late fees to invoices. The "Late payment wording" box only prints your words in the invoice terms, and any fee would be yours to ask for. Claude is not allowed to mention late fees in reminders.
- **Recurring invoices are not supported.** Every invoice is created by you, one at a time.
- **The app sends at most 2 reminders per invoice per day.** PayPal's documentation says an API caller can send only 2 reminders in a day. PayPal's practice (sandbox) system accepted more than 2 when we tried, so this limit is the app's own rule, kept so your client is never pestered and you stay inside what PayPal documents. It only knows about reminders it sent itself since it started, so one sent from PayPal's own website is not counted.
- **Instant paid notices need a public web address.** PayPal cannot call `localhost`. To use `PAYPAL_WEBHOOK_ID` you must expose the app's `/api/webhooks/paypal` address yourself (for example with a tunnel) and register it at developer.paypal.com. The signature check is covered by offline tests with PayPal faked; it has not been tried against a real PayPal webhook. Without it, the 8-second refresh does the same job a few seconds later.
- **Mark as paid (test) is practice only.** It records a cash payment through PayPal's sandbox. It refuses to run against live PayPal, and with **Preview only** on it only shows the request instead of making it.
- **Some things live in memory and reset when the app restarts:** the Activity list, the Preview-only switch (unless `SAFE_MODE=1`), webhook events, and the remembered demo invoice.
- **Reminder drafts are cached for 10 minutes** so reloading the page does not cost another Claude call. **Check again** always asks again.
- The list shows your 25 most recent invoices. One currency per invoice. No tax, discount or tip fields yet (Claude asks about them instead).
- **Local only.** The app has no login and listens on your computer only. Do not expose it to a network. The one deliberate exception, the webhook address, believes nothing PayPal has not signed.
- Currency amounts follow PayPal's rules (no cents for JPY, HUF and TWD).

## If something goes wrong

| What you see | What to do |
|---|---|
| "PayPal didn't accept the keys." | Click **Change my keys** at the bottom of the page. Copy the Client ID and Secret again from the **Sandbox** tab (not Live). |
| "PayPal didn't accept that Client ID and Secret." | Same as above. Make sure you copied both, with no extra spaces. |
| "Claude didn't accept that key." or "The Claude key isn't working." | Create a new key at console.anthropic.com > API Keys, then click **Change my keys** and paste it in. |
| "Your Claude account has no credit left." | A new Anthropic account needs a few dollars of credit first. Add some at console.anthropic.com under Plans & Billing, then try again. |
| "The Claude model this app asks for isn't available to your account." | Remove the `ANTHROPIC_MODEL` line from the `.env` file (or set it to a model you can use) and start the app again. |
| "The AI is busy right now. Try again in a moment." | Claude is overloaded for a moment. Wait a minute and press **Try again**. |
| "PayPal is busy." or "Couldn't reach PayPal." | Check your internet, wait a minute, press **Try again**. Nothing is sent when you see an error. |
| "To stay within PayPal's daily reminder limit, this app sends at most two reminders..." | The app's own daily limit, set to match what PayPal documents. Try again tomorrow. |
| "Can't reach the app. Is the window you started it from still open?" | The black window you started the app from was closed. Start it again with start.bat or ./start.sh. |
| Window says "Couldn't find a free port" or the page doesn't open | Another copy is already running. Close other copies and try again. The app uses ports 8460 to 8470. |
| "uv not found" or "needs a small helper called uv" | Say `y` when it offers to install it, then close the window and start the app again. Or install it yourself from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/). |
| "I couldn't find an invoice in that note." | Add who it's for, what you did and the price. |

## Going live

Only do this when you are ready for real invoices. In `.env` set `PAYPAL_BASE=https://api-m.paypal.com` and use the Client ID and Secret from the **Live** tab at developer.paypal.com. **Warning:** after this, invoices go to real customers and real money moves. **Try a demo** and **Mark as paid (test)** switch themselves off in live mode, and **Preview only** is a good first step.

## License

[MIT](LICENSE)
