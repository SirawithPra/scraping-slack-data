# ภาพหน้าจอที่ใช้ใน README

รูปในโฟลเดอร์นี้ **commit ขึ้น git** — ต่างจาก [`../img/`](../img/) ที่เป็นรูปแมวส่วนตัว
และถูก `.gitignore` ไว้ทั้งโฟลเดอร์ เหตุผลง่าย ๆ คือรูปพวกนี้ไม่ใช่รูปส่วนตัว
มันคือหน้าจอของโปรแกรมที่รันบน**ข้อมูลตัวอย่างที่อยู่ในรีโปอยู่แล้ว**

## มาจากไหน

ทุกรูปมาจาก `data/sample/synthetic_work_chat.json` (แชทสังเคราะห์ 1,000 ข้อความ)
merge กับ `data/sample/standup.vtt` — **ไม่มีข้อมูลลูกค้าจริงอยู่ในรูปใดเลย**
ชื่อคนที่เห็น (มาย, นนท์, พี่ก้อง, Bob, Alice) เป็นชื่อในไฟล์สังเคราะห์

สร้างซ้ำได้ด้วยสามคำสั่งนี้ ซึ่งเป็นชุดเดียวกับที่เขียนไว้ใน README:

```bash
cd pipeline
python3 -m tam.ingest.prepare_messages --raw data/sample/synthetic_work_chat.json \
        --out data/processed/syn.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt \
        --title "Daily standup" --started 2026-08-14T09:30 \
        --merge-into data/processed/syn.json
python3 -m tam.web.server --records data/processed/syn.json --days 3650 --port 8899
```

จะได้ `Ready: 938 record(s), 76 topic(s), 2 blocked, summariser template`

## ถ่ายด้วยอะไร

Chrome headless กว้าง 1440 · `--force-device-scale-factor=2` เลยได้ไฟล์กว้าง 2880 px
(จอ retina อ่านออก) ธีมเป็นค่าตั้งต้นคือโหมดมืด

```bash
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
"$CHROME" --headless --disable-gpu --hide-scrollbars \
          --force-device-scale-factor=2 --window-size=1440,1150 \
          --virtual-time-budget=8000 \
          --screenshot=dashboard-digest.png "http://127.0.0.1:8899/"
```

| ไฟล์ | หน้า | หมายเหตุ |
|---|---|---|
| `dashboard-digest.png` | `/` | ตัวเลขสี่ช่อง แล้วรายการเรื่องที่ขยับ |
| `dashboard-blockers.png` | `/blockers` | เรื่องที่ติด เรียงจากค้างนานสุด |
| `item-timeline.png` | `/item/21` | **crop** เอาเฉพาะไทม์ไลน์ — ดูหมายเหตุข้างล่าง |
| `search-why.png` | `/search?q=display_status` | คำที่ตรงถูกไฮไลต์ พร้อมปุ่มบอกว่าทำไมถึงเจอ |

`item-timeline.png` ถูก crop ด้วย `sips -c 1620 2880 --cropOffset 1880 0` เพราะช่วงบน
ของหน้ามีกล่อง "เกิดอะไรขึ้นบ้าง ตามลำดับ" ที่ยัง**ว่าง**อยู่บนข้อมูลตัวอย่าง —
กราฟความสัมพันธ์ยังไม่จับคู่อะไรได้จากไฟล์สังเคราะห์ (`เหตุการณ์ 0`) การ crop คือ
การเลี่ยงกล่องว่าง ไม่ใช่การซ่อนตัวเลขที่ขัดกับคำอธิบาย ถ้าแก้เรื่องนี้ได้แล้ว
ให้ถ่ายใหม่แบบไม่ crop
