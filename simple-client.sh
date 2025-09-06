# สมัคร
curl -s -X POST http://localhost:8000/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"snapshot_url":"http://192.168.137.173:13221/snapshot.cgi?user=admin&pwd=888888","interval_sec":2}'

# สมมุติได้ {"subscription_token":"Oem6raDJedbc"}

# เปิด SSE
curl -N http://localhost:8000/stream/Pa01qWRbBASi

# ดูรายการ
curl -s http://localhost:8000/subs | jq .

# ลบ
curl -s -X DELETE http://localhost:8000/subs/W1PuWkiyH1vz
