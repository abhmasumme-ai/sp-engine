import re

# Wicket: "retired hurt" ধরা হয় না (ডিসমিসাল নয়); শুধু "retired out" ধরা হয়।
_WICKET_RE = re.compile(
    r"^(wicket|out|w)(?:[.!]|\s|$)"
    r"|^(bowled|lbw|caught|c\s*&\s*b|run\s*out|stumped?|hit\s*wicket|handled|obstruct)"
    r"|^retired\s*out"
)
_EXTRAS_RE = re.compile(r"^(no[\s_-]?ball|nb|wide|wd)\b")
_DOT_RE = re.compile(r"^(dot|0)\b|^no[\s_-]?run\b")
_FOUR_RE = re.compile(r"^(four|4)\b")
_SIX_RE = re.compile(r"^(six|6)\b")
_RUN_RE = re.compile(r"^\d+\b")


def classify_entity_event(event_text) -> str:
    """Entity ইভেন্ট টেক্সট → wicket | other | dot | four | six | run | intermediate"""
    ev = str(event_text or "").strip().lower()
    if _WICKET_RE.match(ev):
        return "wicket"
    if _EXTRAS_RE.match(ev):
        return "other"
    if _DOT_RE.match(ev):
        return "dot"
    if _FOUR_RE.match(ev):
        return "four"
    if _SIX_RE.match(ev):
        return "six"
    if _RUN_RE.match(ev):
        return "run"
    return "intermediate"