import asyncio
import gzip
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request as URLRequest, urlopen

import psutil
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from patchright.async_api import async_playwright
import uvicorn
import websockets

from bet_guard import guard
from config import (
    ENTITY_API_TOKEN,
    ENTITY_PUSH_TOKEN,
    ENTITY_PUSH_URL,
    ENTITY_REST_BASE,
    OVER_ZERO_IS_LAST_BALL,
    profile_dir,
)
from event_utils import classify_entity_event
from melbet_engine import MelbetEngine
from delivery_worker import DeliveryWorker
from batsman_worker import BatsmanWorker
from wicket_worker import WicketWorker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # startup
    global entity_push_task
    if not ENTITY_PUSH_TOKEN:
        entity_push_state["status"] = "missing_token"
    elif entity_push_task is None or entity_push_task.done():
        entity_push_task = asyncio.create_task(entity_push_listener())

    if ENTITY_API_TOKEN and ENTITY_REST_BASE:
        _spawn(_prefetch_fixtures())

    yield

    # shutdown
    if entity_push_task is not None and not entity_push_task.done():
        entity_push_task.cancel()
        await asyncio.gather(entity_push_task, return_exceptions=True)
    for kind, wc in list(worker_ctxs.items()):
        try:
            await wc.close()
        except Exception:
            pass
        worker_ctxs.pop(kind, None)
    if ctx is not None:
        try:
            await ctx.close()
        except Exception:
            pass
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


app = FastAPI(title="Melbet Bot Dashboard", lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent


# ============================================================
#  Browser config
# ============================================================
BROWSER_LIGHT_ARGS = [
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    "--disable-plugins-discovery", "--disable-component-update",
    "--disable-background-networking", "--disable-sync", "--disable-default-apps",
    "--disable-breakpad", "--disable-crash-reporter",
    "--disable-features=Translate,MediaRouter,OptimizationHints,NetworkServiceInProcess2",
    "--metrics-recording-only", "--mute-audio", "--disable-notifications",
    "--disable-popup-blocking", "--disable-dev-shm-usage",
    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
]


def browser_args(position="0,0", size="1100,720"):
    args = list(BROWSER_LIGHT_ARGS) + [f"--window-size={size}", f"--window-position={position}"]
    if os.getenv("DISABLE_GPU", "false").lower() in ("1", "true", "yes", "on"):
        args += ["--disable-gpu", "--disable-software-rasterizer"]
    return args


def worker_headless() -> bool:
    return os.getenv("WORKER_HEADLESS", "false").lower() in ("1", "true", "yes", "on")


async def lower_chrome_priority(delay=2.5):
    await asyncio.sleep(delay)
    try:
        me = psutil.Process(os.getpid())
        for child in me.children(recursive=True):
            try:
                name = child.name().lower()
                if "chrome" in name or "chromium" in name:
                    try:
                        if os.name == "nt":
                            child.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
                        else:
                            child.nice(10)
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        pass
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
    except Exception as e:
        print(f"[priority] skipped: {e}")


async def is_logged_in(target_page) -> bool:
    try:
        return await target_page.evaluate(
            "() => { try { return parseInt(localStorage.getItem('userId')) > 0; } catch(e) { return false; } }"
        )
    except Exception:
        return False


# ============================================================
#  Background task helper
# ============================================================
_bg_tasks: set = set()


def _spawn(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# ============================================================
#  Connection manager
# ============================================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active_connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active_connections:
            self.active_connections.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for c in list(self.active_connections):
            try:
                await c.send_json(message)
            except Exception:
                dead.append(c)
        for c in dead:
            self.disconnect(c)


manager = ConnectionManager()

# --- Playwright / state ---
pw = None
ctx, page = None, None
engine = None
worker_delivery, worker_batsman, worker_wicket = None, None, None

worker_ctxs: dict[str, object] = {}
worker_pages: dict[str, object] = {}
worker_engines: dict[str, MelbetEngine] = {}

current_match_id = None
detected_match_id = None
locked_melbet_id = None
active_entity_match_id = None
verified_match_url = None

entity_push_task = None
armed_workers: set = set()
entity_fixture_cache: dict[str, dict] = {}
entity_last_state: dict[str, dict] = {}
preview_entity_match_id = None
_last_seen_entity_mid = None
_entity_debug_log = deque(maxlen=20)

entity_push_state = {
    "status": "disconnected", "messages": 0,
    "last_message_at": None, "last_error": None,
}
_processed_event_keys = deque()
_processed_event_key_set = set()
_MAX_PROCESSED_EVENTS = 2000


# ============================================================
#  Browser lifecycle helpers
# ============================================================
def _reset_main_state():
    global ctx, page, engine
    global detected_match_id, locked_melbet_id
    global current_match_id, active_entity_match_id, verified_match_url
    global preview_entity_match_id, _last_seen_entity_mid
    global worker_delivery, worker_batsman, worker_wicket

    ctx = None
    page = None
    engine = None
    detected_match_id = None
    locked_melbet_id = None
    current_match_id = None
    active_entity_match_id = None
    verified_match_url = None
    preview_entity_match_id = None
    _last_seen_entity_mid = None
    worker_delivery = worker_batsman = worker_wicket = None
    armed_workers.clear()


def _reset_worker_state(kind: str):
    worker_ctxs.pop(kind, None)
    worker_pages.pop(kind, None)
    worker_engines.pop(kind, None)
    armed_workers.discard(kind)


async def _main_alive() -> bool:
    if ctx is None:
        return False
    if page is not None and page.is_closed():
        return False
    try:
        if page is not None:
            await asyncio.wait_for(page.title(), timeout=2.0)
            return True
    except Exception:
        return False
    try:
        return bool(ctx.pages)
    except Exception:
        return False


def _bind_main_close_handlers():
    if ctx is None:
        return

    async def _on_close(_=None):
        print("⚠️ Main browser closed by user")
        _reset_main_state()
        await manager.broadcast({"type": "browser_closed", "target": "main"})
        for k in ("delivery", "batsman", "wicket"):
            await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})

    try:
        ctx.on("close", lambda *_: _spawn(_on_close()))
    except Exception as e:
        print(f"[close-hook] main: {e}")


def _bind_worker_close_handlers(kind: str, wctx):
    if wctx is None:
        return

    async def _on_close(_=None):
        print(f"⚠️ {kind} browser closed")
        _reset_worker_state(kind)
        await manager.broadcast({"type": "browser_closed", "target": kind})
        await manager.broadcast({"type": "mulling_status", "worker": kind, "enabled": False})

    try:
        wctx.on("close", lambda *_: _spawn(_on_close()))
    except Exception as e:
        print(f"[close-hook] {kind}: {e}")


# ============================================================
#  Generic helpers
# ============================================================
def _event_position(data: dict):
    raw_over = data.get("over")
    over_number = None
    ball_number = None
    try:
        ot = str(raw_over).strip()
        if "." in ot:
            op, bp = ot.split(".", 1)
            over_number = int(op)
            ball_number = int(bp) if bp else 1
        elif ot:
            over_number = int(float(ot))
    except (TypeError, ValueError):
        pass
    px = None
    try:
        if data.get("xball") is not None:
            px = int(data["xball"])
    except (TypeError, ValueError):
        pass
    for cand in (px, data.get("ball")):
        try:
            if cand is not None:
                ball_number = int(cand)
                break
        except (TypeError, ValueError):
            continue
    if OVER_ZERO_IS_LAST_BALL and ball_number == 0 and over_number:
        over_number -= 1
        ball_number = 6
    return over_number, ball_number, px


def _remember_event(mid: str, eid: str) -> bool:
    key = f"{mid}:{eid}"
    if key in _processed_event_key_set:
        return False
    if len(_processed_event_keys) >= _MAX_PROCESSED_EVENTS:
        _processed_event_key_set.discard(_processed_event_keys.popleft())
    _processed_event_keys.append(key)
    _processed_event_key_set.add(key)
    return True


def _fixture_from_push(mid: str, info: dict) -> dict:
    ta = info.get("teama") if isinstance(info.get("teama"), dict) else {}
    tb = info.get("teamb") if isinstance(info.get("teamb"), dict) else {}
    title = (info.get("title") or info.get("short_title")
             or " vs ".join(n for n in (ta.get("name"), tb.get("name")) if n)
             or "Unknown match")
    return {
        "match_id": str(mid), "title": title,
        "format": info.get("format_str") or "—",
        "status": info.get("status_str") or "Live",
        "date_start": info.get("date_start_ist") or info.get("date_start") or "",
    }


def _extract_game_id(url: str):
    if not url:
        return None
    try:
        qs = parse_qs(urlparse(url).query)
        if "gameId" in qs:
            return qs["gameId"][0]
    except Exception:
        pass
    m = re.findall(r"[-/](\d{6,})", url)
    if m:
        return m[-1]
    m = re.findall(r"(\d{6,})", url)
    return m[-1] if m else None


def _extract_score(mi: dict, li: dict, bd: dict):
    runs = wickets = None
    ta = mi.get("teama") if isinstance(mi.get("teama"), dict) else {}
    for src in (ta, li, bd):
        if not isinstance(src, dict):
            continue
        if runs is None:
            for k in ("scores", "score", "runs", "team_score", "total_runs"):
                v = src.get(k)
                if v not in (None, ""):
                    s = str(v)
                    if "/" in s:
                        p = s.split("/", 1)
                        runs = runs or p[0].strip()
                        if wickets is None:
                            wickets = p[1].split("(")[0].strip()
                    else:
                        runs = s
                    break
        if wickets is None:
            for k in ("wickets", "team_wickets", "total_wickets"):
                v = src.get(k)
                if v not in (None, ""):
                    wickets = str(v)
                    break
    if runs is None:
        return None
    return f"{runs} - {wickets or 0}"


def _guard_payload() -> dict:
    return {"type": "guard_status", **guard.status()}


def fetch_entity_fixtures(status: str) -> list:
    """Entity REST — best effort. Free token-এ খালি আসতে পারে, চিন্তা নেই।"""
    q = urlencode({"token": ENTITY_API_TOKEN, "status": status,
                   "per_page": 50, "paged": 1})
    req = URLRequest(f"{ENTITY_REST_BASE}/matches?{q}",
                     headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        raise RuntimeError(f"Entity REST HTTP {e.code}") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Entity REST ({type(e).__name__})") from None

    if not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError("Entity Sports invalid response")

    rd = data.get("response")
    items = rd.get("items", []) if isinstance(rd, dict) else []
    if not isinstance(items, list):
        raise RuntimeError("Entity Sports invalid list")

    out = []
    for it in items:
        if not isinstance(it, dict) or not it.get("match_id"):
            continue
        ta = it.get("teama") if isinstance(it.get("teama"), dict) else {}
        tb = it.get("teamb") if isinstance(it.get("teamb"), dict) else {}
        title = (it.get("title") or it.get("short_title")
                 or " vs ".join(n for n in (ta.get("name"), tb.get("name")) if n)
                 or "Unknown match")
        out.append({
            "match_id": str(it["match_id"]), "title": title,
            "format": it.get("format_str") or "—",
            "status": it.get("status_str") or {
                "3": "Live", "1": "Upcoming", "2": "Completed", "4": "Cancelled"
            }.get(status, "Unknown"),
            "date_start": it.get("date_start_ist") or it.get("date_start") or "",
        })
    return out


async def _prefetch_fixtures():
    """Best-effort — REST fail করলেও crash করবে না। Push cache পরে ভরে দেবে।"""
    if not (ENTITY_API_TOKEN and ENTITY_REST_BASE):
        print("[prefetch] skipped (no REST token/base)")
        return
    try:
        groups = await asyncio.gather(
            *(asyncio.to_thread(fetch_entity_fixtures, s) for s in ("3", "1")),
            return_exceptions=True,
        )
        count = 0
        for g in groups:
            if isinstance(g, Exception):
                print(f"[prefetch] {type(g).__name__}: {g}")
                continue
            for f in g:
                entity_fixture_cache[f["match_id"]] = f
                count += 1
        print(f"[prefetch] cached {count} fixtures from REST")
    except Exception as e:
        print(f"[prefetch] failed: {type(e).__name__}: {e}")


# ============================================================
#  Worker dispatch
# ============================================================
async def _run_worker(name: str, coro):
    try:
        await coro
    except Exception as exc:
        print(f"⚠️ {name} worker failed: {type(exc).__name__}: {exc}")
        await manager.broadcast({
            "type": "worker_error", "worker": name,
            "message": f"{type(exc).__name__}: {exc}",
        })


async def _dispatch_entity_ball(event: dict):
    if (not current_match_id or not active_entity_match_id
        or event["entity_match_id"] != str(active_entity_match_id)
        or not event.get("event_id")):
        return
    on = event.get("over_number")
    bn = event.get("ball_number")
    kind = event.get("kind")
    d_del = kind in {"dot", "wicket", "four"} and "delivery" in armed_workers and worker_delivery and on is not None and bn is not None
    d_wick = kind == "wicket" and "wicket" in armed_workers and worker_wicket
    d_bat = kind == "wicket" and "batsman" in armed_workers and worker_batsman
    if not (d_del or d_wick or d_bat):
        return
    if not _remember_event(event["entity_match_id"], event["event_id"]):
        return

    print(f"[dispatch] {kind} raw(over={event.get('raw_over')}, ball={event.get('raw_ball')}, "
          f"xball={event.get('xball')}) -> over_number={on}, ball_number={bn}")

    if d_del:
        _spawn(_run_worker(
            "delivery",
            worker_delivery.process_ball(on, bn, event["event"], event_kind=kind),
        ))
    if d_wick:
        _spawn(_run_worker("wicket", worker_wicket.on_wicket()))
    if d_bat:
        sname = str(event.get("striker") or "").strip()
        if not sname:
            print("⚠️ [Batsman] striker-এর নাম পাওয়া যায়নি — বেট স্কিপ")
        else:
            _spawn(_run_worker(
                "batsman",
                worker_batsman.on_batsman_runs(striker_name=sname, line_value="31.5"),
            ))


# ============================================================
#  Entity message handler
# ============================================================
async def _handle_entity_message(payload: dict):
    global _last_seen_entity_mid
    rd = payload.get("response")
    if not isinstance(rd, dict):
        return
    mi = rd.get("match_info") if isinstance(rd.get("match_info"), dict) else {}
    li = rd.get("live") if isinstance(rd.get("live"), dict) else {}
    bd = rd.get("data") if isinstance(rd.get("data"), dict) else {}
    mid = rd.get("match_id") or mi.get("match_id") or li.get("match_id") or bd.get("match_id")
    if not mid:
        return
    mid = str(mid)

    _entity_debug_log.append({
        "ts": round(time.time(), 3), "mid": mid, "keys": list(rd.keys()),
        "has_mi": bool(mi), "has_bd": bool(bd),
    })

    is_new = mid not in entity_last_state
    state = entity_last_state.setdefault(mid, {})
    if is_new:
        print(f"[entity] New match seen: mid={mid} keys={list(rd.keys())} has_mi={bool(mi)}")

    # -------- Focus / show decision --------
    if preview_entity_match_id:
        is_shown = (mid == str(preview_entity_match_id))
    elif active_entity_match_id:
        is_shown = (mid == str(active_entity_match_id))
    elif _last_seen_entity_mid is None:
        is_shown = True
    else:
        is_shown = (mid == _last_seen_entity_mid)

    is_active = bool(active_entity_match_id and mid == str(active_entity_match_id))

    if is_shown:
        _last_seen_entity_mid = mid

    # -------- Teams (match_info > fixture cache) --------
    teams_changed = False
    if mi:
        ta = mi.get("teama") if isinstance(mi.get("teama"), dict) else {}
        tb = mi.get("teamb") if isinstance(mi.get("teamb"), dict) else {}
        if ta.get("name") or tb.get("name") or mi.get("title") or mi.get("short_title"):
            entity_fixture_cache[mid] = _fixture_from_push(mid, mi)
            teams = " vs ".join(n for n in (ta.get("name"), tb.get("name")) if n) or mi.get("title") or ""
            if teams and teams != state.get("teams"):
                state["teams"] = teams
                teams_changed = True

    if not state.get("teams"):
        fx = entity_fixture_cache.get(mid, {})
        if fx.get("title") and fx["title"] != "Unknown match":
            state["teams"] = fx["title"]
            teams_changed = True

    if is_shown and state.get("teams") and teams_changed:
        await manager.broadcast({
            "type": "entity_match_info", "entity_match_id": mid, "teams": state["teams"],
        })

    # -------- Score (when no ball_event) --------
    be = rd.get("ball_event")
    if not be:
        s = _extract_score(mi, li, bd)
        if s:
            state["score"] = s
            if is_shown:
                await manager.broadcast({"type": "match_update", "entity_match_id": mid, "score": s})
        return

    # -------- Ball event --------
    if isinstance(be, dict):
        et = str(be.get("ev") or be.get("event") or "")
        if not bd:
            n = be.get("data")
            bd = n if isinstance(n, dict) else {}
    else:
        et = str(be)

    on, bn, xb = _event_position(bd)
    eid = bd.get("event_id") or bd.get("id") or rd.get("event_id")
    if eid is None and (on is not None or bn is not None):
        eid = "|".join("" if v is None else str(v) for v in (
            bd.get("inn") or bd.get("inn_num"), on, bn, xb,
        ))
    eid = str(eid) if eid is not None else ""

    score = _extract_score(mi, li, bd)
    event = {
        "type": "entity_ball_event", "entity_match_id": mid, "event_id": eid,
        "event": et, "kind": classify_entity_event(et),
        "over": bd.get("over"), "over_number": on, "ball_number": bn, "xball": xb,
        "raw_over": bd.get("over"), "raw_ball": bd.get("ball"),
        "innings": bd.get("inn") or bd.get("inn_num"),
        "striker": bd.get("striker"),
        "striker_id": bd.get("striker_id") or bd.get("player_id"),
        "bowler": bd.get("bowler"), "score": score,
        "teams": state.get("teams"),
    }
    ts = payload.get("timestamp")
    try:
        t_ms = float(ts)
        if t_ms < 100_000_000_000:
            t_ms *= 1000
        lag = int(time.time() * 1000 - t_ms)
        event["lag_ms"] = lag if 0 <= lag < 60_000 else None
    except (TypeError, ValueError):
        event["lag_ms"] = None

    if score:
        state["score"] = score
    state["event"] = event
    if is_shown:
        await manager.broadcast(event)
    if is_active:
        await _dispatch_entity_ball(event)


async def entity_push_listener():
    sep = "&" if "?" in ENTITY_PUSH_URL else "?"
    uri = f"{ENTITY_PUSH_URL}{sep}{urlencode({'token': ENTITY_PUSH_TOKEN})}"
    delay = 3
    while True:
        try:
            entity_push_state["status"] = "connecting"
            entity_push_state["last_error"] = None
            await manager.broadcast({"type": "entity_push_status", **entity_push_state})
            async with websockets.connect(uri, open_timeout=15, ping_interval=20, max_size=None) as c:
                delay = 3
                entity_push_state["status"] = "connected"
                entity_push_state["last_error"] = None
                await manager.broadcast({"type": "entity_push_status", **entity_push_state})
                async for msg in c:
                    if isinstance(msg, bytes):
                        try:
                            if msg[:2] == b"\x1f\x8b":
                                msg = gzip.decompress(msg)
                            msg = msg.decode("utf-8")
                        except (OSError, UnicodeDecodeError):
                            continue
                    try:
                        p = json.loads(msg)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(p, dict):
                        continue
                    entity_push_state["messages"] += 1
                    entity_push_state["last_message_at"] = time.time()
                    try:
                        await _handle_entity_message(p)
                    except Exception as exc:
                        print(f"⚠️ Entity handler: {type(exc).__name__}: {exc}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            et = type(exc).__name__
            print(f"⚠️ Entity Push disconnected: {et}")
            entity_push_state["status"] = "disconnected"
            entity_push_state["last_error"] = et
            await manager.broadcast({"type": "entity_push_status", **entity_push_state})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


# ============================================================
#  Models
# ============================================================
class LaunchRequest(BaseModel):
    website_url: str = ""
    match_url: str = ""

class LockRequest(BaseModel):
    match_id: str = ""

class VerifyRequest(BaseModel):
    url: str
    match_id: str
    entity_match_id: str
    format: str = ""
    title: str = ""

class MullingRequest(BaseModel):
    enabled: bool


# ============================================================
#  WebSocket
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "entity_push_status", **entity_push_state})
        await websocket.send_json(_guard_payload())
        await websocket.send_json({
            "type": "browser_status",
            "main": await _main_alive(),
            "batsman": bool(worker_ctxs.get("batsman")),
            "wicket": bool(worker_ctxs.get("wicket")),
            "locked": bool(locked_melbet_id),
            "verified": bool(current_match_id and active_entity_match_id),
            "match_key": locked_melbet_id or detected_match_id or "",
            "entity_key": active_entity_match_id or "",
        })
        if current_match_id and active_entity_match_id:
            await websocket.send_json({
                "type": "entity_active_match",
                "match_id": active_entity_match_id,
                "melbet_match_id": current_match_id,
            })
        for w in ("delivery", "batsman", "wicket"):
            await websocket.send_json({
                "type": "mulling_status", "worker": w, "enabled": w in armed_workers,
            })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)


# ============================================================
#  Launch main browser
# ============================================================
@app.post("/api/launch")
async def launch_browser(data: LaunchRequest):
    global pw, ctx, page, engine

    if ctx is not None:
        if not await _main_alive():
            print("🧹 Stale browser — auto reset")
            try:
                await ctx.close()
            except Exception:
                pass
            _reset_main_state()
        else:
            return {"status": "error", "message": "ব্রাউজার ইতিমধ্যে ওপেন!"}

    url = (data.website_url or "").strip() or (data.match_url or "").strip()
    if not url:
        return {"status": "error", "message": "Website URL required"}

    new_ctx = None
    try:
        if pw is None:
            pw = await async_playwright().start()

        new_ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir("main"),
            headless=False, channel="chrome", no_viewport=True,
            args=browser_args("0,0", "1100,720"),
        )
        new_page = new_ctx.pages[0] if new_ctx.pages else await new_ctx.new_page()
        new_engine = MelbetEngine(new_page)

        _reset_main_state()
        ctx = new_ctx
        page = new_page
        engine = new_engine

        _bind_main_close_handlers()

        async def on_request(req):
            global detected_match_id
            if "gameEvents" not in req.url:
                return
            try:
                vals = parse_qs(urlparse(req.url).query).get("gameId")
                if not vals:
                    return
                gid = vals[0]
                if gid != detected_match_id:
                    detected_match_id = gid
                    await manager.broadcast({
                        "type": "match_id_detected", "match_id": gid,
                        "locked": bool(locked_melbet_id),
                    })
            except Exception as exc:
                print(f"⚠️ Match URL ধরতে ব্যর্থ: {exc}")

        page.on("request", lambda r: _spawn(on_request(r)))
        await page.goto(url)
        _spawn(lower_chrome_priority())
    except Exception as exc:
        for obj, fn in ((new_ctx, "close"), (ctx, "close"), (pw, "stop")):
            try:
                if obj is not None:
                    await getattr(obj, fn)()
            except Exception:
                pass
        _reset_main_state()
        pw = None
        return {"status": "error", "message": f"Launch failed: {exc}"}

    await manager.broadcast({
        "type": "browser_status", "main": True,
        "batsman": bool(worker_ctxs.get("batsman")),
        "wicket": bool(worker_ctxs.get("wicket")),
    })
    return {"status": "success", "message": "Main browser opened"}


# ============================================================
#  Lock / Unlock / Verify
# ============================================================
@app.post("/api/lock")
async def do_lock(data: LockRequest):
    global locked_melbet_id, current_match_id, active_entity_match_id
    mid = (data.match_id or detected_match_id or "").strip()
    if not mid:
        return {"status": "error", "message": "ম্যাচ আইডি নেই — Match URL দিন বা ব্রাউজারে ম্যাচ খুলুন"}
    if locked_melbet_id != mid:
        current_match_id = None
        active_entity_match_id = None
        armed_workers.clear()
    locked_melbet_id = mid
    return {"status": "success", "match_id": mid}


@app.post("/api/unlock")
async def do_unlock():
    global locked_melbet_id, current_match_id, active_entity_match_id, verified_match_url
    global preview_entity_match_id, _last_seen_entity_mid
    locked_melbet_id = None
    current_match_id = None
    active_entity_match_id = None
    verified_match_url = None
    preview_entity_match_id = None
    _last_seen_entity_mid = None
    armed_workers.clear()
    await manager.broadcast({"type": "unlocked"})
    for k in ("delivery", "batsman", "wicket"):
        await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})
    return {"status": "success"}


class UrlRequest(BaseModel):
    url: str = ""


class PreviewRequest(BaseModel):
    entity_match_id: str = ""


@app.post("/api/parse-match-url")
async def parse_match_url(data: UrlRequest):
    mid = _extract_game_id((data.url or "").strip())
    if not mid:
        return {"status": "error", "message": "URL থেকে ম্যাচ আইডি পাওয়া যায়নি"}
    return {"status": "success", "match_id": mid}


@app.post("/api/preview")
async def set_preview(data: PreviewRequest):
    global preview_entity_match_id
    mid = (data.entity_match_id or "").strip()
    preview_entity_match_id = mid or None
    if not mid:
        return {"status": "success"}
    fx = entity_fixture_cache.get(mid, {})
    st = entity_last_state.get(mid, {})
    await manager.broadcast({
        "type": "entity_snapshot", "entity_match_id": mid,
        "teams": st.get("teams") or fx.get("title") or "",
        "score": st.get("score"),
        "event": st.get("event"),
    })
    return {"status": "success"}


@app.post("/api/verify-match-url")
async def verify_match_url(data: VerifyRequest):
    global current_match_id, active_entity_match_id, detected_match_id, verified_match_url
    global preview_entity_match_id

    if ctx is None or not await _main_alive():
        _reset_main_state()
        return {"status": "error", "message": "Browser not running"}
    if not locked_melbet_id:
        return {"status": "error", "message": "Lock a match first"}
    if not data.match_id or data.match_id != locked_melbet_id:
        return {"status": "error", "message": f"Match ID mismatch — locked: {locked_melbet_id}"}
    if not data.entity_match_id:
        return {"status": "error", "message": "Select a fixture first"}
    if not data.format:
        return {"status": "error", "message": "Match format missing"}

    url_id = _extract_game_id(data.url)
    if url_id and url_id != locked_melbet_id:
        return {"status": "error", "message": f"URL gameId ({url_id}) != locked ({locked_melbet_id})"}

    detected_match_id = None
    try:
        await page.goto(data.url.strip(), wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        return {"status": "error", "message": f"Navigate failed: {exc}"}
    for _ in range(16):
        if detected_match_id:
            break
        await asyncio.sleep(0.5)

    if detected_match_id and detected_match_id != locked_melbet_id:
        return {"status": "error", "message": f"Detected ({detected_match_id}) != locked ({locked_melbet_id})"}

    current_match_id = locked_melbet_id
    active_entity_match_id = data.entity_match_id
    preview_entity_match_id = None
    verified_match_url = data.url.strip()

    fixture = entity_fixture_cache.get(active_entity_match_id, {})
    teams = fixture.get("title") or data.title or "-- vs --"

    await manager.broadcast({
        "type": "entity_active_match",
        "match_id": active_entity_match_id,
        "melbet_match_id": current_match_id,
    })
    await manager.broadcast({"type": "entity_match_info", "teams": teams})
    await manager.broadcast({
        "type": "verified", "match_id": current_match_id,
        "entity_match_id": active_entity_match_id,
    })
    for k in ("delivery", "batsman", "wicket"):
        await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})
    return {"status": "success", "match_id": current_match_id, "entity_match_id": active_entity_match_id}


# ============================================================
#  Launch worker browser
# ============================================================
@app.post("/api/launch-worker/{kind}")
async def launch_worker(kind: str, data: LaunchRequest):
    if kind not in {"batsman", "wicket"}:
        return {"status": "error", "message": "Only batsman/wicket supported"}

    existing = worker_ctxs.get(kind)
    if existing is not None:
        alive = False
        try:
            wp = worker_pages.get(kind)
            if wp is not None and not wp.is_closed():
                await asyncio.wait_for(wp.title(), timeout=2.0)
                alive = True
        except Exception:
            alive = False
        if not alive:
            print(f"🧹 {kind} stale — reset")
            try:
                await existing.close()
            except Exception:
                pass
            _reset_worker_state(kind)
        else:
            return {"status": "success", "message": f"{kind} browser already open"}

    url = (data.website_url or "").strip()
    if not url:
        return {"status": "error", "message": "Website URL required"}
    if pw is None:
        return {"status": "error", "message": "Launch main browser first"}

    pos = {"batsman": "400,60", "wicket": "780,120"}.get(kind, "0,0")
    want_headless = worker_headless()

    try:
        wctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir(kind),
            headless=want_headless, channel="chrome", no_viewport=True,
            args=browser_args(pos, "1000,680"),
        )
        _spawn(lower_chrome_priority())
        wpage = wctx.pages[0] if wctx.pages else await wctx.new_page()
        await wpage.goto(url)

        if want_headless:
            await asyncio.sleep(2.0)
            if not await is_logged_in(wpage):
                print(f"[worker] {kind} headless no-login — visible relaunch")
                try:
                    await wctx.close()
                except Exception:
                    pass
                wctx = await pw.chromium.launch_persistent_context(
                    user_data_dir=profile_dir(kind),
                    headless=False, channel="chrome", no_viewport=True,
                    args=browser_args(pos, "1000,680"),
                )
                _spawn(lower_chrome_priority())
                wpage = wctx.pages[0] if wctx.pages else await wctx.new_page()
                await wpage.goto(url)

        worker_ctxs[kind] = wctx
        worker_pages[kind] = wpage
        worker_engines[kind] = MelbetEngine(wpage)
        _bind_worker_close_handlers(kind, wctx)
    except Exception as exc:
        _reset_worker_state(kind)
        return {"status": "error", "message": f"Failed: {exc}"}

    return {"status": "success", "message": f"{kind} browser opened"}


@app.post("/api/close-worker/{kind}")
async def close_worker(kind: str):
    if kind not in {"batsman", "wicket"}:
        return {"status": "error", "message": "Only batsman/wicket supported"}
    wc = worker_ctxs.get(kind)
    _reset_worker_state(kind)
    if wc is not None:
        try:
            await wc.close()
        except Exception:
            pass
    await manager.broadcast({"type": "mulling_status", "worker": kind, "enabled": False})
    await manager.broadcast({"type": "browser_closed", "target": kind})
    return {"status": "success", "message": f"{kind} browser closed"}


@app.post("/api/reset-browser")
async def reset_browser():
    global pw
    try:
        if ctx is not None:
            await ctx.close()
    except Exception:
        pass
    _reset_main_state()
    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass
        pw = None
    await manager.broadcast({"type": "browser_closed", "target": "main"})
    for k in ("delivery", "batsman", "wicket"):
        await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})
    return {"status": "success", "message": "Browser state reset"}


@app.post("/api/stop-all")
async def stop_all():
    armed_workers.clear()
    for k in ("delivery", "batsman", "wicket"):
        await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})
    return {"status": "success", "message": "All workers stopped"}


@app.get("/api/status")
async def get_status():
    return {
        "main_alive": await _main_alive(),
        "batsman": bool(worker_ctxs.get("batsman")),
        "wicket": bool(worker_ctxs.get("wicket")),
        "locked_melbet_id": locked_melbet_id,
        "active_entity_match_id": active_entity_match_id,
        "preview_entity_match_id": preview_entity_match_id,
        "last_seen_entity_mid": _last_seen_entity_mid,
        "armed_workers": sorted(armed_workers),
        "kill_switch": guard.killed,
        "entity_push": dict(entity_push_state),
    }


# ============================================================
#  Debug
# ============================================================
@app.get("/api/debug/entity")
async def debug_entity():
    return {
        "status": "success",
        "push": dict(entity_push_state),
        "active_entity_match_id": active_entity_match_id,
        "preview_entity_match_id": preview_entity_match_id,
        "last_seen_entity_mid": _last_seen_entity_mid,
        "known_matches": list(entity_last_state.keys()),
        "cached_fixtures": {k: v.get("title") for k, v in entity_fixture_cache.items()},
        "recent_messages": list(_entity_debug_log),
    }


# ============================================================
#  Bet guard
# ============================================================
@app.get("/api/guard")
async def get_guard():
    return {"status": "success", **guard.status()}


@app.post("/api/guard/kill")
async def guard_kill():
    guard.kill()
    armed_workers.clear()
    await manager.broadcast(_guard_payload())
    for k in ("delivery", "batsman", "wicket"):
        await manager.broadcast({"type": "mulling_status", "worker": k, "enabled": False})
    return {"status": "success", "message": "Kill-switch ON — সব বেট বন্ধ"}


@app.post("/api/guard/resume")
async def guard_resume():
    guard.resume()
    await manager.broadcast(_guard_payload())
    return {"status": "success", "message": "Kill-switch OFF — আবার Mulling চালু করুন"}


# ============================================================
#  Fixtures
# ============================================================
@app.get("/api/fixtures")
async def get_fixtures():
    """Push cache প্রথমে; REST best-effort (free token-এ খালি এলেও সমস্যা নেই)।"""
    by_id = dict(entity_fixture_cache)

    if ENTITY_API_TOKEN and ENTITY_REST_BASE:
        try:
            groups = await asyncio.gather(
                *(asyncio.to_thread(fetch_entity_fixtures, s) for s in ("3", "1", "2", "4")),
                return_exceptions=True,
            )
            for g in groups:
                if isinstance(g, Exception):
                    print(f"[fixtures] REST {type(g).__name__}: {g}")
                    continue
                for f in g:
                    by_id[f["match_id"]] = f
                    entity_fixture_cache[f["match_id"]] = f
        except Exception as e:
            print(f"[fixtures] REST failed: {e}")

    so = {"live": 0, "started": 0, "3": 0,
          "upcoming": 1, "not started": 1, "1": 1,
          "completed": 2, "2": 2, "cancelled": 3, "4": 3}
    fixtures = sorted(by_id.values(), key=lambda f: (
        so.get(str(f["status"]).lower(), 4),
        f.get("date_start", ""),
        f.get("title", ""),
    ))
    return {"status": "success", "fixtures": fixtures}


# ============================================================
#  Mulling
# ============================================================
@app.post("/api/mulling/{worker_kind}")
async def set_mulling(worker_kind: str, data: MullingRequest):
    global worker_delivery, worker_batsman, worker_wicket
    if worker_kind not in {"delivery", "batsman", "wicket"}:
        raise HTTPException(status_code=404, detail="Unknown worker")
    if not current_match_id:
        return {"status": "error", "message": "Verify a match first!"}
    if not active_entity_match_id:
        return {"status": "error", "message": "No entity match active"}
    if data.enabled and guard.killed:
        return {"status": "error", "message": "Kill-switch ON — আগে Resume করুন"}

    if data.enabled:
        if worker_kind == "delivery":
            if engine is None or ctx is None or not await _main_alive():
                return {"status": "error", "message": "Main browser not running"}
            worker_delivery = DeliveryWorker(engine, current_match_id)
        elif worker_kind == "batsman":
            if not worker_engines.get("batsman"):
                return {"status": "error", "message": "Batsman browser not launched"}
            worker_batsman = BatsmanWorker(worker_engines["batsman"], current_match_id)
            if verified_match_url:
                try:
                    wp = worker_pages.get("batsman")
                    if wp and verified_match_url not in wp.url:
                        await wp.goto(verified_match_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[batsman] navigate failed: {e}")
        elif worker_kind == "wicket":
            if not worker_engines.get("wicket"):
                return {"status": "error", "message": "Wicket browser not launched"}
            worker_wicket = WicketWorker(worker_engines["wicket"], current_match_id)
            if verified_match_url:
                try:
                    wp = worker_pages.get("wicket")
                    if wp and verified_match_url not in wp.url:
                        await wp.goto(verified_match_url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    print(f"[wicket] navigate failed: {e}")
        armed_workers.add(worker_kind)
    else:
        armed_workers.discard(worker_kind)
    await manager.broadcast({"type": "mulling_status", "worker": worker_kind, "enabled": data.enabled})
    return {"status": "success", "message": f"{worker_kind} mulling {'ON' if data.enabled else 'OFF'}"}


# ============================================================
#  Manual bet endpoints
# ============================================================
@app.post("/api/bet/delivery")
async def bet_delivery():
    if not current_match_id or not worker_delivery:
        return {"status": "error", "message": "Worker not ready"}
    _spawn(worker_delivery.on_dot_ball(over_num=3, ball_num=5))
    return {"status": "success", "message": "Delivery bet fired"}


class BatsmanBetRequest(BaseModel):
    striker_name: str = ""


@app.post("/api/bet/batsman")
async def bet_batsman(data: BatsmanBetRequest):
    if not current_match_id or not worker_batsman:
        return {"status": "error", "message": "Worker not ready"}
    name = (data.striker_name or "").strip()
    if not name:
        return {"status": "error", "message": "striker_name required"}
    _spawn(worker_batsman.on_batsman_runs(striker_name=name, line_value="31.5"))
    return {"status": "success", "message": f"Batsman bet fired for {name}"}


@app.post("/api/bet/wicket")
async def bet_wicket():
    if not current_match_id or not worker_wicket:
        return {"status": "error", "message": "Worker not ready"}
    _spawn(worker_wicket.on_wicket(line_value="1.5"))
    return {"status": "success", "message": "Wicket bet fired"}


# ============================================================
#  Webhook
# ============================================================
@app.post("/api/entity-webhook")
async def entity_webhook(request: Request):
    try:
        d = await request.json()
        if not isinstance(d, dict):
            raise HTTPException(status_code=400, detail="Invalid payload")
        await manager.broadcast({
            "type": "match_update",
            "teams": d.get("teams"), "score": d.get("score"),
            "over": d.get("over"), "ball": d.get("event_display"),
            "ballKind": d.get("event_type"), "status": "Live",
            "overNo": f"OVER {d.get('over', '0')}",
        })
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ============================================================
#  Static files
# ============================================================
@app.get("/")
async def serve_ui():
    return FileResponse(BASE_DIR / "index.html", media_type="text/html")


@app.get("/index.css")
async def serve_stylesheet():
    return FileResponse(BASE_DIR / "index.css", media_type="text/css")


@app.get("/script.js")
async def serve_script():
    return FileResponse(BASE_DIR / "script.js", media_type="application/javascript")


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


# ============================================================
#  Lifecycle
# ============================================================
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=5000)