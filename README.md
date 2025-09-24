# Human Counting Service

ตอนแรกแค่อยากทำระบบเปิดปิดไฟอัตโนมัติในห้องประชุม ใครจะไปคิดว่ามันจะซับซ้อนขนาดนี้...

## เรื่องราวเริ่มต้น

ที่ทำงานพนักงานชอบลืมปิดไฟ แอร์ จนบิลไฟฟ้าพุ่ง เลยคิดว่าเอา Pi3B+ ที่เหลือใช้ มาต่อกับกล้อง IP แล้วใช้ YOLO detect คน เจอคนก็เปิดไฟ ไม่เจอก็ปิด ง่ายมั้ย?

**ผิดโดยสิ้นเชิง**

## ปัญหาที่ไม่คิดมาก่อน

ลองรันดูแล้ว YOLO มันไม่เสถียร frame นึงเจอ 2 คน frame ถัดไปเจอ 0 คน แล้วก็ 1 คน อีก frame เจอ 3 คน คนไม่ได้วิ่งไปไหนนะ แต่ detection มันกระโดดไปมา

เอาไปต่อกับรีเลย์ไฟโดยตรง → ไฟก็กระพริบ นึกว่าผับ

เลยต้องหาทางแก้ให้มันเวิร์กขึ้น

## Trick แรก: Instant + Stable Detection

1. **เปิดไฟ = เร็ว** เจอคน confidence >= 0.32 เปิดทันที
2. **ปิดไฟ = มั่นใจ** ต้องไม่เจอคนติดกัน 3 frame ถึงจะปิด

```python
# instant detect (เปิดไฟทันที)
if count > 0 and conf_max >= INSTANT_CONF and self.last_state == "no_person":
    self.last_state = "person"

# stable detect (ปิดไฟช้า)
self.history.append(count)
if all(c == 0 for c in self.history[-STABLE_FRAMES:]) and self.last_state == "person":
    self.last_state = "no_person"
```

แบบนี้ไฟเปิดปิดได้โอเคขึ้น ไม่มั่วไปมา

## Trick สอง: Async ทุกอย่าง

Pi3B+ อ่อนแอ YOLO inference ช้า ถ้าให้มันรอ sync จะค้าง SSE stream

เลยใช้ `asyncio.to_thread()` ทั้ง snapshot fetch และ YOLO inference

```python
content = await asyncio.to_thread(fetch_snapshot_requests, self.url)
count, conf_list = await asyncio.to_thread(infer_person_count, frame)
```

Event loop ไม่ block, SSE ไม่ค้าง

## Trick สาม: Decoupling ระหว่าง Detection กับ Publish

ตอนแรกคิดว่า detect ได้กี่เฟรมต่อวินาที ก็ publish เท่านั้น แต่บน Pi3B+ มันได้แค่ 1-2 fps ทำให้ external system รอนาน

เลยแยกออกมา:
- Detection worker รันด้วย fps ที่เครื่องทำได้
- แต่ publish state ทุก `interval_sec` ที่ user กำหนด

External system เห็น stream สม่ำเสมอ ไม่กระชาก

## Trick สี่: Backoff Recovery

กล้อง IP บางตัวมันล่ม network บางทีขาด แต่เราไม่อยากให้ service crash

```python
try:
    # do work
    backoff = 1.0
except Exception as e:
    await self.push(f"event: error\ndata: {str(e)}\n\n")
    await asyncio.sleep(min(backoff, 10.0))
    backoff *= 1.5
```

Error แล้วถอยหลัง 1s → 1.5s → 2.25s → ... → หยุดที่ 10s แล้ว auto recover

## Trick ห้า: Token-based Subscription

ใช้ random token ง่ายๆ:

```python
def new_token(n: int = 12) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
```

Stateless, lightweight, ปลอดภัยพอสำหรับ edge device (มั้ง)

## ผลลัพธ์ที่ได้

ปรับแต่งมา 3 อาทิตย์ ตอนนี้:
- Pi3B+ รันได้เสถียร ไม่ crash (แต่แอบๆร้อน)
- ไฟเปิดปิดได้โอเค ไม่มั่วไปมา
- ทีมงานไม่บ่นแล้ว

## วิธีใช้

```bash
# ติดตั้ง
pip install -r requirements.txt

# รัน
uvicorn main:app --host 0.0.0.0 --port 8000

# สมัคร
curl -X POST http://localhost:8000/subscribe \
  -H "Content-Type: application/json" \
  -d '{"snapshot_url":"http://192.168.1.100/snapshot.jpg","interval_sec":2}'

# เปิด stream
curl -N http://localhost:8000/stream/{token}
```

## API Endpoints ทั้งหมด

**POST /subscribe** - สร้าง subscription ใหม่
```json
{
  "snapshot_url": "http://192.168.1.100/snapshot.jpg",
  "interval_sec": 2
}
```
Response: `{"subscription_token": "abc123xyz789"}`

**GET /subs** - ดูรายการ subscription ทั้งหมด
```json
{
  "items": [
    {
      "token": "abc123xyz789",
      "url": "http://192.168.1.100/snapshot.jpg", 
      "interval": 2,
      "created_at": 1704067200
    }
  ]
}
```

**DELETE /subs/{token}** - ลบ subscription
Response: `{"ok": true}`

**GET /stream/{token}** - เปิด SSE stream รับข้อมูล real-time

## Event Types ใน SSE Stream

- `event: count` - จำนวนคนในห้อง (0 หรือ 1 หลัง instant/stable processing)
- `event: log` - ข้อมูล debug แสดง detection ดิบๆ + confidence
- `event: error` - เมื่อเกิด error (network, กล้องล่ม, etc.)
- `event: revoked` - token ถูกลบแล้ว stream จะปิด
- `: ping` / `: idle` - heartbeat กัน connection timeout

## Config ละเอียดยิบ (สำคัญมาก!)

**Environment Variables ที่ต้องรู้จัก:**

1. **DB_PATH** — พาธไฟล์ SQLite ที่เก็บ subscription
   ไฟล์นี้เก็บ token, snapshot_url, interval_sec, created_at ของแต่ละงานที่สมัครไว้ ถ้าตั้งไว้ในโฟลเดอร์ที่สิทธิ์เขียนไม่พอ บริการจะเด้งเงียบๆ ตอน INSERT หรือ DELETE แล้วคุณจะงงว่า "ทำไม token ไม่มา/ไม่หาย" ทางที่ดีชี้ไป path ที่เขียนได้แน่ๆ เช่นโฟลเดอร์ทำงานปัจจุบัน หรือ `/var/lib/human-count/subs.db`

2. **MODEL_PATH** — พาธไฟล์โมเดล YOLO ที่จะโหลด
   จุดชี้เป็นหัวใจของความเร็วและความแม่นยำ: บน Pi3B+ ใช้ `yolo11n.pt` จะสบายใจกว่า ส่วน Pi4/Pi5 ใช้ `yolo11s.pt` ก็ยังไหว ถ้าใส่ path ผิด/ไฟล์เสีย โมเดลโหลดไม่ขึ้นตั้งแต่เริ่มและแอปจะล้มทันที

3. **IMG_SIZE** — ขนาดภาพอินพุตให้โมเดล (พิกเซลด้านยาว)
   มันคือตัวคุม trade-off ระหว่าง "เร็ว" กับ "แม่น": ยิ่งเล็กยิ่งเร็ว แต่รายละเอียดหายและ conf ตก โดยทั่วไป 640 คือค่ากลางที่ดีสำหรับเครื่องแรงหน่อย ส่วน Pi3B+ ถ้าอยากหายใจ ให้ลอง 416 หรือ 480

4. **CONF_THRES** — เกณฑ์คัดกรอง "กล่องกาก" ที่ระดับโมเดล
   นี่คือ filter ชั้นแรกของ YOLO: กล่องที่มีความมั่นใจต่ำกว่าเกณฑ์นี้จะไม่ถูกส่งต่อออกมาจากโมเดลเลย ช่วยลด noise และภาระ CPU หลังบ้าน สำหรับงานคนในห้อง ตั้งแถว 0.25–0.35 กำลังดี

5. **IOU_THRES** — เกณฑ์ NMS (Non-Max Suppression) เวลา "กล่องทับกัน"
   เมื่อโมเดลยิงกล่องมาหลายใบที่ทับกัน NMS จะเลือกทิ้งให้เหลือใบที่ดีที่สุดโดยดู IoU = พื้นที่ซ้อน/พื้นที่รวม ตั้ง 0.45 เป็นค่าพื้นฐานดีๆ สำหรับคนในห้อง

6. **PERSON_CLASS_ID** — หมายเลขคลาสของ "คน"
   โมเดล COCO มาตรฐานคือ `0 = person` ถ้าใช้โมเดล custom ที่แม็พคลาสไม่เหมือน COCO ต้องเช็ก mapping ให้ตรง ไม่งั้นจะไปนับเก้าอี้เป็นคนแล้วงานงอก

7. **HEARTBEAT_SEC** — จังหวะส่งชีพให้ SSE ไม่หลุด
   ถ้าไม่มี event วิ่งผ่าน connection นานๆ พร็อกซีบางตัวจะตัดทิ้ง เราเลยยิง `: ping` ทุกๆ x วินาที ตั้ง 15 วิเป็นกลางๆ ถ้าเน็ต/พร็อกซีดื้อก็ลดเหลือ 5–10 วิได้

8. **STABLE_WINDOW** — ขนาดหน้าต่างจำเฟรมล่าสุด (history length)
   เราเก็บ `count` ย้อนหลังไว้เพื่อใช้ตัดสินใจ "ปิดไฟช้า" เวลาไม่มีคนจริงๆ ค่านี้ควร ≥ `STABLE_FRAMES` เสมอ ปกติ 5 กำลังดี: หน้าต่างไม่ยาวเกินจนหน่วงการตอบสนอง และยาวพอจะกันเฟรมหลุดๆ

9. **STABLE_FRAMES** — ต้อง "0 คน" ติดกันกี่เฟรม ถึงจะกล้าเปลี่ยนสถานะเป็นไม่มีคน
   นี่คือหัวใจของ "ไม่ปิดไฟมั่ว": ถ้าตั้ง 3 หมายถึงต้องเห็น 0 ติดต่อ 3 ครั้งใน history ถึงจะสับสวิตช์เป็น `no_person` ถ้าตั้ง 1 ระบบจะไวแต่ปิดพลาดบ่อย ถ้าตั้ง 5 จะนิ่งมากแต่ปิดช้า แนะนำ 3 เป็นจุดพอดี

10. **INSTANT_CONF** — เกณฑ์ conf สำหรับ "เปิดไฟเร็ว" ทันทีที่พบคน
    ต่างจาก `CONF_THRES` ที่เป็น filter ชั้นโมเดล ตัวนี้คือเกณฑ์เชิงพฤติกรรม ใช้ตัดสินใจสับ state จาก `no_person` → `person` แบบไม่ต้องรอ history ถ้าหน้างานจริงของคุณ conf มักแกว่ง 0.30–0.48 ก็จูนไว้แถว 0.32–0.35 จะกำลังดี

11. **interval_sec** (มาจาก request ไม่ใช่ env) — ความถี่ในการ "พ่นค่าออกทาง SSE"
    อันนี้คือฝั่ง publish ไม่ใช่ความถี่ YOLO จริงๆ เรา decouple ออกแล้ว: YOLO จะวิ่งได้เท่าไรช่างมัน แต่เราจะส่ง `event: count` ตาม interval ตายตัวเพื่อให้ระบบข้างนอกอ่านค่าได้เสถียร เช่นตั้ง 2 วินาที บน Pi3B+ จะพอดีๆ ไม่หอบเกิน

**สรุปเก็บตก:** `CONF_THRES` กันกล่องขยะ, `IOU_THRES` กันกล่องซ้ำ, `INSTANT_CONF` ทำให้ "เปิดไว", `STABLE_FRAMES` ทำให้ "ปิดชัวร์", `STABLE_WINDOW` ให้บริบทพอจะตัดสิน, `IMG_SIZE` เป็นคันเร่ง/เบรกของทั้งระบบ ส่วน `interval_sec` คือจังหวะที่คุณสื่อสารกับโลกภายนอก—อย่าเอาไปผูกกับ fps ของโมเดล

## Config แนะนำตามเครื่อง

**Pi3B+**: 
```bash
export IMG_SIZE=416
export CONF_THRES=0.3
export IOU_THRES=0.45
export INSTANT_CONF=0.32
export STABLE_FRAMES=3
export STABLE_WINDOW=5
export MODEL_PATH="yolo11n.pt"
# interval_sec=2 ใน request
```

**Pi4/Pi5**: 
```bash
export IMG_SIZE=640
export CONF_THRES=0.25
export IOU_THRES=0.45
export INSTANT_CONF=0.35
export STABLE_FRAMES=3
export STABLE_WINDOW=5
export MODEL_PATH="yolo11s.pt"  
# interval_sec=1-2 ใน request
```

**Jetson Nano**: 
```bash
export IMG_SIZE=640
export CONF_THRES=0.25
export IOU_THRES=0.4
export INSTANT_CONF=0.35
export STABLE_FRAMES=2
export STABLE_WINDOW=4
export MODEL_PATH="yolo11s.pt"
# interval_sec=1 ใน request + ใส่ TensorRT จะเร็วขึ้นเยอะ
```

## เคล็ดลับการ Deploy

กล้อง IP บ้างตัวใช้ self-signed cert ก็ตั้ง verify=False ได้เลย

เช็คว่าไฟล์โมเดลไม่ใช่ 0 ไบต์ก่อนรัน

ถ้าต้องการ debug ให้เปิด log level เป็น DEBUG

Memory leak บน Pi3B+ ถ้ารันนานๆ ให้ restart service ทุก 24 ชม.

## Troubleshooting ปัญหาที่เจอบ่อย

**1. "token not found" ทั้งๆ ที่เพิ่งสมัคร**
   - เช็ค DB_PATH ว่าสิทธิ์เขียนได้ไหม
   - ลอง `ls -la subs.db` ดูขนาดไฟล์ ไม่ใช่ 0 ไบต์

**2. Stream ไม่มีข้อมูลมา หรือขาดๆ หายๆ**
   - เช็คว่ากล้องตอบ snapshot_url ได้ไหม: `curl -I http://your-camera/snapshot.jpg`
   - ลองลด interval_sec หรือเพิ่ม IMG_SIZE ดู
   - ดู event: error ใน stream มีอะไรบ้าง

**3. Detection กระโดดไปมา ไม่เสถียร**
   - เพิ่ม STABLE_FRAMES (แต่ปิดไฟจะช้าขึ้น)
   - ลด CONF_THRES หรือ INSTANT_CONF 
   - เช็คแสงในห้อง เงาคนอาจทำให้ conf ผันผวน

**4. CPU หอบ หรือ memory ใช้เยอะ**
   - Pi3B+: ลด IMG_SIZE เหลือ 320-416
   - ใช้โมเดลเบากว่า yolo11n.pt แทน yolo11s.pt
   - เพิ่ม interval_sec ให้มากขึ้น

**5. Connection หลุดบ่อย**
   - ลด HEARTBEAT_SEC ลงเหลือ 10 หรือ 5 วินาที
   - เช็คว่า reverse proxy (nginx/apache) มี timeout settings
   - ใส่ Connection: keep-alive ใน HTTP headers

## ตัวอย่างการใช้งานจริง

**เปิดปิดไฟผ่าน Home Assistant:**
```yaml
# configuration.yaml
sensor:
  - platform: rest
    resource: http://pi.local:8000/stream/YOUR_TOKEN
    method: GET
    name: "meeting_room_occupancy"
    scan_interval: 1

automation:
  - alias: "Turn on lights when person detected"
    trigger:
      platform: state
      entity_id: sensor.meeting_room_occupancy
      to: '1'
    action:
      service: light.turn_on
      entity_id: light.meeting_room
```

**เชื่อมกับ Node-RED:**
```javascript
// HTTP Request node ตั้ง URL: http://pi.local:8000/stream/YOUR_TOKEN
// แล้วใช้ function node แปลง SSE เป็น msg.payload
var lines = msg.payload.split('\n');
for(var line of lines) {
    if(line.startsWith('data: ')) {
        msg.payload = parseInt(line.substring(6));
        return msg;
    }
}
```

## Dependencies (requirements.txt)

```txt
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
ultralytics>=8.0.0
opencv-python>=4.8.0
numpy>=1.24.0
pydantic>=2.0.0
requests>=2.31.0
```

## Docker Deploy (ถ้าอยากสะดวก)

```dockerfile
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libglib2.0-0 libsm6 libxext6 libxrender-dev \
    libgomp1 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
# Build & Run
docker build -t human-counter .
docker run -p 8000:8000 -v $(pwd)/data:/app/data \
  -e DB_PATH=/app/data/subs.db \
  -e MODEL_PATH=/app/yolo11n.pt \
  human-counter
```

---

*ปล. โค้ดนี้เขียนขึ้นมาแค่พอเป็น MVP เพื่อแก้ปัญหาจริง ถ้าจะเอาไปใช้ production อย่าลืม error handling กับ security เพิ่มเติมด้วย*