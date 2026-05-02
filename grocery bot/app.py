import os
import json
import logging
import tempfile
import urllib.parse
import uuid
from datetime import datetime

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioRestException
from gtts import gTTS
from notion_client import Client as NotionClient
from google import genai

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("grocery-bot")

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")
MY_WHATSAPP_NUMBER = os.environ.get("MY_WHATSAPP_NUMBER")
COOK_WHATSAPP_NUMBER = os.environ.get("COOK_WHATSAPP_NUMBER")
HUGGINGFACE_TOKEN = os.environ.get("HUGGINGFACE_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")
HF_WHISPER_URL = "https://api-inference.huggingface.co/models/openai/whisper-large-v3"

AUDIO_DIR = os.path.join(tempfile.gettempdir(), "grocery_bot_audio")
os.makedirs(AUDIO_DIR, exist_ok=True)


def get_twilio_client():
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials are not configured.")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def get_notion_client():
    if not NOTION_API_KEY:
        raise RuntimeError("NOTION_API_KEY not configured.")
    return NotionClient(auth=NOTION_API_KEY)


def fetch_menu_for_date(date_str):
    """Return dict with breakfast/lunch/dinner for the given YYYY-MM-DD row."""
    if not NOTION_DATABASE_ID:
        raise RuntimeError("NOTION_DATABASE_ID not configured.")
    notion = get_notion_client()
    response = notion.databases.query(
        database_id=NOTION_DATABASE_ID,
        filter={"property": "date", "title": {"equals": date_str}},
        page_size=1,
    )
    results = response.get("results", [])
    if not results:
        return None
    row = results[0]

    try:
        date_value = row["properties"]["date"]["title"][0]["text"]["content"]
    except (KeyError, IndexError, TypeError):
        date_value = ""
    try:
        breakfast = row["properties"]["breakfast"]["rich_text"][0]["text"]["content"]
    except (KeyError, IndexError, TypeError):
        breakfast = ""
    try:
        lunch = row["properties"]["lunch"]["rich_text"][0]["text"]["content"]
    except (KeyError, IndexError, TypeError):
        lunch = ""
    try:
        dinner = row["properties"]["dinner"]["rich_text"][0]["text"]["content"]
    except (KeyError, IndexError, TypeError):
        dinner = ""

    return {
        "date": date_value,
        "breakfast": breakfast,
        "lunch": lunch,
        "dinner": dinner,
    }


def download_twilio_media(media_url):
    """Twilio media URLs require basic auth with the account SID/token."""
    resp = requests.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def transcribe_with_whisper(audio_bytes, content_type="audio/ogg"):
    if not HUGGINGFACE_TOKEN:
        raise RuntimeError("HUGGINGFACE_TOKEN not configured.")
    headers = {
        "Authorization": f"Bearer {HUGGINGFACE_TOKEN}",
        "Content-Type": content_type,
    }
    params = {"language": "hi", "task": "transcribe"}
    resp = requests.post(
        HF_WHISPER_URL,
        headers=headers,
        params=params,
        data=audio_bytes,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "text" in data:
        return data["text"]
    if isinstance(data, list) and data and "text" in data[0]:
        return data[0]["text"]
    raise ValueError(f"Unexpected Whisper response: {data}")


def extract_grocery_list(text):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not configured.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "Extract a grocery list from this Hindi/English text from a home cook.\n"
        "Return ONLY a JSON array:\n"
        '[{"item": "tomatoes", "quantity": "500g", "hindi": "tamatar"}, ...]\n'
        "If quantity not mentioned, use 'as needed'.\n\n"
        f"Text: {text}"
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    raw = (response.text or "").strip()
    # Strip Markdown code fences if Gemini wrapped the JSON.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"Gemini did not return a JSON array: {response.text}")
    return json.loads(raw[start : end + 1])


ITEM_EMOJIS = {
    "tomato": "🍅", "tamatar": "🍅",
    "onion": "🧅", "pyaaz": "🧅", "pyaz": "🧅",
    "potato": "🥔", "aloo": "🥔",
    "carrot": "🥕", "gajar": "🥕",
    "milk": "🥛", "doodh": "🥛",
    "egg": "🥚", "anda": "🥚",
    "bread": "🍞",
    "rice": "🍚", "chawal": "🍚",
    "chili": "🌶️", "mirchi": "🌶️", "mirch": "🌶️",
    "lemon": "🍋", "nimbu": "🍋",
    "garlic": "🧄", "lehsun": "🧄",
    "ginger": "🫚", "adrak": "🫚",
    "banana": "🍌", "kela": "🍌",
    "apple": "🍎", "seb": "🍎",
}


def emoji_for(item_name):
    name = (item_name or "").lower()
    for key, emoji in ITEM_EMOJIS.items():
        if key in name:
            return emoji
    return "🛒"


def format_grocery_message(items):
    lines = ["🛒 *Grocery List*", ""]
    for entry in items:
        item = (entry.get("item") or "").strip()
        quantity = (entry.get("quantity") or "as needed").strip()
        hindi = (entry.get("hindi") or "").strip()
        if not item:
            continue
        emoji = emoji_for(item) + " " + emoji_for(hindi) if hindi else emoji_for(item)
        # Keep it tidy — just the item emoji.
        emoji = emoji_for(item)
        label = f"{item}"
        if hindi:
            label += f" ({hindi})"
        encoded = urllib.parse.quote_plus(item)
        blinkit = f"https://blinkit.com/s/?q={encoded}"
        zepto = f"https://www.zeptonow.com/search?q={encoded}"
        lines.append(f"{emoji} {label} — {quantity}")
        lines.append(f"   • Blinkit: {blinkit}")
        lines.append(f"   • Zepto: {zepto}")
        lines.append("")
    return "\n".join(lines).strip()


def send_whatsapp_text(to_number, body):
    client = get_twilio_client()
    return client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to_number,
        body=body,
    )


def send_whatsapp_audio(to_number, media_url, body=""):
    client = get_twilio_client()
    return client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER,
        to=to_number,
        body=body,
        media_url=[media_url],
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    """Twilio WhatsApp inbound webhook — handles audio grocery requests."""
    try:
        form = request.form
        num_media = int(form.get("NumMedia", "0") or 0)
        body_text = form.get("Body", "") or ""

        transcript = None
        if num_media > 0:
            media_url = form.get("MediaUrl0")
            content_type = form.get("MediaContentType0", "audio/ogg")
            if not media_url:
                return jsonify({"error": "No media URL"}), 400

            try:
                audio_bytes = download_twilio_media(media_url)
            except requests.RequestException as e:
                logger.exception("Failed to download Twilio media: %s", e)
                return jsonify({"error": "media download failed"}), 502

            try:
                transcript = transcribe_with_whisper(audio_bytes, content_type)
                logger.info("Whisper transcript: %s", transcript)
            except (requests.RequestException, ValueError) as e:
                logger.exception("Whisper transcription failed: %s", e)
                return jsonify({"error": "transcription failed"}), 502
        elif body_text.strip():
            transcript = body_text
        else:
            return jsonify({"error": "no audio or text provided"}), 400

        try:
            items = extract_grocery_list(transcript)
        except (json.JSONDecodeError, ValueError, RuntimeError) as e:
            logger.exception("Gemini extraction failed: %s", e)
            return jsonify({"error": "grocery extraction failed"}), 502

        message_body = format_grocery_message(items)

        try:
            send_whatsapp_text(MY_WHATSAPP_NUMBER, message_body)
        except TwilioRestException as e:
            logger.exception("Twilio send failed: %s", e)
            return jsonify({"error": "whatsapp send failed"}), 502

        return jsonify({"ok": True, "items": items})
    except Exception as e:
        logger.exception("Unhandled error in /webhook: %s", e)
        return jsonify({"error": "internal error"}), 500


@app.route("/send-menu-audio", methods=["POST"])
def send_menu_audio():
    try:
        payload = request.get_json(silent=True) or {}
        date_str = payload.get("date")
        if not date_str:
            return jsonify({"error": "missing 'date'"}), 400
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400

        try:
            menu = fetch_menu_for_date(date_str)
        except Exception as e:
            logger.exception("Notion read failed: %s", e)
            return jsonify({"error": "notion read failed"}), 502

        if not menu:
            return jsonify({"error": f"no menu row for {date_str}"}), 404

        hindi_text = (
            f"Kal banana hai: Subah {menu['breakfast']}, "
            f"Dopahar {menu['lunch']}, Raat ko {menu['dinner']}. "
            "Kripya kal sham 8 baje tak grocery list bhejein."
        )

        try:
            tts = gTTS(text=hindi_text, lang="hi")
            filename = f"menu_{date_str}_{uuid.uuid4().hex[:8]}.mp3"
            file_path = os.path.join(AUDIO_DIR, filename)
            tts.save(file_path)
        except Exception as e:
            logger.exception("gTTS failed: %s", e)
            return jsonify({"error": "tts failed"}), 502

        if not PUBLIC_BASE_URL:
            logger.error("PUBLIC_BASE_URL not set — Twilio cannot fetch the audio.")
            return jsonify({"error": "PUBLIC_BASE_URL not configured"}), 500

        media_url = f"{PUBLIC_BASE_URL.rstrip('/')}/audio/{filename}"

        try:
            send_whatsapp_audio(COOK_WHATSAPP_NUMBER, media_url, body="Kal ka menu")
        except TwilioRestException as e:
            logger.exception("Twilio audio send failed: %s", e)
            return jsonify({"error": "whatsapp send failed"}), 502

        return jsonify({"ok": True, "audio_url": media_url, "text": hindi_text})
    except Exception as e:
        logger.exception("Unhandled error in /send-menu-audio: %s", e)
        return jsonify({"error": "internal error"}), 500


@app.route("/order-confirmed", methods=["POST"])
def order_confirmed():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            menu = fetch_menu_for_date(today)
        except Exception as e:
            logger.exception("Notion read failed: %s", e)
            return jsonify({"error": "notion read failed"}), 502

        if not menu:
            return jsonify({"error": f"no menu row for {today}"}), 404

        message = (
            "Groceries order ho gaya! Aa rahe hain 10-15 min mein. "
            f"Aaj ka khana - Subah: {menu['breakfast']}, "
            f"Dopahar: {menu['lunch']}, Raat: {menu['dinner']}"
        )

        try:
            send_whatsapp_text(COOK_WHATSAPP_NUMBER, message)
        except TwilioRestException as e:
            logger.exception("Twilio send failed: %s", e)
            return jsonify({"error": "whatsapp send failed"}), 502

        return jsonify({"ok": True, "message": message})
    except Exception as e:
        logger.exception("Unhandled error in /order-confirmed: %s", e)
        return jsonify({"error": "internal error"}), 500


@app.route("/audio/<path:filename>", methods=["GET"])
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, mimetype="audio/mpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
