import os
import time
import json
import sqlite3
import secrets
import asyncio
from typing import Dict, Set, Optional, Tuple
import requests

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, AnyUrl, field_validator

import onnxruntime as ort

# ================== Config ==================
DB_PATH = os.getenv("DB_PATH", "subs.db")
MODEL_PATH = os.getenv("MODEL_PATH", "yolo11n.onnx")
IMG_SIZE = int(os.getenv("IMG_SIZE", "640"))
CONF_THRES = float(os.getenv("CONF_THRES", "0.35"))
IOU_THRES = float(os.getenv("IOU_THRES", "0.45"))
PERSON_CLASS_ID = int(os.getenv("PERSON_CLASS_ID", "0"))
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "15"))
ORT_THREADS = int(os.getenv("ORT_THREADS", "2"))

# ================== App & DB ==================
app = FastAPI(title="Human Count from Snapshot", version="0.3.0")

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

# ================== ONNX Runtime ==================
so = ort.SessionOptions()
so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
so.intra_op_num_threads = ORT_THREADS
so.inter_op_num_threads = 1
so.enable_mem_pattern = True
providers = ['CPUExecutionProvider']
provider_options = [{'intra_op_num_threads': ORT_THREADS}]
try:
    sess = ort.InferenceSession(MODEL_PATH, sess_options=so,
                                providers=providers, provider_options=provider_options)
except TypeError:
    sess = ort.InferenceSession(MODEL_PATH, sess_options=so, providers=providers)
inp_name = sess.get_inputs()[0].name

blob = np.empty((1, 3, IMG_SIZE, IMG_SIZE), dtype=np.float32)

# ================== Utils ==================
def letterbox(img: np.ndarray, new_shape=(IMG_SIZE, IMG_SIZE), color=(114,114,114)):
    h0, w0 = img.shape[:2]
    r = min(new_shape[0] / h0, new_shape[1] / w0)
    new_unpad = (int(round(w0 * r)), int(round(h0 * r)))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]
    dw *= 0.5; dh *= 0.5
    if (w0,h0) != new_unpad:
        interp = cv2.INTER_AREA if r < 1.0 else cv2.INTER_LINEAR
        img = cv2.resize(img, new_unpad, interpolation=interp)
    top, bottom = int(round(dh-0.1)), int(round(dh+0.1))
    left, right = int(round(dw-0.1)), int(round(dw+0.1))
    img = cv2.copyMakeBorder(img, top,bottom,left,right, cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw,dh)

def preprocess(frame: np.ndarray) -> Tuple[float, Tuple[float,float]]:
    img, r, dwdh = letterbox(frame, (IMG_SIZE, IMG_SIZE))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    blob[0] = np.transpose(img, (2,0,1))
    return r, dwdh

def sigmoid(x): return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))

def xywh2xyxy(xywh):
    x,y,w,h = xywh.T
    x1 = x - w*0.5; y1 = y - h*0.5
    x2 = x + w*0.5; y2 = y + h*0.5
    return np.stack([x1,y1,x2,y2], axis=1)

def nms(boxes, scores, iou_thres=0.45):
    if len(boxes)==0: return []
    boxes = boxes.astype(np.float32)
    x1,y1,x2,y2 = boxes.T
    areas = np.maximum(0,(x2-x1)) * np.maximum(0,(y2-y1))
    order = scores.argsort()[::-1]
    keep=[]
    while order.size>0:
        i=order[0]; keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2-xx1); h = np.maximum(0.0, yy2-yy1)
        inter = w*h
        ovr = inter / (areas[i]+areas[order[1:]]-inter+1e-9)
        inds = np.where(ovr<=iou_thres)[0]
        order = order[inds+1]
    return keep

def scale_coords(xyxy, r, dwdh, orig_shape):
    dw,dh = dwdh
    xyxy[:,[0,2]] -= dw; xyxy[:,[1,3]] -= dh
    xyxy /= max(r,1e-9)
    xyxy[:,[0,2]] = np.clip(xyxy[:,[0,2]], 0, orig_shape[1])
    xyxy[:,[1,3]] = np.clip(xyxy[:,[1,3]], 0, orig_shape[0])
    return xyxy

def decode_outputs(raw):
    pred = raw
    if isinstance(pred, list): pred = pred[0]
    pred = np.array(pred)
    if pred.ndim==3 and pred.shape[-1]==6:
        arr = np.squeeze(pred,axis=0) if pred.shape[0]==1 else pred
        boxes_xyxy = arr[:,:4].astype(np.float32)
        conf = arr[:,4].astype(np.float32)
        cls = arr[:,5]
        num = boxes_xyxy.shape[0]
        cls_probs = np.zeros((num,80),dtype=np.float32)
        cls_idx = np.clip(cls.astype(int),0,79)
        cls_probs[np.arange(num), cls_idx] = conf
        return boxes_xyxy, cls_probs, None, True
    if pred.ndim==3 and pred.shape[0]==1: pred = np.squeeze(pred,0)
    if pred.ndim==2 and pred.shape[0] in (84,85) and pred.shape[1]>100: pred=pred.T
    if pred.ndim!=2 or pred.shape[1] not in (84,85):
        raise ValueError(f"Unexpected output shape: {pred.shape}")
    C = pred.shape[1]
    boxes = pred[:,:4].astype(np.float32)
    if C==85:
        obj = pred[:,4].astype(np.float32); cls_probs = pred[:,5:].astype(np.float32)
    else:
        obj=None; cls_probs=pred[:,4:].astype(np.float32)
    if np.max(cls_probs)>1.0 or np.min(cls_probs)<0.0:
        cls_probs = sigmoid(cls_probs); 
        if obj is not None: obj = sigmoid(obj)
    if np.max(boxes)<=1.5: boxes *= float(IMG_SIZE)
    return boxes, cls_probs, obj, False

def infer_person_count(frame: np.ndarray) -> int:
    r,dwdh = preprocess(frame)
    raw_out = sess.run(None,{inp_name: blob})
    out0 = raw_out[0]
    try:
        boxes_in, cls_probs, obj, already_xyxy = decode_outputs(out0)
    except Exception:
        return 0
    cls_ids = np.argmax(cls_probs,axis=1)
    cls_max = np.max(cls_probs,axis=1)
    confs = cls_max if obj is None else (cls_max*obj)
    mask = (cls_ids==PERSON_CLASS_ID) & (confs>=CONF_THRES)
    boxes_sel = boxes_in[mask]; confs_sel=confs[mask]
    boxes_xyxy = boxes_sel if already_xyxy else xywh2xyxy(boxes_sel)
    keep = nms(boxes_xyxy, confs_sel, IOU_THRES)
    boxes_xyxy = boxes_xyxy[keep]; confs_sel=confs_sel[keep]
    boxes_xyxy = scale_coords(boxes_xyxy.copy(), r, dwdh, frame.shape)
    return int(len(boxes_xyxy))

def fetch_snapshot_requests(url: str) -> bytes:
    r = requests.get(url, headers={
        "User-Agent":"human-count/0.3",
        "Accept":"image/jpeg,*/*",
        "Cache-Control":"no-cache",
        "Pragma":"no-cache",
        "Connection":"close",
    }, timeout=5, verify=False, stream=False)
    r.raise_for_status()
    return r.content

# ================== Worker ==================
class Worker:
    def __init__(self, token:str, url:str, interval:int):
        self.token=token; self.url=url; self.interval=interval
        self.queues:Set[asyncio.Queue]=set()
        self.task:Optional[asyncio.Task]=None
        self._last=0.0
    def add(self,q): self.queues.add(q)
    def remove(self,q): self.queues.discard(q)
    def has_subs(self): return len(self.queues)>0
    async def push(self,data:str):
        for q in list(self.queues):
            try: q.put_nowait(data)
            except: self.queues.discard(q)
    async def revoke(self):
        # Notify clients and close their streams
        for q in list(self.queues):
            try:
                q.put_nowait("event: revoked\ndata: token deleted\n\n")
                q.put_nowait(None)
            except:
                self.queues.discard(q)
    async def run(self):
        backoff=1.0
        while self.has_subs():
            t0=time.time()
            try:
                content = await asyncio.to_thread(fetch_snapshot_requests, self.url)
                arr=np.frombuffer(content,dtype=np.uint8)
                frame=cv2.imdecode(arr,cv2.IMREAD_COLOR)
                if frame is None: raise RuntimeError("decode fail")

                count=await asyncio.to_thread(infer_person_count, frame)
                await self.push(f"event: count\ndata: {count}\n\n")
                self._last=t0; backoff=1.0
            except Exception as e:
                await self.push(f"event: error\ndata: {json.dumps({'err':str(e)[:100]})}\n\n")
                await asyncio.sleep(min(backoff,10.0)); backoff*=1.5
            if time.time()-self._last>HEARTBEAT_SEC:
                await self.push(": ping\n\n"); self._last=time.time()
            await asyncio.sleep(max(0.0,self.interval-(time.time()-t0)))

class Manager:
    def __init__(self): self.ws:Dict[str,Worker]={}; self.lock=asyncio.Lock()
    async def ensure(self,token,url,interval):
        async with self.lock:
            w=self.ws.get(token)
            if not w:
                w=Worker(token,url,interval); self.ws[token]=w
            w.interval=interval
            if not w.task or w.task.done():
                w.task=asyncio.create_task(self._run(w))
            return w
    async def _run(self,w:Worker):
        try: await w.run()
        finally:
            async with self.lock: self.ws.pop(w.token,None)
    async def revoke(self, token:str) -> bool:
        async with self.lock:
            w=self.ws.get(token)
        if not w:
            return False
        await w.revoke()
        return True

manager=Manager()

# ================== Routes ==================
@app.post("/subscribe")
def subscribe(inp:SubscribeIn):
    token=new_token(); ts=int(time.time())
    with conn:
        conn.execute("INSERT INTO subscriptions(token,snapshot_url,interval_sec,created_at) VALUES(?,?,?,?)",
                     (token,str(inp.snapshot_url),inp.interval_sec,ts))
    return {"subscription_token":token}

@app.get("/subs")
def list_subs():
    rows=conn.execute("SELECT token,snapshot_url,interval_sec,created_at FROM subscriptions").fetchall()
    return {"items":[{"token":t,"url":u,"interval":i,"created_at":ts} for (t,u,i,ts) in rows]}

@app.delete("/subs/{token}")
async def delete_sub(token:str):
    with conn:
        cur=conn.execute("DELETE FROM subscriptions WHERE token=?",(token,))
    if cur.rowcount==0:
        raise HTTPException(status_code=404,detail="token not found")
    # Revoke any active SSE streams for this token
    await manager.revoke(token)
    return {"ok":True}

@app.get("/stream/{token}")
async def stream(token:str, request:Request):
    row=conn.execute("SELECT snapshot_url,interval_sec FROM subscriptions WHERE token=?",(token,)).fetchone()
    if not row: raise HTTPException(status_code=404,detail="token not found")
    url,itv=row; q=asyncio.Queue(maxsize=10)
    w=await manager.ensure(token,url,itv); w.add(q)
    async def gen():
        try:
            yield ": hello\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    msg=await asyncio.wait_for(q.get(),timeout=HEARTBEAT_SEC+5)
                    if msg is None:
                        break
                    yield msg
                except asyncio.TimeoutError: yield ": idle\n\n"
        finally: w.remove(q)
    return StreamingResponse(gen(), media_type="text/event-stream")
