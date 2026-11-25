import asyncio
import gzip
import json
import logging
import os
import sys
import time
from collections import defaultdict, OrderedDict
from contextlib import asynccontextmanager

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import httpx
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from patchright.async_api import async_playwright
from dotenv import load_dotenv

# ⚡ নতুন ওয়ার্কারগুলো ইমপোর্ট করা হলো
from melbet_engine import MelbetEngine
from delivery_worker import DeliveryWorker
from batsman_worker import BatsmanWorker
from wicket_worker import WicketWorker

load_dotenv()

PUSH_TOKEN = os.environ.get("ES_PUSH_TOKEN")
PUSH_URL = os.environ.get("ES_PUSH_URL", "ws://webhook.entitysport.com:8087/connect")

UP = {"connected": False, "msgs": 0}
client = httpx.AsyncClient(timeout=15)

# ⚡ ওয়ার্কার ও ইঞ্জিন গ্লোবাল ভেরিয়েবল
bot_engine = None
active_match_id = None
worker_delivery = None
worker_batsman = None
worker_wicket = None
active_workers = {"delivery": False, "batsman": False, "wicket": False}

class ConnectionManager:
    def __init__(self):
        self.by_match = defaultdict(set)
        self.conns = {}

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.conns[ws] = None

    def disconnect(self, ws: WebSocket):
        self.conns.pop(ws, None)
        for s in self.by_match.values(): s.discard(ws)

    def watch(self, ws: WebSocket, mid):
        for s in self.by_match.values(): s.discard(ws)
        self.conns[ws] = mid
        if mid: self.by_match[mid].add(ws)

    async def broadcast(self, msg: bytes, mid: str):
        targets = self.by_match.get(mid, [])
        for ws in list(targets):
            try: await ws.send_bytes(msg)
            except Exception: self.disconnect(ws)

manager = ConnectionManager()

class LaunchRequest(BaseModel):
    website_url: str

class WorkerRequest(BaseModel):
    kind: str

@app.post("/api/launch")
async def launch_browser(data: LaunchRequest):
    global bot_engine
    if hasattr(app.state, "pw") and app.state.pw:
        return {"status": "error", "message": "Browser already open"}

    app.state.pw = await async_playwright().start()
    app.state.ctx = await app.state.pw.chromium.launch_persistent_context(
        user_data_dir="./profile_main", headless=False, channel="chrome", no_viewport=True, args=["--start-maximized"]
    )
    app.state.page = app.state.ctx.pages[0] if app.state.ctx.pages else await app.state.ctx.new_page()
    await app.state.page.goto(data.website_url)
    
    bot_engine = MelbetEngine(app.state.page)
    
    # Melbet Match ID ক্যাচার
    async def on_request(req):
        global active_match_id, worker_delivery, worker_batsman, worker_wicket
        if "gameEvents" in req.url and "gameId=" in req.url:
            try:
                gid = req.url.split("gameId=")[1].split("&")[0]
                if gid != active_match_id:
                    active_match_id = gid
                    print(f"\n🎯 Auto-Detected Melbet Match ID: {gid}")
                    # ওডস ডিটেক্ট হলেই ওয়ার্কার আপডেট করা
                    worker_delivery = DeliveryWorker(bot_engine, active_match_id)
                    worker_batsman = BatsmanWorker(bot_engine, active_match_id)
                    worker_wicket = WicketWorker(bot_engine, active_match_id)
            except: pass
            
    app.state.page.on("request", lambda r: asyncio.create_task(on_request(r)))
    return {"status": "success"}

@app.post("/api/set-active")
async def set_active(data: dict):
    global active_match_id, worker_delivery, worker_batsman, worker_wicket
    active_match_id = data.get("match_id")
    if bot_engine:
        worker_delivery = DeliveryWorker(bot_engine, active_match_id)
        worker_batsman = BatsmanWorker(bot_engine, active_match_id)
        worker_wicket = WicketWorker(bot_engine, active_match_id)
    return {"status": "success"}

@app.post("/api/worker/start")
async def start_worker(data: WorkerRequest):
    active_workers[data.kind] = True
    print(f"✅ {data.kind.upper()} Worker একটিভ হয়েছে!")
    return {"status": "success"}

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data.startswith("watch:"):
                manager.watch(websocket, data[6:].strip())
    except WebSocketDisconnect:
        manager.disconnect(websocket)

def _handle_ball_event(r: dict, mid: str):
    if not manager.by_match.get(mid): return
    d = r.get("data") if isinstance(r.get("data"), dict) else {}
    ev = str(r.get("ball_event", ""))
    over = int(d.get("over", 0))
    ball = int(d.get("ball", 0))
    striker = d.get("striker_name")

    # UI-তে পাঠানো
    out = {"t": "ev", "id": mid, "ev": ev, "over": over, "ball": ball}
    asyncio.create_task(manager.broadcast(json.dumps(out).encode("utf-8"), mid))

    # ⚡ Entity Event থেকে বেট প্লেসমেন্ট ট্রিগার
    if active_match_id == mid:
        if active_workers["delivery"] and worker_delivery:
            asyncio.create_task(worker_delivery.process_ball(over, ball, ev))
        
        if ev in ["wicket", "W", "out"]:
            if active_workers["batsman"] and worker_batsman and striker:
                asyncio.create_task(worker_batsman.process_wicket(striker))
            if active_workers["wicket"] and worker_wicket:
                asyncio.create_task(worker_wicket.process_wicket())

async def upstream_loop():
    url = f"{PUSH_URL}?token={PUSH_TOKEN}"
    while True:
        try:
            async with websockets.connect(url, max_size=None) as ws:
                UP["connected"] = True
                print("✅ Entity Push Connected")
                async for raw in ws:
                    UP["msgs"] += 1
                    try:
                        if raw[:2] == b"\x1f\x8b": raw = gzip.decompress(raw)
                        msg = json.loads(raw)
                        r = msg.get("response", {})
                        mi = r.get("match_info", {})
                        mid = str(r.get("match_id") or mi.get("match_id") or "")
                        if "ball_event" in r:
                            _handle_ball_event(r, mid)
                    except: pass
        except Exception as e:
            print("Push disconnected:", e)
            UP["connected"] = False
            await asyncio.sleep(3)

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pw = None
    tasks = []
    if PUSH_TOKEN:
        tasks.append(asyncio.create_task(upstream_loop()))
    yield
    for t in tasks: t.cancel()
    if app.state.pw:
        await app.state.ctx.close()
        await app.state.pw.stop()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.get("/")
async def index(): return FileResponse("index.html")
@app.get("/index.css")
async def css(): return FileResponse("index.css")
@app.get("/script.js")
async def js(): return FileResponse("script.js")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=5000)
