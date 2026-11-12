# Invoice Autopilot

Type what you did in plain words, for example *"Finished logo for Maya, $450, maya@example.com"*, and Invoice Autopilot turns it into a PayPal invoice. You check it, press send, and PayPal emails your client a button to pay. Later, it can also write a polite payment reminder for you to approve.

## What you need

- A computer (Windows, Mac or Linux) and about 10 minutes
- A free PayPal Developer account (the same login as a normal PayPal account works)
- An Anthropic API key for Claude (a few cents of use)

This app runs in PayPal's **practice (sandbox) mode**: no real money moves and no real customers are emailed.

## Get it running

1. **Download.** On this page click the green **Code** button, then **Download ZIP**. Unzip it (right-click the file, then Extract All or Open). You get a folder called `invoice-autopilot-main`.
2. **Start it.**
   - **Windows:** open the folder and double-click **start.bat**.
   - **Mac:** open the **Terminal** app (press Cmd+Space, type Terminal, press Enter). Type `cd ` (with a space), drag the unzipped folder onto the Terminal window, press Enter, then type `./start.sh` and press Enter.
   - **Linux:** open a terminal in the folder (right-click in the folder, Open in Terminal) and type `./start.sh`.

   The first start installs what the app needs (a helper called uv, which asks before installing) and takes a minute or two. Then your browser opens on its own. Keep the window open while you use the app. Closing it stops the app.
3. **Connect PayPal and Claude.** The page asks for two things and shows how to get each one. The steps are:
   - **PayPal:** go to [developer.paypal.com](https://developer.paypal.com), log in, click **Apps & Credentials**, make sure **Sandbox** is selected, click **Create App**, then copy the **Client ID** and **Secret** and paste them into the page.
   - **Claude:** go to [console.anthropic.com](https://console.anthropic.com), click **API Keys**, then **Create Key**, and paste the key into the page.

   Click **Save and check**. Your keys are saved only on this computer, in a file called `.env`.

## Try it

1. Click **Try an example**, then **Create my invoice**. (Or write your own note.)
2. Check the invoice. Change anything that's wrong. Use the sandbox buyer's email as the client's email (see below) so you can open the result.
3. Click **Review and send**, then **Yes, send it**. The invoice appears under **Your invoices** as *Waiting for payment*. Each invoice has a **View invoice** button that opens the page your client would see.
4. To see the email your client would get: go to [sandbox.paypal.com](https://www.sandbox.paypal.com) and log in as the sandbox buyer. You find that buyer's email and password at developer.paypal.com under **Apps & Credentials > Sandbox > Accounts** (the account of type Personal). Use that email as the client's email when you send.
5. On any unpaid invoice click **Write a reminder**, read and edit the wording, then **Send reminder**.

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
| "Can't reach the app. Is the window you started it from still open?" | The black window you started the app from was closed. Start it again with start.bat or ./start.sh. |
| Window says "Couldn't find a free port" or the page doesn't open | Another copy is already running. Close other copies and try again. The app uses ports 8460 to 8470. |
| "uv not found" or "needs a small helper called uv" | Say `y` when it offers to install it, then close the window and start the app again. Or install it yourself from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/). |
| "I couldn't find an invoice in that note." | Add who it's for, what you did and the price. |

## Judges: fastest path

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/Julian-Schwabecher/invoice-autopilot.git
cd invoice-autopilot
uv run python run.py
```

No uv? Use Python 3.11 or newer with a virtual environment (works on every OS, and avoids the "externally-managed-environment" pip error):

```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

The page opens and asks for your keys, or create the file yourself first:

- Mac/Linux: `cp .env.example .env`
- Windows: `copy .env.example .env`

Then fill in `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (a PayPal **sandbox** app) and `ANTHROPIC_API_KEY`.

## How the AI is used

- **Reading your note.** Claude fills in a structured invoice (client, email, items, prices, due date) using tool use, so the answer comes back as data rather than free text. It is told never to invent an email, price or quantity. When something is missing or unclear it asks a question in a yellow "Check these before sending" box instead of guessing.
- **Writing reminders.** Claude drafts a payment reminder with a tone you can choose (friendly, neutral, firm). It uses only the invoice facts, and never invents fees, deadlines or threats. A not-yet-due invoice gets a friendly heads-up.
- **A person approves everything.** Nothing is sent until you press the confirm button, and every reminder is shown for you to edit before it goes out. Nothing is automatic.

| Tool or API | Used for |
|---|---|
| Anthropic Messages API with tool use, model `claude-sonnet-5-5` (change with `ANTHROPIC_MODEL`) | Reading notes, writing reminders. This model does not allow forcing a specific tool, so the prompt tells it to always call the tool and the app falls back safely if it doesn't. |
| PayPal Invoicing v2 | create invoice, send, search (list), get, remind, delete draft |
| FastAPI, httpx, uv | The local web app, PayPal calls, and one-command setup |

More detail: see [docs/architecture.md](docs/architecture.md).

## Going live

Only do this when you are ready for real invoices. In `.env` set `PAYPAL_BASE=https://api-m.paypal.com` and use the Client ID and Secret from the **Live** tab at developer.paypal.com. **Warning:** after this, invoices go to real customers and real money moves.

## License

[MIT](LICENSE)

---
How it works under the hood: [docs/architecture.md](docs/architecture.md)
