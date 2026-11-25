import os
from dotenv import load_dotenv

# .env ফাইল লোড করা
load_dotenv()

ENTITY_API_TOKEN = (
    os.getenv("ES_REST_TOKEN")
    or os.getenv("ENTITY_API_TOKEN")
    or ""
)
ENTITY_API_SECRET = (
    os.getenv("ES_PUSH_TOKEN")
    or os.getenv("ENTITY_API_SECRET")
    or ""
)
ENTITY_MATCH_ID = os.getenv("ENTITY_MATCH_ID", "")
ENTITY_REST_BASE = os.getenv("ES_REST_BASE", "").rstrip("/")
ENTITY_PUSH_TOKEN = os.getenv("ES_PUSH_TOKEN", "")
ENTITY_PUSH_URL = os.getenv(
    "ES_PUSH_URL",
    "ws://webhook.entitysport.com:8087/connect",
).rstrip("/")


def _read_stake(default=15):
    """BET_STAKE .env থেকে পড়ে। ভুল বা শূন্য মান হলে default (15) ব্যবহার হয়।"""
    raw = os.getenv("BET_STAKE", "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        print(f"⚠️ BET_STAKE='{raw}' বোঝা যায়নি, default {default} ব্যবহার হচ্ছে।")
        return default
    if v <= 0:
        print(f"⚠️ BET_STAKE অবশ্যই ০-এর বেশি হতে হবে, default {default} ব্যবহার হচ্ছে।")
        return default
    return int(v) if v == int(v) else v


BET_STAKE = _read_stake()

if not ENTITY_API_TOKEN:
    print("⚠️ সতর্কতা: .env ফাইলে Entity Sports REST token পাওয়া যায়নি!")
