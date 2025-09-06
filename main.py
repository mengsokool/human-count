import os
import time
import json
import sqlite3
import secrets
import asyncio
from typing import Dict, Set, Optional
import requests

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, AnyUrl, field_validator

from ultralytics import YOLO

# ================== Config ==================
DB_PATH = os.getenv("DB_PATH", "subs.db")
MODEL_PATH = os.getenv("MODEL_PATH", "yolo11n.pt")  # ใช้ .pt
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))
CONF_THRES = float(os.getenv("CONF_THRES", "0.35"))
IOU_THRES = float(os.getenv("IOU_THRES", "0.45"))
PERSON_CLASS_ID = int(os.getenv("PERSON_CLASS_ID", "0"))
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "15"))

# ================== App & DB ==================
app = FastAPI(title="Human Count from Snapshot", version="0.4.0")

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
  token TEXT PRIMARY KEY,
  snapshot_url TEXT NOT NULL,
  interval_sec INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
""")
conn.commit()

# ================== Token ==================
def new_token(n: int = 12) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))

class SubscribeIn(BaseModel):
    snapshot_url: AnyUrl
    interval_sec: int
    @field_validator("interval_sec")
    @classmethod
    def _min_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("interval_sec must be >= 1")
        return v

# ================== YOLO Model ==================
model = YOLO(MODEL_PATH)

def infer_person_count(frame: np.ndarray) -> int:
    results = model.predict(
        source=frame,
        imgsz=IMG_SIZE,
        conf=CONF_THRES,
        iou=IOU_THRES,
        verbose=False
    )
    # results[0].boxes มี boxes, conf, cls
    boxes = results[0].boxes
    if boxes is None:
        return 0
    cls_ids = boxes.cls.cpu().numpy().astype(int)
    confs = boxes.conf.cpu().numpy()
    mask = (cls_ids == PERSON_CLASS_ID) & (confs >= CONF_THRES)
    return int(np.sum(mask))

def fetch_snapshot_requests(url: str) -> bytes:
    r = requests.get(url, headers={
        "User-Agent": "human-count/0.4",
        "Accept": "image/jpeg,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }, timeout=5, verify=False, stream=False)
    r.raise_for_status()
    return r.content

# ================== Worker ==================
class Worker:
    def __init__(self, token: str, url: str, interval: int):
        self.token = token
        self.url = url
        self.interval = interval
        self.queues: Set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self._last = 0.0

    def add(self, q): self.queues.add(q)
    def remove(self, q): self.queues.discard(q)
    def has_subs(self): return len(self.queues) > 0

    async def push(self, data: str):
        for q in list(self.queues):
            try:
                q.put_nowait(data)
            except:
                self.queues.discard(q)

    async def revoke(self):
        # Notify clients and close their streams
        for q in list(self.queues):
            try:
                q.put_nowait("event: revoked\ndata: token deleted\n\n")
                q.put_nowait(None)
            except:
                self.queues.discard(q)

    async def run(self):
        backoff = 1.0
        while self.has_subs():
            t0 = time.time()
            try:
                content = await asyncio.to_thread(fetch_snapshot_requests, self.url)
                arr = np.frombuffer(content, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    raise RuntimeError("decode fail")

                count = await asyncio.to_thread(infer_person_count, frame)
                await self.push(f"event: count\ndata: {count}\n\n")
                self._last = t0
                backoff = 1.0
            except Exception as e:
                await self.push(f"event: error\ndata: {json.dumps({'err': str(e)[:100]})}\n\n")
                await asyncio.sleep(min(backoff, 10.0))
                backoff *= 1.5
            if time.time() - self._last > HEARTBEAT_SEC:
                await self.push(": ping\n\n")
                self._last = time.time()
            await asyncio.sleep(max(0.0, self.interval - (time.time() - t0)))

class Manager:
    def __init__(self): 
        self.ws: Dict[str, Worker] = {}
        self.lock = asyncio.Lock()

    async def ensure(self, token, url, interval):
        async with self.lock:
            w = self.ws.get(token)
            if not w:
                w = Worker(token, url, interval)
                self.ws[token] = w
            w.interval = interval
            if not w.task or w.task.done():
                w.task = asyncio.create_task(self._run(w))
            return w

    async def _run(self, w: Worker):
        try:
            await w.run()
        finally:
            async with self.lock:
                self.ws.pop(w.token, None)

    async def revoke(self, token: str) -> bool:
        async with self.lock:
            w = self.ws.get(token)
        if not w:
            return False
        await w.revoke()
        return True

manager = Manager()

# ================== Routes ==================
@app.post("/subscribe")
def subscribe(inp: SubscribeIn):
    token = new_token()
    ts = int(time.time())
    with conn:
        conn.execute(
            "INSERT INTO subscriptions(token,snapshot_url,interval_sec,created_at) VALUES(?,?,?,?)",
            (token, str(inp.snapshot_url), inp.interval_sec, ts)
        )
    return {"subscription_token": token}

@app.get("/subs")
def list_subs():
    rows = conn.execute("SELECT token,snapshot_url,interval_sec,created_at FROM subscriptions").fetchall()
    return {"items": [{"token": t, "url": u, "interval": i, "created_at": ts} for (t, u, i, ts) in rows]}

@app.delete("/subs/{token}")
async def delete_sub(token: str):
    with conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE token=?", (token,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="token not found")
    await manager.revoke(token)
    return {"ok": True}

@app.get("/stream/{token}")
async def stream(token: str, request: Request):
    row = conn.execute("SELECT snapshot_url,interval_sec FROM subscriptions WHERE token=?", (token,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="token not found")
    url, itv = row
    q = asyncio.Queue(maxsize=10)
    w = await manager.ensure(token, url, itv)
    w.add(q)

    async def gen():
        try:
            yield ": hello\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SEC + 5)
                    if msg is None:
                        break
                    yield msg
                except asyncio.TimeoutError:
                    yield ": idle\n\n"
        finally:
            w.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream")
