import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# .env ফাইল লোড করা (প্রজেক্ট ফোল্ডারের .env আগে, তারপর ডিফল্ট সার্চ)
load_dotenv(BASE_DIR / ".env")
load_dotenv()


def profile_dir(name: str) -> str:
    """ব্রাউজার প্রোফাইল সবসময় প্রজেক্ট ফোল্ডারের ভেতরে থাকবে (যেখান থেকেই চালান)।"""
    return str(BASE_DIR / f"profile_{name}")


# ---------------------------------------------------------------
#  Entity Sports
# ---------------------------------------------------------------
ENTITY_API_TOKEN = (
    os.getenv("ES_REST_TOKEN")
    or os.getenv("ENTITY_API_TOKEN")
    or ""
)
ENTITY_REST_BASE = os.getenv("ES_REST_BASE", "").rstrip("/")
ENTITY_PUSH_TOKEN = os.getenv("ES_PUSH_TOKEN", "")
ENTITY_PUSH_URL = os.getenv(
    "ES_PUSH_URL",
    "ws://webhook.entitysport.com:8087/connect",
).rstrip("/")


# ---------------------------------------------------------------
#  .env helpers
# ---------------------------------------------------------------
def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_number(name, default, minimum=0):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        print(f"⚠️ {name}='{raw}' বোঝা যায়নি, default {default} ব্যবহার হচ্ছে।")
        return default
    if v < minimum:
        print(f"⚠️ {name} {minimum}-এর কম হতে পারবে না, default {default} ব্যবহার হচ্ছে।")
        return default
    return int(v) if v == int(v) else v


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


# ---------------------------------------------------------------
#  Betting
# ---------------------------------------------------------------
BET_STAKE = _read_stake()

# Safety limits (bet_guard.py ব্যবহার করে)। 0 মানে "সীমা নেই"।
MAX_BETS_PER_MATCH = int(_env_number("MAX_BETS_PER_MATCH", 20))
MAX_STAKE_PER_MATCH = _env_number("MAX_STAKE_PER_MATCH", 0)
MIN_ODDS = _env_number("MIN_ODDS", 1.01, minimum=1)

# Entity যদি ওভারের শেষ বলে "N.0" পাঠায় (যেমন 3.6-এর বদলে 4.0), তাহলে ON করুন।
# তখন "N.0" = আগের ওভারের ৬ষ্ঠ বল ধরা হবে। আগে লাইভ পেলোড দেখে নিশ্চিত হয়ে নিন।
OVER_ZERO_IS_LAST_BALL = _env_bool("OVER_ZERO_IS_LAST_BALL", False)


if not ENTITY_API_TOKEN:
    print("⚠️ সতর্কতা: .env ফাইলে Entity Sports REST token পাওয়া যায়নি!")