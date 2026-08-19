# สิ่งที่ส่ง — ตาม (Tam)

**ผู้ส่ง:** สิรวิชญ์ · **วันที่:** 20 ส.ค. 2569 · **กำหนดส่ง:** 19 ส.ค. 2569
**รีโป:** <https://github.com/SirawithPra/scraping-slack-data> (public)

> เอกสารนี้คือใบปะหน้า ไม่ใช่ตัวงาน — มีไว้ให้คนตรวจรู้ว่าของสี่อย่างที่ขอ
> อยู่ตรงไหน และตรวจตัวเลขที่อ้างไว้ได้เองอย่างไร

---

## ของสี่อย่างที่ขอ · อยู่ตรงไหน

| # | รายการที่ขอ | ส่งเป็น | สถานะ |
|---|---|---|---|
| 1 | **Working Prototype** | รันได้จากรีโปโดยไม่ต้องมี Slack token — ดู [ตรวจเองได้ใน 2 นาที](#ตรวจเองได้ใน-2-นาที) | ✓ |
| 2 | **User Manual** | [docs/USER_MANUAL.md](docs/USER_MANUAL.md) — ไทย ติดตั้ง/ใช้งาน/ต่อ Slack/แก้ปัญหา · การใช้รายวัน [docs/DAILY_USE.md](docs/DAILY_USE.md) | ✓ |
| 3 | **Draft Presentation** | [docs/deck.html](docs/deck.html) 10 สไลด์ · **[PDF](docs/pdf/deck.pdf)** สำหรับเปิดครั้งเดียวจบ | ✓ |
| 4 | **Source Code** | รีโปทั้งก้อน — Python (pipeline) + TypeScript (slack-bot) | ✓ |

**ตอบคำถามที่ตัวเองถามก่อนส่ง: ลิงก์รีโปอย่างเดียวไม่พอ** เพราะข้อ 3 เป็นไฟล์ HTML
ที่ GitHub โชว์เป็น source code ไม่ render — จึง export เป็น PDF ไว้ให้ด้วย และเพราะ
prototype ที่ต้องโหลดโมเดล 2.2 GB ก่อนเห็นหน้าจอ ไม่มีใครรันตอนตรวจ — จึงมี
[ภาพหน้าจอ](README.md#what-it-looks-like) อยู่ใน README แล้ว

---

## ถ้ามีเวลา 5 นาที ให้กดสามอย่างนี้

1. **[README — What it looks like](README.md#what-it-looks-like)** — สี่ภาพหน้าจอ
   จากข้อมูลตัวอย่างที่ commit ไว้ เห็นของจริงโดยไม่ต้องติดตั้งอะไร
2. **[docs/pdf/deck.pdf](docs/pdf/deck.pdf)** — 10 สไลด์ ปัญหา → ทางแก้ → สถาปัตยกรรม → ข้อจำกัด
3. **[docs/EXPERIMENTS.md](docs/EXPERIMENTS.md)** — ที่อยู่ของงานวัดผล: เลือก `BAAI/bge-m3`
   เพราะวัดแล้วชนะ และ **fine-tune ที่เทรนเองสองตัวแพ้แบบวัดได้ จึงไม่ถูกใช้** —
   ตัวเลขทุกตัวมีวิธีวัดกำกับ

เอกสารทางเทคนิคฉบับเต็มอยู่ที่ [docs/architecture.html](docs/architecture.html)
([PDF](docs/pdf/architecture.pdf)) · ที่มาของทุกตัวเลขอยู่ที่
[docs/CALCULATION.md](docs/CALCULATION.md) · ศัพท์ที่ใช้อยู่ที่
[docs/GLOSSARY.md](docs/GLOSSARY.md)

---

## ข้อออกแบบที่เป็นหัวใจของงาน

สามข้อนี้คือสิ่งที่ทำให้มันต่างจาก "อีกหนึ่ง dashboard" และเป็นสิ่งที่ควรตรวจ

1. **สถานะคำนวณด้วยกฎ ไม่ใช่ด้วยโมเดลภาษา** — `blocked` / `active` / `resolved`
   มาจากกฎบนความสัมพันธ์ที่มีชนิด โมเดลเขียนได้แค่ประโยคสรุปหนึ่งบรรทัด
2. **ทุกคำกล่าวอ้างมีหลักฐานติดมา** — "ค้างมา 6 วัน" กดเข้าไปเจอข้อความที่พูดไว้จริง
   ถ้าสรุปไหนไม่มี citation ที่ยืนยันได้ในโค้ด หน้าจอจะติดป้าย `unverified` ให้เห็น
3. **รายงานที่ตัวงาน ไม่ใช่ที่ตัวคน** — ไม่มีการนับข้อความต่อคน ไม่มีกระดานจัดอันดับ
   เพราะวินาทีที่มันให้ความรู้สึกเป็นการสอดส่อง ทีมจะเลิกใช้ และงานนี้จะทำให้ชีวิตแย่ลง

---

## ตรวจเองได้ใน 2 นาที

ไม่ต้องมี Slack token ไม่ต้องมี API key — ข้อมูลตัวอย่างไทย/อังกฤษ commit ไว้ในรีโปแล้ว

```bash
git clone https://github.com/SirawithPra/scraping-slack-data.git
cd scraping-slack-data && python3 -m venv .venv && source .venv/bin/activate
cd pipeline && python3 -m pip install -r requirements.txt

python3 -m tam.ingest.prepare_messages --raw data/sample/synthetic_work_chat.json \
        --out data/processed/syn.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt \
        --title "Daily standup" --started 2026-08-14T09:30 \
        --merge-into data/processed/syn.json
python3 -m tam.web.server --records data/processed/syn.json --days 3650 --port 8899
```

**ควรได้:** `Ready: 938 record(s), 76 topic(s), 2 blocked, summariser template`
แล้วเปิด <http://localhost:8899>

`summariser template` คือประเด็น — ไม่ได้ตั้ง key ของโมเดลใด ๆ ประโยคสรุปมาจาก
เทมเพลต แต่ตัวเลขบนหน้าจอครบเหมือนเดิม เพราะตัวเลขไม่เคยเป็นงานของโมเดล

**ค่าใช้จ่ายรอบแรก:** `BAAI/bge-m3` เป็นไฟล์ 2.2 GB โหลดลง `~/.cache/huggingface`
ครั้งเดียว · `--days 3650` ไม่ใช่ typo — หน้าต่าง default คือ 7 วัน แต่ข้อมูลตัวอย่าง
ลงวันที่ย้อนหลัง ถ้าใช้ 7 วันจะเห็นแค่ประชุม

### ตัวเลขที่อ้างในเด็ค ตรวจได้ด้วยสองคำสั่ง

| คำสั่ง | ได้ (วัด 20 ส.ค. 2569) |
|---|---|
| `cd pipeline && python3 -m pytest` | **178 passed** |
| `cd slack-bot && npm test` | **120 pass · 0 fail** |
| `cd slack-bot && npm run typecheck` | ไม่มี error |

ทั้งสามคำสั่งรันได้โดยไม่ต้องมี Slack token · ฝั่งบอทเชื่อมกับ pipeline จริงได้ด้วย
`TAM_API_URL=http://127.0.0.1:8899 npm run check-api` ซึ่งเดินเส้นทาง boot ทั้งเส้น
โดยไม่มี Slack อยู่ในลูป

---

## สิ่งที่ **ไม่** อยู่ในรีโป และเหตุผล

- **ข้อมูล Slack จริงของลูกค้า** — ไฟล์ export จริงไม่ถูก commit และไม่ถูก track
  (`git ls-files` ยืนยันได้) ทุกภาพหน้าจอและทุกตัวเลขที่เปิดเผยมาจากข้อมูลสังเคราะห์
  หรือข้อมูลที่ลบตัวระบุตัวตนแล้ว
- **token / .env ที่กรอกจริง** — มีแต่ `.env.example` สองไฟล์
- **รูปถ่ายส่วนตัวในเด็ค** — `docs/img/` ถูก `.gitignore` ทั้งโฟลเดอร์
  เด็คจะถอด `<img>` ที่ไม่มีไฟล์ออกเอง เปิดได้ปกติ ไม่มีไอคอนรูปแตก

---

## ข้อจำกัดที่รู้ตัว

พูดไว้ตรงนี้ดีกว่าให้คนตรวจไปเจอเอง

- **กราฟความสัมพันธ์ยังว่างบนข้อมูลตัวอย่าง** — หน้ารายละเอียดงานมีกล่อง
  "เกิดอะไรขึ้นบ้าง ตามลำดับ" ที่ยังขึ้น `เหตุการณ์ 0` เพราะตัวจับคู่ยังหาคู่ถาม-ตอบ
  จากไฟล์สังเคราะห์ไม่ได้ ไทม์ไลน์รวมสองแหล่ง (Slack + ประชุม) ทำงานปกติ —
  ที่ยังไม่ทำงานคือชั้นความสัมพันธ์ระหว่างข้อความ
- **การเดโมฝั่ง Slack ต้องมี token** — ภาพนิ่งพิสูจน์ไม่ได้ว่าบอทกำลังทำงาน
  ต้องเดโมสด (`/mt demo`) ตาม [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md)
- **YouTrack / Notion ยังต่อไม่ครบตามข้อเสนอ** — ledger ปัจจุบันรวม Slack + ประชุม
  เป็นหลัก ส่วนที่เหลืออยู่ในหัวข้อก้าวถัดไปของเด็ค

---

## ข้อความสำหรับส่ง (คัดลอกไปวางได้)

> สวัสดีครับ ส่งงาน **ตาม (Tam)** — บอทที่อ่าน Slack กับโน้ตประชุม รวมเป็นบันทึกเดียว
> ต่อหนึ่งงาน แล้วบอกว่าอะไรติด ติดเพราะอะไร และข้อความไหนเป็นหลักฐาน
>
> รีโป (public): https://github.com/SirawithPra/scraping-slack-data
>
> ของสี่อย่างที่ขอ อยู่ในนั้นครบ:
> · **Working Prototype** — รันได้โดยไม่ต้องมี Slack token คำสั่งอยู่ใน README
> · **User Manual** — `docs/USER_MANUAL.md` (ไทย)
> · **Draft Presentation** — `docs/pdf/deck.pdf` (10 สไลด์)
> · **Source Code** — Python + TypeScript ในรีโป
>
> ถ้ามีเวลาจำกัด แนะนำสามอย่าง: ภาพหน้าจอในหัวข้อ *What it looks like* ของ README
> (เห็นของจริงโดยไม่ต้องติดตั้ง) · `docs/pdf/deck.pdf` · และ `docs/EXPERIMENTS.md`
> ที่บันทึกว่าทำไมเลือก `bge-m3` และทำไม fine-tune ที่เทรนเองสองตัวถึงไม่ถูกใช้
>
> จุดที่อยากให้ดูเป็นพิเศษคือ **สถานะทุกอันคำนวณด้วยกฎ ไม่ใช่ด้วยโมเดลภาษา** และ
> **ทุกคำกล่าวอ้างกดไปหาข้อความที่พูดไว้จริงได้** ข้อจำกัดที่รู้ตัวเขียนไว้ใน
> `SUBMISSION.md` หัวข้อสุดท้ายแล้วครับ
>
> ขอบคุณครับ
</content>
