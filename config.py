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

if not ENTITY_API_TOKEN:
    print("⚠️ সতর্কতা: .env ফাইলে Entity Sports REST token পাওয়া যায়নি!")
