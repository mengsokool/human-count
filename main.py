import os
import time
import json
import sqlite3
import secrets
import asyncio
from typing import Dict, Set, Optional
import requests
from collections import Counter

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, AnyUrl, field_validator

from ultralytics import YOLO

# ================== Config ==================
# ตั้งค่าพื้นฐานของระบบผ่าน env var (ปรับได้ตามสภาพแวดล้อมจริง)
DB_PATH = os.getenv("DB_PATH", "subs.db")
MODEL_PATH = os.getenv("MODEL_PATH", "yolo11s.pt")
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))
CONF_THRES = float(os.getenv("CONF_THRES", "0.3"))   # filter ชั้น model: กรองกล่อง conf ต่ำออกไปเลย (ลด noise + เบา CPU)
IOU_THRES = float(os.getenv("IOU_THRES", "0.45"))   # Non-Max Suppression threshold
PERSON_CLASS_ID = int(os.getenv("PERSON_CLASS_ID", "0"))  # class id ของ "person"
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "15"))     # ส่ง heartbeat กัน connection ตาย

# logic filter ชั้น behavior
STABLE_WINDOW = int(os.getenv("STABLE_WINDOW", "5"))  # ความยาว sliding window เก็บ history count
STABLE_FRAMES = int(os.getenv("STABLE_FRAMES", "3"))  # ต้องเจอ "0 คน" ติดกัน n frame ถึงจะปิดไฟ
INSTANT_CONF = float(os.getenv("INSTANT_CONF", "0.32"))  # conf ขั้นต่ำสำหรับ instant detect (เปิดไฟเร็ว)

# ================== App & DB ==================
app = FastAPI(title="Human Count from Snapshot", version="0.8.0")

# ใช้ SQLite เก็บ token/subscription (เบา + portable)
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
# สร้าง token ใหม่แบบ random (ใช้เป็น key สำหรับ stream)
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
# โหลดโมเดล YOLO
model = YOLO(MODEL_PATH)

# ฟังก์ชัน infer: รับ frame → detect คน → คืน count + confidence list
def infer_person_count(frame: np.ndarray):
    results = model.predict(
        source=frame,
        imgsz=IMG_SIZE,
        conf=CONF_THRES,   # กรองตั้งแต่ระดับ model
        iou=IOU_THRES,
        verbose=False
    )
    boxes = results[0].boxes
    if boxes is None:
        return 0, []
    cls_ids = boxes.cls.cpu().numpy().astype(int)
    confs = boxes.conf.cpu().numpy()
    mask = (cls_ids == PERSON_CLASS_ID) & (confs >= CONF_THRES)
    person_confs = confs[mask]
    count = int(np.sum(mask))
    return count, person_confs.tolist()

# ดึง snapshot จากกล้อง (เป็น JPEG) → คืน bytes
def fetch_snapshot_requests(url: str) -> bytes:
    r = requests.get(url, headers={
        "User-Agent": "human-count/0.8",
        "Accept": "image/jpeg,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }, timeout=5, verify=False, stream=False)
    r.raise_for_status()
    return r.content

# ================== Worker ==================
# worker = ตัวทำงานจริง: loop ดึง snapshot → detect → push SSE
class Worker:
    def __init__(self, token: str, url: str, interval: int):
        self.token = token
        self.url = url
        self.interval = interval
        self.queues: Set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self._last = 0.0

        # state machine สำหรับ instant/stable
        self.history = []               # เก็บ count ย้อนหลัง
        self.last_state = "no_person"   # state ล่าสุด ("person" หรือ "no_person")
        self.last_count = 0

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
        # แจ้ง client ว่า token ถูกลบ แล้วปิด stream
        for q in list(self.queues):
            try:
                q.put_nowait("event: revoked\ndata: token deleted\n\n")
                q.put_nowait(None)
            except:
                self.queues.discard(q)

    async def run(self):
        backoff = 1.0  # ค่าเริ่มต้น exponential backoff
        while self.has_subs():
            t0 = time.time()
            try:
                # ดึง snapshot จากกล้อง
                content = await asyncio.to_thread(fetch_snapshot_requests, self.url)
                arr = np.frombuffer(content, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is None:
                    raise RuntimeError("decode fail")

                # infer → ได้ count + conf
                count, conf_list = await asyncio.to_thread(infer_person_count, frame)
                conf_max = max(conf_list, default=0.0)

                # --- instant detect: เปิดไฟทันทีถ้าเจอ conf สูง ---
                if count > 0 and conf_max >= INSTANT_CONF and self.last_state == "no_person":
                    self.last_state = "person"

                # --- stable detect: ปิดไฟช้า (ต้องเจอ 0 ติดกัน n frame) ---
                self.history.append(count)
                if len(self.history) > STABLE_WINDOW:
                    self.history.pop(0)

                if len(self.history) >= STABLE_FRAMES and all(c == 0 for c in self.history[-STABLE_FRAMES:]) and self.last_state == "person":
                    self.last_state = "no_person"
                    self.last_count = 0
                elif self.last_state == "person":
                    non_zero_history = [c for c in self.history if c > 0]
                    if non_zero_history:
                        # ใช้ mode ใน window เพื่อลดการสวิงจากกล่อง YOLO
                        self.last_count = Counter(non_zero_history).most_common(1)[0][0]

                # --- publish state-based count (output ที่นิ่งกว่า raw) ---
                publish_val = self.last_count if self.last_state == "person" else 0
                await self.push(f"event: count\ndata: {publish_val}\n\n")

                # --- log raw detect (เอาไว้ debug/conf tuning) ---
                log_conf = ", ".join([f"{c*100:.2f}%" for c in conf_list])
                log_msg = f"เจอคน {count} คน: {log_conf}"
                await self.push(f"event: log\ndata: {log_msg}\n\n")

                self._last = t0
                backoff = 1.0

            except Exception as e:
                # error → push event ไปหา client
                await self.push(f"event: error\ndata: {json.dumps({'err': str(e)[:100]})}\n\n")
                await asyncio.sleep(min(backoff, 10.0))
                backoff *= 1.5  # เพิ่ม delay ทีละ step

            # ส่ง heartbeat กัน connection timeout
            if time.time() - self._last > HEARTBEAT_SEC:
                await self.push(": ping\n\n")
                self._last = time.time()

            # รอให้ครบ interval ที่กำหนด (publish steady)
            await asyncio.sleep(max(0.0, self.interval - (time.time() - t0)))

# Manager = ดูแล worker หลายตัวพร้อมกัน (ตาม token)
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
# สมัคร subscription ใหม่
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

# ดูรายการ subs ทั้งหมด
@app.get("/subs")
def list_subs():
    rows = conn.execute("SELECT token,snapshot_url,interval_sec,created_at FROM subscriptions").fetchall()
    return {"items": [{"token": t, "url": u, "interval": i, "created_at": ts} for (t, u, i, ts) in rows]}

# ลบ subscription
@app.delete("/subs/{token}")
async def delete_sub(token: str):
    with conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE token=?", (token,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="token not found")
    await manager.revoke(token)
    return {"ok": True}

# เปิด SSE stream สำหรับ token
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
            yield ": hello\n\n"  # initial hello
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

