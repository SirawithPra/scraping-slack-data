# คู่มือการใช้งาน — ตาม (Tam)

**ระบบอ่าน Slack + บันทึกการประชุม แล้วบอกว่าอะไรติด ติดเพราะอะไร และหลักฐานอยู่ข้อความไหน**

เอกสารนี้ครอบคลุมการติดตั้งและการใช้งานจริงทั้งสามผิว คำสั่งทุกบรรทัดในเอกสารนี้
ทดสอบแล้วบนเครื่อง macOS · Python 3.10.17 · Node v24.3.0

Repository: <https://github.com/SirawithPra/scraping-slack-data>

---

## 1. ระบบนี้ทำอะไร

ปัญหาเดิม: สถานะจริงของงานกระจายอยู่ใน Slack, ที่ประชุม, และหัวคน ไม่มีที่ไหนครบ
เข้า standup ทุกเช้าแล้วต้องนึกเอง

ระบบนี้รวมข้อความ Slack กับบันทึกการประชุมเป็น **"งาน" หนึ่งชิ้น** (work item)
แล้วคำนวณว่างานนั้นติดหรือเดินอยู่ โดย **ทุกคำกล่าวอ้างต้องชี้กลับไปที่ข้อความจริงได้**

หลักการที่ไม่ยอมลดหย่อน: **สถานะงานคำนวณด้วยกฎ ไม่ได้ให้ AI เดา** โมเดลภาษาเขียนได้
แค่ประโยคสรุป และประโยคนั้นต้องอ้างอิง message id ที่ตรวจสอบได้ ถ้า id ไม่มีอยู่จริง
ระบบตัดออกและติดป้าย `unverified` ให้เห็น

### สามผิว

| ผิว | ใช้ตอนไหน | เทคโนโลยี |
|---|---|---|
| **CLI** | พัฒนาและวัดผลตัวระบบเอง | Python |
| **Dashboard** | อ่านว่าวันนี้ทีมติดอะไร ก่อนเข้า standup | Python · FastAPI |
| **Slack bot** | ในที่ที่งานเกิดขึ้นจริง | TypeScript · Bolt |

---

## 2. สิ่งที่ต้องมีก่อน

| | ขั้นต่ำ | หมายเหตุ |
|---|---|---|
| macOS หรือ Linux | — | คำสั่งในคู่มือใช้ `python3` ไม่ใช่ `python` |
| Python | 3.10+ | ทดสอบบน 3.10.17 |
| Node.js | 20+ | ทดสอบบน v24.3.0 — ต้องมีเฉพาะเมื่อจะใช้ Slack bot |
| พื้นที่ดิสก์ | ~3.5 GB | venv ~1.3 GB + โมเดล embedding default `BAAI/bge-m3` **2.2 GB** โหลดครั้งแรกครั้งเดียวลง `~/.cache/huggingface` · **+2.1 GB** เมื่อใช้ preset ที่มี rerank (`hybrid-rerank`, `full`) เพราะต้องโหลด cross-encoder `BAAI/bge-reranker-v2-m3` เพิ่ม → รวม ~5.6 GB |
| Slack token | ไม่จำเป็น | **ลองได้โดยไม่ต้องมี** — ดูข้อ 4 |

ตัวเลขข้างบนวัดจาก cache จริงด้วย `du -shL ~/.cache/huggingface/hub/models--*/snapshots/*/`
ไม่ได้ประมาณเอา — วัดที่ระดับ **snapshot** เพราะ `du -sh` ที่ระดับโฟลเดอร์โมเดลจะรวมทุก
revision ที่เคยดึงมา (บนเครื่องที่พัฒนาอันนี้ `models--BAAI--bge-m3` ขึ้น 4.3 GB เพราะมีสอง
revision) ตัวที่ต้องใช้จริงคือ snapshot ที่ `refs/main` ชี้ = 2.2 GB

> **โมเดล default เปลี่ยนแล้ว** เวอร์ชันก่อนใช้ `paraphrase-multilingual-MiniLM-L12-v2`
> (458 MB) ตัวใหม่ใหญ่กว่าราว 5 เท่า แลกกับ context 8192 token แทน 128 และ
> triplet accuracy ที่สูงกว่า — เหตุผลและตัวเลขทั้งหมดอยู่ใน [EXPERIMENTS.md](EXPERIMENTS.md)
> ถ้าเคยรันเวอร์ชันเก่า ตัว MiniLM ยังค้างใน cache อยู่ ลบทิ้งได้ถ้าไม่ได้ตั้ง
> `EMBEDDING_MODEL` ชี้กลับไปหามัน

> ระบบไม่ส่งข้อมูลออกนอกเครื่อง โมเดลทุกตัวรันในเครื่อง ยกเว้นกรณีเดียวคือคุณตั้ง
> `SUMMARIZER=claude` เอง ซึ่งจะส่งข้อความไป Anthropic API

---

## 3. ติดตั้ง

### 3.1 โคลนและเตรียม Python

```bash
git clone https://github.com/SirawithPra/scraping-slack-data.git
cd scraping-slack-data

python3 -m venv .venv
source .venv/bin/activate

cd pipeline
python3 -m pip install -r requirements.txt
```

**คำสั่ง Python ทุกคำสั่งในคู่มือนี้รันจากโฟลเดอร์ `pipeline/`** และคำสั่ง `npm` ทุกคำสั่ง
รันจาก `slack-bot/` — สองฝั่งเข้าแบบเดียวกัน

ใช้เวลา ~2 นาที ติดตั้งแล้วตรวจว่าทุกโมดูลเรียกได้:

```bash
python3 -m tam.retrieval.retrieve --help
python3 -m pytest                    # ชุดทดสอบฝั่ง Python รันจาก pipeline/
```

ถ้าขึ้น help และ pytest ผ่านหมด (**98 passed**) ถือว่าฝั่ง Python พร้อม

### 3.2 ตั้งค่า

```bash
cp .env.example .env          # อยู่ใน pipeline/
```

เปิด `pipeline/.env` แก้ค่า ทุกตัวมีคำอธิบายกำกับอยู่ในไฟล์ ที่สำคัญคือ:

| ตัวแปร | ต้องใส่ไหม | คืออะไร |
|---|---|---|
| `SLACK_TOKEN` | เฉพาะเมื่อจะดึงข้อมูลจริง | Bot token `xoxb-...` หรือ user token `xoxp-...` |
| `SLACK_CHANNEL_ID` | เฉพาะเมื่อจะดึงข้อมูลจริง | รหัสช่อง `C...` ไม่ใช่ชื่อช่อง |
| `EMBEDDING_MODEL` | ไม่ | `.env.example` ปล่อยว่างไว้ตั้งใจ ให้ใช้ default ในโค้ดคือ `BAAI/bge-m3` · ตัวเลือกอื่นและผลวัดเทียบกันอยู่ใน [EXPERIMENTS.md](EXPERIMENTS.md) · แต่ละโมเดลมี cache แยกไฟล์ สลับได้ไม่พัง |
| `SUMMARIZER` | ไม่ | `template` (default, ออฟไลน์) หรือ `claude` |
| `TAM_NAMES` | ไม่ | ว่างไว้ = ใช้ชื่อจริงถ้ามี cache · `pseudonym` เวลาเดโม/แคปหน้าจอ · ดึงชื่อครั้งเดียวด้วย `python3 -m tam.ingest.users --fetch` |
| `HF_HUB_OFFLINE` | **ไม่ — และอย่าเปิดตอนรันครั้งแรก** | `.env.example` ปิดไว้ตั้งใจ เพราะครั้งแรกยังไม่มีโมเดลใน cache ถ้าเปิดจะดาวน์โหลดไม่ได้และขึ้น `We couldn't connect` ทั้งที่เน็ตปกติ · **เปิดหลังรันผ่านรอบแรกแล้ว** จะเร็วขึ้นทุกรอบ |
| `TAM_OVERRIDES_PATH` | ไม่ | ไฟล์ที่บอทเขียนตอนคนแก้การผูก ticket และ linker อ่านเป็นชั้นสูงสุด · สองฝั่ง default ตรงกันอยู่แล้ว |
| `YOUTRACK_URL` / `YOUTRACK_TOKEN` | เฉพาะถ้าจะใช้ drift detection | **อ่านอย่างเดียวพอ** สร้าง service account แยกแล้วให้แค่ `Read Issue` — ไม่มีอะไรในระบบเขียนกลับเข้า YouTrack |

> **`pipeline/.env` ถูก gitignore ไว้แล้ว** อย่า commit และอย่าส่ง token ทางแชทหรืออีเมล
> ถ้าเคยส่งไปแล้ว ให้ revoke แล้วออกใหม่ที่ <https://api.slack.com/apps>

---

## 4. ลองใช้โดยไม่มี Slack token

รีโปมีข้อมูลตัวอย่างไทย/อังกฤษ commit ไว้ให้ ทำงานได้ครบทุกขั้นโดยไม่ต้องต่อ Slack เลย
เหมาะกับการตรวจว่าระบบทำงานก่อนไปขอ token

```bash
# 1. เตรียมข้อมูลตัวอย่าง — เก็บ 18 จาก 23 ข้อความ ได้ 22 record
python3 -m tam.ingest.prepare_messages \
        --raw data/sample/slack_messages.sample.json \
        --out data/processed/sample_messages.json

# 2. ค้นหา
python3 -m tam.core --records data/processed/sample_messages.json \
        -q "FE sorting เสร็จแล้วแต่ยังรอ BE API"

# 3. สร้าง corpus ที่มีทั้ง Slack และที่ประชุม
#    --merge-into รวมเข้า "ไฟล์ที่มีอยู่แล้ว" ดังนั้นต้องเตรียมฝั่ง Slack ลงไฟล์นั้นก่อน
#    ไม่ทำขั้นแรกนี้ ไฟล์จะมีแต่บทประชุมล้วน
python3 -m tam.ingest.prepare_messages \
        --raw data/sample/slack_messages.sample.json \
        --out data/processed/sample_combined.json
python3 -m tam.ingest.meetings --transcript data/sample/standup.vtt \
        --title "Daily standup" --started 2026-08-14T09:30 \
        --merge-into data/processed/sample_combined.json

# 4. เปิด dashboard
python3 -m tam.web.server --records data/processed/sample_combined.json \
        --days 3650 --port 8899
```

ก่อนเสิร์ฟ มันจะพิมพ์ `Ready: 29 record(s), 4 topic(s), 2 blocked` แล้วเปิด
<http://localhost:8899> ได้เลย — หน้า digest มีงาน 4 ชิ้น ติดอยู่ 2 ชิ้น และมีชิ้นหนึ่ง
ที่มีข้อความทั้งจาก Slack และจากที่ประชุมอยู่ในงานเดียวกัน (ดูคอลัมน์ที่มา) กดเข้าไปดู
timeline ได้

> **`--days 3650` ไม่ได้พิมพ์ผิด** หน้าต่าง digest ค่า default คือ 7 วัน แต่ไฟล์ตัวอย่าง
> ฝั่ง Slack ลงวันที่ 2025-08-01 ถ้าใช้ 7 วันจะเห็นแต่บทประชุม หน้า digest/blockers
> เลยว่างเกือบหมด ตัวอย่างใช้หน้าต่างกว้าง ๆ ข้อมูลจริงใช้ `--days 7`
> (ระบบยังพิมพ์อายุจริงของแต่ละงานให้เห็นอยู่ ไม่ได้ทำให้ดูสดกว่าความจริง)

---

## 5. ใช้กับข้อมูลจริงจาก Slack

### 5.1 สร้าง Slack app สำหรับดึงข้อมูล

ไปที่ <https://api.slack.com/apps> → **Create New App** → **From a manifest** →
เลือก workspace → วางเนื้อหาไฟล์ `pipeline/slack-app-manifest.json` → **Create** →
**Install to Workspace** → คัดลอก **Bot User OAuth Token** (`xoxb-...`)

manifest นี้ขอ scope แค่ที่จำเป็นต่อการอ่านประวัติ และตั้ง `token_rotation_enabled: false`
ไว้ ทำให้ token ไม่หมดอายุทุก 12 ชั่วโมง

**scope ที่ต้องมี**

| ต้องการอ่าน | scope |
|---|---|
| ช่องสาธารณะ | `channels:history` |
| ช่องส่วนตัว | `groups:history` |
| DM | `im:history` / `mpim:history` |

**สำคัญ: ต้องเชิญบอทเข้าช่องก่อน** ไม่ว่า scope จะครบแค่ไหน bot token อ่านช่องที่ตัวเอง
ไม่ได้อยู่ไม่ได้

```
/invite @ชื่อแอปของคุณ
```

หา `SLACK_CHANNEL_ID` จาก: ชื่อช่อง → About → เลื่อนลงล่างสุด → Channel ID

> **token แต่ละแบบใช้ไม่เหมือนกัน** `xoxb-` และ `xoxp-` ใช้ได้ · `xoxe-` (refresh token)
> และ `xapp-` (app-level token) **ใช้ไม่ได้** โปรแกรมปฏิเสธสองตัวหลังทันทีและเรียก
> `auth.test` ก่อนเริ่มดึง เพื่อให้ token ผิดพังใน 1 request ไม่ใช่พังกลางทาง

### 5.2 ดึงและเตรียมข้อมูล

```bash
python3 -m tam.ingest.export_slack        # → data/raw/slack_messages.json
python3 -m tam.ingest.prepare_messages    # → data/processed/messages.json
```

> **ดึงรอบต่อ ๆ ไป ถ้าเคย merge ประชุมเข้าไปแล้ว** ให้ใช้
> `python3 -m tam.ingest.prepare_messages --merge-into data/processed/messages.json`
> เพราะ `--out` เฉย ๆ จะทับไฟล์ทิ้ง — โปรแกรมตรวจเจอว่ามี record ประชุมอยู่แล้วก็จะ
> ปฏิเสธและบอกให้ใช้ `--merge-into` แทน (ถ้าจะทับจริง ๆ ต้องใส่ `--force` เอง)

> **ถ้าดึงช้ามาก ไม่ใช่บั๊ก** แอปที่สร้างหลัง 29 พ.ค. 2025 และไม่ได้อยู่บน Marketplace
> ถูกจำกัด `conversations.history` เหลือราว 1 request/นาที (~15 ข้อความ/request)
> ดึง 200 ข้อความอาจใช้หลายนาที โปรแกรมรอตาม `Retry-After` แล้วไปต่อเอง ปล่อยไว้ได้

### 5.3 เปิด dashboard

```bash
python3 -m tam.web.server --records data/processed/messages.json --port 8899
```

---

## 6. Dashboard

```bash
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899
```

ก่อนเปิดให้บริการ มันพิมพ์บอกว่าโหลดอะไรได้ — เป็นวิธีเช็คเร็วที่สุดว่าข้อมูลเข้าจริง
(ตัวเลขข้างล่างคือข้อมูลตัวอย่างจากข้อ 4):

```text
INFO Building index from data/processed/sample_combined.json
INFO Reused 27 cached embedding(s) from data/processed/embeddings_….npz
INFO Ready: 29 record(s), 4 topic(s), 2 blocked, summariser template
```

บรรทัดถัดจากนั้นมันพิมพ์ URL ทุกหน้า และ **token สำหรับ route ที่เขียนข้อมูล**
(`X-TAM-Token`) ตั้ง `TAM_ADMIN_TOKEN` ไว้ถ้าอยากให้ token เดิมอยู่ข้าม restart

### หน้าจอ

| URL | เห็นอะไร |
|---|---|
| `/` | Digest — งานที่ขยับ เรียงใหม่สุดก่อน |
| `/blockers` | เฉพาะที่ติด พร้อมข้อความที่เป็นหลักฐาน |
| `/item/{key}` | งานหนึ่งชิ้น — timeline ข้าม Slack และที่ประชุม · `{key}` ใช้ `item_id` ที่คงที่ (ticket key หรือ `c30a929`) ส่วนเลข cluster ยังเปิดได้แต่ rebuild แล้วเปลี่ยนความหมาย |
| `/people` | ใครอยู่กับเรื่องไหน — 23 คน พร้อมจำนวนงานที่เกี่ยวข้อง งานที่ติด และข้อความที่พูด · กดชื่อคนเพื่อเปิดหน้าของคนนั้น |
| `/person/{user}` | คนหนึ่งคน — งานทั้งหมดที่เขาอยู่ด้วย (กรอง/เรียงได้) · 40 ข้อความล่าสุดของเขาแยกตามวันพร้อมงานที่แต่ละข้อความสังกัด · คนที่อยู่ในงานเดียวกันบ่อย · `{user}` ใช้ Slack id หรือชื่อที่แสดงก็ได้ เพราะชื่อผู้พูดในบทประชุมคือ id ของเขาเอง |
| `/tracker` | เทียบกับทิกเก็ต — การ์ด drift แต่ละใบโชว์**สองประโยค** (ประโยคที่ตัดสินสถานะ + ประโยคที่พิมพ์เลขทิกเก็ต) และเตือนเมื่อไม่ใช่ข้อความเดียวกัน · 4 หัวข้อตามการ์ดตัวเลขด้านบน: ที่ขัดกัน (drift) · ที่เปิดค้างแต่ไม่มีใครแตะ (silent) · ทิกเก็ตที่เปิดอยู่ทั้งหมดพร้อมเวลาที่ถูกแตะครั้งสุดท้าย · เรื่องที่จับคู่ทิกเก็ตได้ พร้อมผลเทียบสามค่า (ขัดกัน / ตรงกัน / **เทียบไม่ได้** เมื่อไม่มีใครพิมพ์เลขทิกเก็ตในแชท) |
| `/search` | วางประโยคที่สงสัย ระบบหาข้อความต้นทางให้ |
| `/upload` | **วางโน้ตที่จดเอง** (ที่ใช้จริงบ่อยสุด — ทีมส่วนใหญ่ไม่มีไฟล์ถอดเสียง) หรืออัปโหลด `.vtt` / `.srt` · วางหนึ่งครั้ง = หนึ่ง record · วางซ้ำในวันเดียวกันแทนที่ของเดิม · บรรทัดแบบ `• Pending …` จะขึ้นใน `/blockers` โดยอ้างบรรทัดนั้นเป็นหลักฐาน · CLI: `python3 -m tam.ingest.notes` |

### API

ข้อมูลชุดเดียวกันออกเป็น JSON ได้ ไม่ต้อง scrape HTML:

```bash
curl localhost:8899/api/digest
curl localhost:8899/api/blockers
curl localhost:8899/api/item/c30a929       # {key} คือ item_id ที่ /api/digest ส่งมา — คงที่ข้าม rebuild
curl localhost:8899/api/item/1             # เลข cluster ก็ยังใช้ได้ แต่ rebuild แล้วมันจะชี้งานคนละชิ้น
curl "localhost:8899/api/search?q=Android&k=10"
curl localhost:8899/api/people             # ใครอยู่กับเรื่องไหน · งานที่ติดของแต่ละคน
curl localhost:8899/api/person/U08H0UD5R36 # คนหนึ่งคน — งาน ข้อความล่าสุด และคนที่อยู่ในงานเดียวกัน (ใช้ชื่อที่แสดงก็ได้)
curl localhost:8899/api/tracker            # เทียบกับ ticket system — ดู §7.8
curl localhost:8899/api/health

# route เดียวที่เขียนข้อมูล ต้องแนบ token ที่ server พิมพ์ตอน start
curl -X POST -H "X-TAM-Token: $TAM_ADMIN_TOKEN" localhost:8899/api/reindex
```

> **เปิดครั้งแรกช้า** เพราะต้อง embed ทั้ง corpus ครั้งถัดไปมันใช้ cache ใน
> `pipeline/data/processed/embeddings_*.npz` ถ้าโมเดลและข้อความเดิม จะเหลือไม่กี่วินาที

---

## 7. Slack bot (Meowtam)

### 7.1 สร้างแอป

<https://api.slack.com/apps> → **Create New App** → **From an app manifest** →
วางเนื้อหา `slack-bot/slack-app-manifest.yaml`

manifest ตั้ง scope, slash command และ shortcut ให้ครบในครั้งเดียว
**อย่าตั้งเองทีละอัน** — scope ที่ขาดไปจะโผล่เป็น error ตอน runtime ซึ่งหายากกว่ามาก

จากนั้นเก็บสามค่า:

| ค่า | หาจาก |
|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-`) | Install App → Install to Workspace |
| `SLACK_APP_TOKEN` (`xapp-`) | Basic Information → App-Level Tokens → Generate, scope `connections:write` |
| `SLACK_SIGNING_SECRET` | Basic Information → App Credentials |

### 7.2 ติดตั้งและรัน

```bash
cd slack-bot
cp .env.example .env      # ใส่สามค่าข้างบน + DIGEST_CHANNEL
npm install
npm start
```

ต้อง `/invite @Meowtam` เข้าช่องที่ตั้งใน `DIGEST_CHANNEL` ด้วย

ใช้ **Socket Mode** หมายความว่า **ไม่ต้องมี public URL ไม่ต้องใช้ ngrok ไม่ต้องเปิด
firewall** บอทต่อออกไปหา Slack เอง รันจากโน้ตบุ๊กได้เลย

### 7.3 คำสั่งใน Slack

| พิมพ์ | ได้อะไร |
|---|---|
| `/meowtam` หรือ `/mt` | บอร์ดงานทั้งหมด เรียงงานที่ติดขึ้นก่อน |
| `/meowtam blocked` | เฉพาะที่ติด |
| `/meowtam digest` | digest สำหรับ standup |
| `/meowtam recall <ข้อความ>` | ค้นหา (ไทยได้) พร้อมสายการตัดสินใจของเรื่องนั้น |
| `/meowtam MOB-142` | งานชิ้นเดียวตาม key — `MOB-142` มีอยู่ใน ledger ตัวอย่าง ถ้าตั้ง `TAM_API_URL` key จะเป็น `TAM-0`…`TAM-4` |
| `/meowtam @someone` | คนนั้นกำลังทำอะไร |
| `/meowtam silent` (`quiet`) | ticket ที่เปิดค้างและไม่มีใครแตะเกิน `TAM_SILENT_DAYS` วัน — ดู §7.8 |
| `/meowtam drift` | ที่ Slack กับ ticket ไม่ตรงกัน — ดู §7.8 |
| `/meowtam format` (`help`) | สอนรูปแบบที่ระบบอ่านได้ ส่งให้ทีมดูได้เลย |
| `/meowtam reload` | อ่าน ledger ใหม่ ไม่ต้อง restart |

**Message shortcut** (คลิกขวาที่ข้อความ → More actions)

| shortcut | ทำอะไร |
|---|---|
| ผูกกับ ticket | เอาข้อความนั้นผูกเข้ากับ work item |
| บันทึกเป็นการตัดสินใจ | เก็บเข้า decision log หาเจอด้วย `recall` ได้ตลอด |

**อัตโนมัติ** — DM สรุป 08:45 และ digest เข้าช่อง 09:25
**ปิดไว้เป็นค่า default** เปิดด้วย `ENABLE_SCHEDULE=1` เท่านั้น
(กันไว้ไม่ให้ `npm start` เผลอโพสต์ลงช่องจริง)

### 7.4 ใช้กับข้อมูลช่องจริง

```bash
# ตั้ง EXPORT_CHANNELS=C...,C... ใน .env ก่อน แล้ว:
npm run export     # ดึงประวัติด้วย bot token ที่มีอยู่
npm run ledger     # จัดเป็น work item + สถานะ + หลักฐาน → data/ledger.json
```

`npm run ledger` จะรายงาน unassigned rate ตอนจบ **ต่ำกว่า 25% ถือว่าใช้ได้**
ถ้าสูงกว่านั้น ลดค่า `SIM_FLOOR` ใน `scripts/build-ledger.ts` แล้วรันใหม่

### 7.5 ตรวจว่าบอททำงานโดยไม่ต้องมี token

```bash
npm run typecheck              # ตรวจ type ทั้งโปรเจกต์
npm test                       # ชุดทดสอบฝั่งบอท — 53 ผ่าน (รวม tests/gate.test.ts)
npm run preview                # เรนเดอร์ทุกหน้าจอออฟไลน์ + ตรวจ Slack limits
npm run preview -- digest      # ดัมพ์ payload เดียวไปวางใน Block Kit Builder
```

`npm run preview` เรนเดอร์ 8 หน้าจอ (digest, standup, item, board, drift,
driftModal, recall, recallEmpty) บวกเคสที่ยาวสุดของสี่หน้าที่มีลิสต์ (board, standup,
item, digest) แล้วตรวจว่า
**ทุก payload ผ่าน Slack limits** โดยไม่ต่อเน็ต ถ้าหน้าไหนเกิน limit มันจะบอกชื่อหน้านั้น

---

## 7.6 ให้บอทอ่านจาก pipeline (แนะนำ)

ค่าเริ่มต้นบอทใช้ ledger ของตัวเอง ถ้าอยากให้ใช้ผลจาก pipeline — ซึ่งเป็นตัวที่ใช้โมเดล
embedding จริง — ตั้ง `TAM_API_URL` ใน `slack-bot/.env`

```bash
# เทอร์มินัลที่ 1
cd pipeline
python3 -m tam.web.server --records data/processed/sample_combined.json --days 3650 --port 8899

# เทอร์มินัลที่ 2
cd slack-bot
TAM_API_URL=http://127.0.0.1:8899 npm run check-api   # พิสูจน์ว่าต่อได้ ไม่ต้องมี Slack
TAM_API_URL=http://127.0.0.1:8899 npm start
```

`check-api` ไล่ทั้งเส้นทางที่บอทใช้ตอน boot แล้วพิมพ์ผลออกมา ใช้ตรวจก่อนเดโมได้ —
work item ทุกชิ้น, หลักฐานและ citation ชี้ข้อความที่มีจริง, permalink สร้างคืนได้กี่ข้อความ

### ขั้นสุดท้ายของ `check-api`: calibration ของ relevance gate

gate คือตัวตัดสินว่า recall จะตอบอะไรออกมาไหม **กลไกเปลี่ยนแล้ว ไม่ใช่แค่เปลี่ยนเลข**
เดิมดู cosine ตัวเดียว ตอนนี้ `/api/search` ส่งค่า **absolute** สองตัวมาในฟิลด์ `relevance`
และต้องผ่าน **ทั้งสองตัว**:

| สัญญาณ | เกณฑ์ | จับอะไร |
|---|---|---|
| `lexical` — BM25 ดิบของ record ที่ตรงที่สุด | `> 0` | ข้อความขยะไม่มีคำร่วมกับ corpus เลย BM25 จึงได้ **0.00 สนิท** |
| `dense` — cosine ดิบของ record ที่ใกล้ที่สุด | `>= TAM_MIN_COSINE` | query จริงที่ใช้คำต่างจาก corpus ซึ่ง BM25 เดี่ยวจะทิ้ง |

**ทำไม cosine เดี่ยวใช้ไม่ได้** เพราะ `max cosine` เหนือ N เอกสารสูงขึ้นตาม N สำหรับ query
อะไรก็ตาม — พอมี record หลายร้อยขึ้นไป ยังไงก็มีอันที่ "ดูคล้าย" ปัญหาอยู่ที่กลไก ไม่ใช่โมเดลไม่ดี

บล็อกข้างล่างคือ output จริงจากการรัน `TAM_API_URL=http://127.0.0.1:8899 npm run check-api`
กับ export จริง 936 record ที่เสิร์ฟด้วย `BAAI/bge-m3`:

```text
→ calibration — วัดว่า gate แยก query ขยะออกจาก query จริงได้จริงไหม
  bm25   0.00 · cos 0.597  · ถูกกรอง  “qqqzzzxxx wvwvwv jjjkkk zzzqqq”
  bm25   0.00 · cos 0.457  · ถูกกรอง  “zxqv frobnicate wibble plumbus grommet”
  bm25   0.00 · cos 0.578  · ถูกกรอง  “ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ”
  bm25  11.06 · cos 0.731  ✕ ผ่าน gate  “ๆๆๆ ฯฯฯ ฤฤฤ ฅฅฅ”
  bm25   9.29 · cos 0.681  · ผ่าน  (จริง) “Profile module bug บน Android”
  …อีก 5 บรรทัดเป็นหัวข้องานจริงที่ดึงจาก corpus จึงไม่ลงในเอกสารนี้ — ทั้งหมด “ผ่าน”
  กฎ: bm25 > 0 และ cos >= 0.45 · ขยะที่หลุด 1/4 · query จริงที่เสีย 0/6
⚠ gate กรองขยะได้ 3/4 — ยังมี 1 ตัวหลุด แต่ไม่เสีย query จริงเลย
  ตัวที่หลุดมักเป็นอักขระไทยซ้ำ ๆ (ๆ ฯ) ซึ่งมีอยู่ใน corpus จริง จึงมีคำตรงกันจริง
  นั่นคือขอบเขตของกลไก ไม่ใช่การตั้งค่าผิด — ดู docs/EXPERIMENTS.md
  (ข้อนี้ไม่ทำให้ exit code เป็น 1 — ใส่ --strict-gate ถ้าต้องการให้ fail)
```

อ่านบล็อกนี้อย่างนี้:

- **cosine เดี่ยวจะปล่อยผ่านทั้ง 4 ตัว** — probe ขยะได้ cosine 0.457 ถึง 0.731 เกินเกณฑ์
  0.45 ทุกตัว แต่ 3 ตัวได้ BM25 `0.00` เงื่อนไขคู่จึงกรองออกได้
- **มันบอกว่า 3/4 ไม่ใช่ 4/4 และนั่นคือความจริง** `ๆ` กับ `ฯ` เป็นอักขระไทยที่มีอยู่ใน
  ข้อความปกติ probe นั้นจึงมีคำตรงกัน**จริง** — เป็น**ขอบเขตของกลไก ไม่ใช่ค่าที่ตั้งผิด**
  probe ตัวนี้ถูกเก็บไว้ในลิสต์**โดยตั้งใจ** ถ้าตัดออกรายงานจะขึ้นว่าผ่านสวยงามทั้งที่รูยังอยู่
- **query จริงไม่เสียเลย (0/6)** ถ้ามีตัวเลขนี้ขึ้นแทน แปลว่าอาการหนักกว่าปล่อยขยะผ่าน
  เพราะเป็นความเสียหายที่คนใช้เห็นเอง
- **ห้ามดัน `TAM_MIN_COSINE` ขึ้นเพื่อไล่ขยะ** ตัวที่หลุดหลุดเพราะ BM25 ไม่ใช่เพราะ cosine
  ดัน cosine ขึ้นจะเริ่มตัด query จริงทิ้งก่อนที่จะกันตัวนั้นได้

**exit code ตอบเฉพาะ "การต่อสองฝั่งสมบูรณ์ไหม"** — pipeline ตอบไหม, item ชี้หลักฐานได้ไหม,
recall มาจาก embedding ไหม ส่วน calibration พิมพ์ให้เห็นทุกครั้งแต่**ไม่ทำให้ fail**
(รันข้างบนได้ exit `0` พร้อมคำเตือน `⚠ 3/4`) ถ้าอยากให้ calibration ทำให้ fail ด้วย เช่นใน CI
บน corpus จริง ใส่ `--strict-gate` แล้วรันเดิมจะได้ exit `1`

```bash
# ต้องใส่ query ก่อน flag — argument ตัวแรกถูกอ่านเป็น query ของ recall
TAM_API_URL=http://127.0.0.1:8899 npm run check-api -- "Profile module bug บน Android" --strict-gate
```

> **ค่าเกณฑ์เป็นคุณสมบัติของ corpus คู่กับโมเดล ไม่ใช่ค่าคงที่** ตัวเลขในบล็อกข้างบนมาจาก
> corpus หนึ่งชุดกับโมเดลหนึ่งตัว เปลี่ยนอย่างใดอย่างหนึ่งแล้วต้องวัดใหม่ — `check-api`
> วัดซ้ำที่หน้างานทุกครั้งด้วยเหตุนี้ ไม่ต้องเชื่อเลขในคู่มือ · การเทียบ 5 โมเดล, cross-encoder
> reranker ที่ลองเอามาเป็น gate แล้ว**ไม่ผ่านการวัด**, และโมเดลที่ fine-tune เอง 2 ตัวที่
> **แพ้ของสำเร็จรูป** อยู่ใน [EXPERIMENTS.md](EXPERIMENTS.md) ครบ

> **pipeline เก่ากว่าบอทจะพังเสียงดัง** ถ้า `/api/search` ไม่ส่ง `relevance` มา บอท throw
> ทันทีแทนที่จะปล่อยทุก query ผ่าน — gate ที่แอบเลิกทำงานคือความล้มเหลวที่กลไกนี้มีไว้กัน

**ตั้งแล้วจะไม่มี fallback** ถ้า pipeline ตอบไม่ได้ บอทจะไม่สตาร์ตและบอกวิธีแก้ —
เพราะการแอบเสิร์ฟข้อมูลเก่าที่หน้าตาเหมือนของจริงอันตรายกว่าการไม่สตาร์ต

| มาจาก pipeline | บอทเติมเอง (ไม่มีตัวเทียบใน pipeline) |
|---|---|
| work item · สถานะ · หลักฐาน · timeline · ข้อความ · ประโยคสรุป · recall | decision — อ่านจากไฟล์ที่คนกดบันทึกไว้จริง (`data/decisions.json`) ว่างจนกว่าจะมีคนกด · standup draft — คำนวณจาก item ที่ได้มา · drift บนบอร์ด — มาจาก ledger จึงว่างอยู่ ตัวที่ใช้งานจริงคือ `/meowtam drift` ซึ่งอ่านจาก `/api/tracker` (§7.8) |

ชื่อสถานะสองฝั่งไม่เหมือนกัน pipeline ใช้ `active` / `blocked` / `resolved`
บอร์ดของบอทใช้ `blocked` / `stalled` / `moving` / `done` แปลงกันแบบนี้:
`blocked → blocked`, `resolved → done`, `active → moving` และ `active` ที่เงียบเกิน
`TAM_STALE_DAYS` → `stalled` มีแค่ `stalled` ที่เป็นข้อมูลใหม่ อีกสองอันเป็นการเปลี่ยนชื่อ

ดูภาพประกอบทั้งหมดได้ที่ [architecture.html](architecture.html)

---

### 7.8 ต่อ ticket system (YouTrack) — แหล่งที่สอง

Slack บอกว่า *คนคุยอะไรกัน* ticket บอกว่า *งานอยู่สถานะไหน* ทั้งสองอย่างไม่ตรงกันเสมอ
และ **ช่องว่างระหว่างสองอันนี้คือของที่มีค่าที่สุด** — งานที่ค้างอยู่โดยไม่มีใครพูดถึง

ใส่สามค่านี้ใน `pipeline/.env`:

```bash
YOUTRACK_URL=https://<your-org>.youtrack.cloud   # ไม่ต้องมี /api ต่อท้าย
YOUTRACK_TOKEN=perm-...                          # Profile → Account Security → Authentication → New token
YOUTRACK_PROJECTS=PROJ,OTHER                     # ตัวย่อโปรเจกต์ คั่นด้วย comma — **ต้องตั้ง** ว่างไว้ = ไม่ดึงอะไรเลย
```

หา token ที่ **avatar มุมขวาบน → Profile → Account Security → Authentication → New token…**
ให้ scope `YouTrack` พอ

> **ใช้ service account ที่อ่านได้เท่านั้น** อย่าใช้ token ของ admin ตัวเอง
> ถ้า token รั่ว มันมีสิทธิ์เท่าที่คุณมี ตั้ง user ใหม่แล้วให้ role `Observer` ปลอดภัยกว่ามาก

ทดสอบว่าต่อได้:

```bash
cd pipeline
python3 -m tam.ingest.youtrack --check           # ยืนยัน token และบอกว่ามันเห็นอะไร
python3 -m tam.analysis.drift --records data/processed/real_all.json --json
```

**อ่านผลให้ตรง**

| ได้อะไร | หมายความว่า |
|---|---|
| `silent` | ticket เปิดค้างและไม่ถูกแตะเกิน `TAM_SILENT_DAYS` วัน (default `21`) — ครอบคลุม **ทุกใบที่เปิดค้าง** ไม่ต้องรอให้ Slack เอ่ยถึง |
| ticket ใน corpus | หลังตั้งค่านี้ `tam.ingest.daily` จะดึง ticket มา merge เข้า corpus ด้วย ทำให้ `/search` และ `/meowtam recall` เจอ ticket ได้เหมือนข้อความ Slack · ที่ embed คือ **ชื่อ ticket + 2 บรรทัดแรกของ description ที่มีเนื้อหา** ไม่ใช่ทั้งก้อน (ทั้งก้อน median 582 ตัวอักษรเทียบกับข้อความ Slack 46 — ของยาวจะกลืนการจับกลุ่ม) · ticket ที่ไม่มีใครคุยถึงจะ**ไม่**โผล่เป็นงานใน digest แต่ยังค้นเจอ เพราะงานหนึ่งชิ้นต้องมีคนคุย ticket เป็นแค่ส่วนขยาย |
| `drift` | ปิด ticket แล้วแต่ยังคุยกันต่อ / คุยว่าติดแต่ ticket ยังเปิด — ทำได้เฉพาะ work item ที่**มีเลข ticket อยู่ในข้อความ** จึงครอบคลุมน้อยกว่ามาก |
| `evidence` / `link_text` | **สองประโยคต่อหนึ่งใบ** — `evidence` คือประโยคที่ทำให้สถานะฝั่ง Slack เป็นอย่างนั้น · `link_text` คือประโยคที่มีคนพิมพ์เลข ticket ไว้ ทั้งสองมักไม่ใช่ข้อความเดียวกัน (วัดบนโปรเจกต์จริง: 0 จาก 2 ใบที่เป็นอันเดียวกัน) |
| `evidence_names_ticket` | `false` = ประโยคที่ตัดสินสถานะไม่ได้พูดถึง ticket ใบนั้นตรง ๆ มันมาจากบทสนทนาในเรื่องเดียวกัน — ใบนั้นยังควรดู แต่ต้องอ่านสองประโยคเทียบกันก่อนเชื่อ หน้า `/tracker` ติดป้ายเตือนให้เอง |
| `coverage` | บอกตรง ๆ ว่าเทียบได้กี่ item จากทั้งหมด — ดูตัวเลขนี้ก่อนเชื่อ `drift` |
| `error` ไม่ว่าง | **อ่าน ticket ไม่ได้** ลิสต์ที่ว่างจึงหมายถึง "ยังไม่รู้" ไม่ใช่ "ไม่มีงานค้าง" — หน้าจอกับการ์ดใน Slack เขียนแยกสองกรณีนี้ให้แล้ว |

`mentioned_in_slack` ติดมากับแต่ละใบ แต่ **ไม่ได้ใช้เป็นตัวกรอง** เพราะวัดแล้วว่าที่เกณฑ์
default มันไม่เปลี่ยนผลเลย (24 จาก 24 ใบไม่ถูกพูดถึง) ใส่เป็นตัวกรองจะทำให้ดูเหมือน
ต้องใช้สองแหล่งทั้งที่ไม่จำเป็น — และ "ไม่มีใครพูดถึง" จำกัดอยู่แค่**ช่วงเวลาที่ export มา**
ไม่ใช่ทั้งประวัติของ workspace

จะเปลี่ยนเกณฑ์ก็ตั้ง `TAM_SILENT_DAYS` (หรือ `--silent-days` ตอนเรียก CLI) **แต่อย่าเดาเลข** — ดูการกระจายของทีมตัวเองก่อน
(ของทีมนี้ p25 = 5 · median = 8 · p75 = 22 จึงเลือก 21 ซึ่งตกในช่องว่างที่ไม่มี ticket อยู่เลย
เกณฑ์จะได้ไม่ผ่ากลางกลุ่มหนาแน่นที่ขยับวันเดียวคำตอบเปลี่ยน)

---

## 8. คำสั่งที่ใช้บ่อย

คำสั่งข้างล่างใช้ `data/processed/sample_combined.json` จากข้อ 4 เปลี่ยนเป็นไฟล์ของคุณ
ได้ทุกอัน (แล้วเปลี่ยน `--days 3650` เป็น `--days 3` หรือ `7` ตามจริง)

```bash
# วางโน้ตที่จดเอง (อ่าน stdin หรือ --file) — ใส่ --json ถ้าอยากดูก่อนว่าจะได้ record หน้าตาไหน
pbpaste | python3 -m tam.ingest.notes --title "Sprint planning" --merge-into data/processed/real_all.json

# ค้นหาแบบอธิบายว่าทำไมได้ผลนี้
python3 -m tam.retrieval.retrieve --records data/processed/sample_combined.json \
        -q "BE sorting API พร้อมแล้วหรือยัง" --preset full --explain

# ดูว่ามี preset อะไรให้เลือก (แต่ละ preset อธิบายเฉพาะ stage ที่มันรัน)
python3 -m tam.retrieval.retrieve --list-presets

# งานที่ขยับ / งานที่ติด
python3 -m tam.analysis.digest --records data/processed/sample_combined.json --days 3650
python3 -m tam.analysis.digest --records data/processed/sample_combined.json --blockers

# สรุปเป็นภาษาคน (ออฟไลน์)
python3 -m tam.analysis.summarize --records data/processed/sample_combined.json --days 3650

# วัดผลว่า preset ไหนดีกว่า — ต้องมีไฟล์ label, ตัวอย่างที่ commit ไว้ตรงกับข้อมูลตัวอย่าง
python3 -m tam.evaluation.evaluate --records data/processed/sample_messages.json \
        --eval-file data/eval_queries.example.json --presets dense hybrid hybrid-rerank full

# รายงานกราฟ / รายงานภาษาไทย → output/report.html, output/report_th.html
python3 -m tam.report.visualize  --records data/processed/sample_messages.json \
        --eval-file data/eval_queries.example.json
python3 -m tam.report.report_th  --records data/processed/sample_messages.json \
        --eval-file data/eval_queries.example.json
```

โมดูลทั้งหมด 24 ไฟล์ · **21 ตัวเป็น CLI รับ `--help`** ส่วนที่เหลือสามตัว
(`retrieval/embeddings.py` `retrieval/fusion.py` `ingest/quoted.py`) เป็น library ที่โมดูลอื่นเรียกใช้

---

## 9. แก้ปัญหาที่เจอบ่อย

| อาการ | สาเหตุและวิธีแก้ |
|---|---|
| `SLACK_TOKEN missing` | ยังไม่ได้ `cp .env.example .env` ใน `pipeline/` หรือยังไม่ได้ใส่ค่า |
| `invalid_auth` / `not_authed` | token ผิดแบบ — ต้องเป็น `xoxb-` หรือ `xoxp-` ไม่ใช่ `xoxe-` / `xapp-` |
| `not_in_channel` | ยังไม่ได้ `/invite` บอทเข้าช่อง |
| `missing_scope` | scope ไม่ครบ — สร้างแอปใหม่จาก manifest จะได้ครบทันที |
| ดึงข้อมูลช้ามาก | rate limit ของแอปใหม่ ไม่ใช่บั๊ก ปล่อยให้รันต่อ |
| เปิด dashboard ครั้งแรกช้า | กำลัง embed corpus ครั้งถัดไปใช้ cache |
| `ModuleNotFoundError: tam` | ต้องรันจากโฟลเดอร์ `pipeline/` และ activate venv แล้ว |
| bot ไม่ตอบ slash command | `SLACK_APP_TOKEN` ต้องมี scope `connections:write` |
| ไม่มีอะไรใน digest | corpus ว่างหรือ `--days` แคบไป — ข้อมูลตัวอย่างฝั่ง Slack ลงวันที่ 2025-08-01 ต้องใช้ `--days 3650` (`--days 30` ก็ยังไม่เจอ) ข้อมูลจริงใช้ `--days 7` |
| `/blockers` ว่าง แต่ digest มีงาน | ไม่ใช่บั๊ก — ไม่มีงานไหนติดจริงใน corpus นั้น สถานะมาจาก typed relation ระบบไม่แต่งให้ ข้อมูลตัวอย่างฝั่ง Slack ล้วนไม่มี blocker เลย ต้องรวมบทประชุมเข้าไปตามข้อ 4 |
| `403` ตอนเรียก `POST /api/reindex` หรือ `/upload` | route ที่เขียนข้อมูลต้องแนบ token ที่ server พิมพ์ตอน start (`X-TAM-Token`) และ `Origin` ต้องเป็น host เดียวกัน |
| `check-api` ขึ้น `⚠ gate กรองขยะได้ 3/4` | **ไม่ใช่บั๊กและไม่ทำให้ exit 1** ตัวที่หลุดคือ `ๆๆๆ ฯฯฯ …` — `ๆ` `ฯ` เป็นอักขระไทยที่มีใน corpus จริง BM25 จึงเจอคำตรงกันจริง เป็นขอบเขตของกลไก ดูข้อ 7.6 · ถ้าอยากให้นับเป็น fail ใส่ `--strict-gate` (ต้องใส่ query ก่อน flag) |
| `check-api` ขึ้น `✕ gate ตัด query จริงทิ้ง N อัน` | อาการนี้**หนักกว่า**ปล่อยขยะผ่าน เพราะคนใช้เห็นเอง — ลด `TAM_MIN_COSINE` (default 0.45) หรือเช็คว่า corpus มีเรื่องนั้นอยู่จริงไหม · **อย่าดันขึ้น**เพื่อไล่ขยะ |
| `/api/search ไม่ได้ส่ง relevance มา — pipeline เวอร์ชันเก่า` | pipeline เก่ากว่าฝั่งบอท gate ทำงานไม่ได้ บอทจึงหยุดแทนที่จะปล่อยผ่าน — รัน `python3 -m tam.web.server` จาก repo ชุดเดียวกันกับบอท |
| `--host 0.0.0.0` แล้วไม่ยอมเริ่ม | ตั้งใจ — server bind แค่ loopback ถ้าจะให้เครื่องอื่นเข้าถึงต้องใส่ `--expose` ด้วย เพราะ `/upload` เขียน corpus ได้ และ `--expose` เองก็บังคับให้ตั้ง `TAM_ADMIN_TOKEN` ก่อน เพื่อให้ token ที่แจกเป็นตัวที่คุณเลือกเอง |

---

## 10. ข้อมูลเก็บที่ไหน และอะไรที่ไม่ขึ้น Git

| ที่ | อะไร | ขึ้น Git ไหม |
|---|---|---|
| `pipeline/data/raw/` | ข้อมูล export ดิบจาก Slack | **ไม่** |
| `pipeline/data/processed/` | records + embedding cache ที่สร้างขึ้น | **ไม่** |
| `pipeline/data/sample/` | ตัวอย่างไทย/อังกฤษ | ขึ้น (ตั้งใจ) |
| `slack-bot/data/ledger.fixture.json` | ledger ตัวอย่าง (ข้อมูลสังเคราะห์) | ขึ้น (ตั้งใจ) |
| `slack-bot/data/ledger.json` | ledger ที่ `npm run ledger` เขียนจาก export จริง — `npm run seed` คัดลอกจาก fixture ให้ถ้าไฟล์ยังไม่มี | **ไม่** |
| `slack-bot/data/raw-slack.json` | export จริงของบอท | **ไม่** |
| `slack-bot/data/decisions.json` | decision ที่คนกดบันทึกไว้ | **ไม่** |
| `pipeline/.env`, `slack-bot/.env` | token ทั้งหมด | **ไม่** |
| `pipeline/models/` | โมเดลที่ fine-tune เอง 2 ตัว (465 MB ต่อตัว) — **วัดแล้วแพ้ของสำเร็จรูป ระบบไม่ได้ใช้** ดู [EXPERIMENTS.md](EXPERIMENTS.md) | **ไม่** — เกิน limit GitHub |
| `pipeline/output/` | รายงาน HTML | **ไม่** |

ข้อมูลจริงและ token ทุกชิ้นอยู่แค่ในเครื่อง สิ่งที่อยู่ในรีโปคือโค้ดกับข้อมูลตัวอย่าง
ที่สังเคราะห์ขึ้นเท่านั้น

---

## 11. ข้อจำกัดที่ควรรู้ก่อนใช้จริง

- **บอทเปลี่ยนชื่อสถานะและเพิ่มสถานะที่ pipeline ไม่มี** ตั้ง `TAM_API_URL` แล้วนิยาม
  "งานหนึ่งชิ้น" มีเจ้าของเดียวคือฝั่ง Python (ข้อ 7.6) แต่บอร์ดของบอทยังเปลี่ยน
  `active → moving`, `resolved → done` และเพิ่ม `stalled` ให้งานที่เงียบเกิน
  `TAM_STALE_DAYS` ดังนั้น `/api/digest` กับบอร์ดจะดูเหมือนไม่ตรงกันได้ทั้งที่ตรงกัน
  ถ้าไม่ตั้ง `TAM_API_URL` ฝั่งบอทจับกลุ่มด้วย character trigram ของตัวเองซึ่งได้
  work item ไม่เหมือนฝั่ง Python — โหมดนั้นยังมีอยู่สำหรับเดโมออฟไลน์
- **decision, standup draft และ drift ยังไม่มีของเทียบใน pipeline** decision อ่านจาก
  ไฟล์ที่คนกดบันทึกเอง standup draft คำนวณจาก item ที่ได้มา และ **drift ไม่มีแหล่งข้อมูล
  จริงเลย** ต้องต่อ ticket system ก่อน (ตัวอย่างใน fixture โหลดเฉพาะเมื่อตั้ง
  `DEMO_FIXTURES=1` และหน้าจอจะติดป้ายบอกว่าเป็นตัวอย่าง)
- **relevance gate มีรูที่รู้อยู่ และมันบอกเอง** ต้องผ่านสองเงื่อนไข (`bm25 > 0` **และ**
  `cos >= TAM_MIN_COSINE`) วัดบน export จริง 936 record ด้วย `bge-m3` ได้ **3/4** คือกรอง
  query ขยะได้ 3 จาก 4 และไม่เสีย query จริงเลย (0/6) ตัวที่หลุดคือ `ๆๆๆ ฯฯฯ …` เพราะ
  `ๆ` `ฯ` เป็นอักขระไทยที่มีใน corpus จริง คำที่ตรงกันจึงเป็นของจริง — **นี่คือขอบเขต
  ไม่ใช่ค่าที่ตั้งผิด และ `check-api` รายงาน 3/4 ตรง ๆ ไม่ได้ปิด probe ตัวนั้นทิ้ง**
  ส่วน **cosine เดี่ยวใช้แทนไม่ได้** — probe ขยะทั้งสี่ตัวได้ cosine 0.457–0.731 ผ่านเกณฑ์
  0.45 หมด `npm run check-api` วัดซ้ำที่หน้างานทุกครั้ง เพราะเกณฑ์เป็นคุณสมบัติของ corpus
  คู่กับโมเดล ไม่ใช่ค่าคงที่ (ดูข้อ 7.6)
- **ยังไม่แปลง user id เป็นชื่อ** ผลลัพธ์แสดง `U01FE` ไม่ใช่ชื่อจริง ต้องเพิ่ม scope `users:read`
- **ค้นหาแบบ brute-force** ทุก query คิดคะแนนกับทุก record ไหวระดับหลายพันข้อความ
  ไม่ไหวระดับล้าน
- **คะแนน cosine เทียบข้ามช่องไม่ได้** 0.6 ในช่องหนึ่งไม่เท่ากับ 0.6 ในอีกช่อง ให้ดูลำดับ
  ไม่ใช่ตัวเลขดิบ
- **ไม่ export reaction, ไฟล์, attachment, ประวัติการแก้ข้อความ**
- **การกรอง noise เป็น word list** ไม่ได้เข้าใจประชด คำพูดอ้างอิง หรือบทสนทนานอกเรื่องยาวๆ

รายละเอียดเชิงเทคนิคทั้งหมด ผลการวัด และเหตุผลเบื้องหลังการออกแบบแต่ละอย่าง
อยู่ใน [pipeline/README.md](../pipeline/README.md)
