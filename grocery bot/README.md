# Home Grocery Automation Bot

A Flask backend that automates the weekly home grocery loop:

1. The cook gets a Hindi audio message in the evening listing tomorrow's menu and asking for the grocery list.
2. The cook replies on WhatsApp with a Hindi/English voice note.
3. The bot transcribes the voice note (Whisper), extracts a grocery list (Gemini), and sends you a clean WhatsApp message with one-tap Blinkit and Zepto links.
4. After you place the order, your phone fires a webhook (MacroDroid) and the cook gets a confirmation message in Hindi.

## Endpoints

| Method | Path                | Purpose                                                         |
| ------ | ------------------- | --------------------------------------------------------------- |
| POST   | `/webhook`          | Twilio WhatsApp inbound — voice note → grocery list to you      |
| POST   | `/send-menu-audio`  | Body `{"date":"YYYY-MM-DD"}` — sends Hindi menu audio to cook   |
| POST   | `/order-confirmed`  | MacroDroid trigger — sends order confirmation to cook           |
| GET    | `/health`           | `{"status":"ok"}`                                               |
| GET    | `/audio/<file>`     | Serves generated TTS files so Twilio can fetch them             |

## Setup

### 1. Local

```bash
python -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in values
python app.py
```

The app listens on `PORT` (default `8080`).

### 2. Environment variables

Copy `.env.example` and fill in:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` — from the Twilio console.
- `TWILIO_WHATSAPP_NUMBER` — e.g. `whatsapp:+14155238886` (sandbox) or your business number.
- `MY_WHATSAPP_NUMBER` — your number, formatted `whatsapp:+91XXXXXXXXXX`.
- `COOK_WHATSAPP_NUMBER` — cook's number, same format.
- `HUGGINGFACE_TOKEN` — token with read access to the Inference API.
- `GEMINI_API_KEY` — from Google AI Studio.
- `NOTION_API_KEY` — internal integration token from https://www.notion.so/my-integrations.
- `NOTION_DATABASE_ID` — the database ID (the 32-char hex segment of the database URL). Share the database with your Notion integration.
- `PUBLIC_BASE_URL` — the public URL of this service, e.g. `https://grocery-bot.up.railway.app`. Required so Twilio can fetch the generated audio.

### 3. Notion database

Create a Notion database with these properties:

| Property      | Type      |
| ------------- | --------- |
| Date          | Date      |
| Breakfast     | Rich text |
| Lunch         | Rich text |
| Dinner        | Rich text |
| Special Notes | Rich text |

The `Date` property must be set to a single calendar date (no time, no range). Share the database with your Notion integration so the API can read it.

### 4. Deploy on Railway

1. Push this folder to a GitHub repo.
2. In Railway, **New Project → Deploy from GitHub repo**.
3. Add every variable from `.env.example` under **Variables**.
4. Set `PUBLIC_BASE_URL` to your Railway public domain after the first deploy.
5. Railway will use `railway.json` and run `python app.py`.

### 5. Wire up Twilio

In the Twilio WhatsApp Sandbox (or your number's messaging settings):

- **When a message comes in** → `https://<your-railway-domain>/webhook` (HTTP POST).

### 6. Wire up MacroDroid

Create a macro on your Android phone that fires when you confirm a Blinkit/Zepto order, with an HTTP Request action:

- URL: `https://<your-railway-domain>/order-confirmed`
- Method: `POST`

### 7. Schedule the evening menu audio

Use Railway Cron, or any scheduler, to call `/send-menu-audio` once per evening with tomorrow's date:

```bash
curl -X POST https://<your-railway-domain>/send-menu-audio \
  -H "Content-Type: application/json" \
  -d '{"date":"2026-05-02"}'
```

## Notes

- Generated TTS audio is written to a temp directory and served from `/audio/<file>`. On Railway, this is ephemeral storage — fine because Twilio fetches the file within seconds of the send.
- All external calls (Twilio, Hugging Face, Gemini, Notion) are wrapped in `try/except` and log full tracebacks to stdout. Tail logs with `railway logs`.
