# Human Count + SSE
<img width="770" height="498" alt="image" src="https://github.com/user-attachments/assets/ba101dd6-6509-4ab8-ab8d-26466ef75e94" />

**ภาพรวม**
- นับจำนวนคนจาก snapshot ด้วย Ultralytics YOLO แล้วสตรีมผลแบบ SSE
- ให้บริการผ่าน FastAPI พร้อม endpoint สมัคร/ลบ subscription ด้วยโทเคน

**ความต้องการระบบ**
- Python 3.10 ขึ้นไป
- ติดตั้งแพ็กเกจ: `pip install -r requirements.txt`
- ไฟล์โมเดล YOLO: วาง `yolo11n.pt` ไว้ในโฟลเดอร์ทำงาน (หรือกำหนดพาธผ่าน `MODEL_PATH`)

**เริ่มรัน**
- Development:  
  ```bash
  uvicorn main:app --reload
  ```

- Production (ตัวอย่าง):
  ```bash
  uvicorn main:app --host 0.0.0.0 --port 8000
  ```

**การตั้งค่า (Environment Variables)**

* `DB_PATH`: พาธฐานข้อมูล SQLite (ค่าเริ่มต้น `subs.db`)
* `MODEL_PATH`: พาธไฟล์โมเดล YOLO (ค่าเริ่มต้น `yolo11n.pt`)
* `IMG_SIZE`: ขนาดภาพอินพุตโมเดล (ค่าเริ่มต้น `640`)
* `CONF_THRES`: ค่าความเชื่อมั่นขั้นต่ำ (ค่าเริ่มต้น `0.35`)
* `IOU_THRES`: ค่า IoU สำหรับ NMS (ค่าเริ่มต้น `0.45`)
* `PERSON_CLASS_ID`: หมายเลขคลาสสำหรับคน (ค่าเริ่มต้น `0`)
* `HEARTBEAT_SEC`: ระยะส่งสัญญาณคงชีพในสตรีม (ค่าเริ่มต้น `15`)

**Endpoints**

* `POST /subscribe`
  สมัครรับบริการนับคนจาก URL ของรูป snapshot ตามช่วงเวลา
  Body:

  ```json
  { "snapshot_url": "<AnyUrl>", "interval_sec": 2 }
  ```

  Response:

  ```json
  { "subscription_token": "<token>" }
  ```

* `GET /subs`
  ดูรายการ subscription ทั้งหมด
  Response:

  ```json
  {
    "items": [
      { "token": "...", "url": "...", "interval": 2, "created_at": 1712345678 }
    ]
  }
  ```

* `DELETE /subs/{token}`
  ลบ subscription ตามโทเคน
  หากมีการเชื่อมต่อ SSE อยู่ จะส่งอีเวนต์ `revoked` แล้วปิดสตรีมทันที
  Response:

  ```json
  { "ok": true }
  ```

* `GET /stream/{token}`
  เปิดสตรีม SSE (`Content-Type: text/event-stream`)
  ต้องมีโทเคนที่สมัครไว้ก่อนหน้า

**รูปแบบอีเวนต์ SSE**

* `event: count` + `data: <int>` → จำนวนคนที่ตรวจพบล่าสุด
* `event: error` + `data: {"err":"..."}` → เกิดข้อผิดพลาดระหว่างดึงรูป/ประมวลผล
* คอมเมนต์ heartbeat: `: ping` และ `: idle` ใช้รักษาการเชื่อมต่อ
* เมื่อมีการลบโทเคน: `event: revoked` + `data: token deleted` แล้วการเชื่อมต่อจะปิด

**ตัวอย่างการใช้งานด้วย curl**

* สมัคร:

  ```bash
  curl -s -X POST http://localhost:8000/subscribe \
    -H "Content-Type: application/json" \
    -d '{"snapshot_url":"http://<ip>:<port>/snapshot.jpg","interval_sec":2}'
  ```
* เปิดสตรีม:

  ```bash
  curl -N http://localhost:8000/stream/<token>
  ```
* ดูรายการ:

  ```bash
  curl -s http://localhost:8000/subs
  ```
* ลบโทเคน:

  ```bash
  curl -s -X DELETE http://localhost:8000/subs/<token>
  ```

**ฐานข้อมูล**

* SQLite ไฟล์เดียว (กำหนดตำแหน่งด้วย `DB_PATH`)
* ตาราง:

  ```sql
  CREATE TABLE subscriptions (
    token TEXT PRIMARY KEY,
    snapshot_url TEXT,
    interval_sec INTEGER,
    created_at INTEGER
  );
  ```

**หมายเหตุด้านความปลอดภัยและการใช้งาน**

* การดึง snapshot ใช้ `requests` พร้อม `verify=False` → ใช้ในเครือข่ายที่เชื่อถือได้เท่านั้น
* อย่าใส่รหัสผ่านจริงในตัวอย่างสาธารณะ หากจำเป็นให้ใช้บัญชีเฉพาะกิจ
* โมเดลทำงานบน CPU โดยใช้ PyTorch (ผ่าน Ultralytics)

**การดีบัก/แก้ไขปัญหา**

* สตรีมไม่อัปเดต: ตรวจสอบว่า snapshot URL เข้าถึงได้
* ค่า `interval_sec` ต่ำเกินไปอาจทำให้โหลดสูง
* ถ้าโมเดลผิดพลาด/ไฟล์ไม่พบ: ตรวจ `MODEL_PATH` และ `IMG_SIZE`

**สคริปต์ตัวอย่าง**

* ดู `simple-client.sh` สำหรับคำสั่ง curl ครบวงจร
