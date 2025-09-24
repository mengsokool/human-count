# สมัคร
curl -s -X POST http://localhost:8000/subscribe \
  -H 'Content-Type: application/json' \
  -d '{"snapshot_url":"http://10.16.11.200:51642/snapshot.cgi?user=admin&pwd=888888","interval_sec":2}'

# สมมุติได้ {"subscription_token":"Oem6raDJedbc"}

# เปิด SSE
curl -N http://localhost:8000/stream/ZkO02xDBMwWu

# ดูรายการ
curl -s http://localhost:8000/subs | jq .

# ลบ
curl -s -X DELETE http://localhost:8000/subs/W1PuWkiyH1vz
