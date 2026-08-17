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
| พื้นที่ดิสก์ | ~1 GB | โมเดล embedding 458 MB โหลดครั้งแรกครั้งเดียว · **+2.1 GB** เมื่อใช้ preset ที่มี rerank (`hybrid-rerank`, `full`) เพราะต้องโหลด cross-encoder เพิ่ม |
| Slack token | ไม่จำเป็น | **ลองได้โดยไม่ต้องมี** — ดูข้อ 4 |

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

ถ้าขึ้น help และ pytest ผ่านหมด ถือว่าฝั่ง Python พร้อม

### 3.2 ตั้งค่า

```bash
cp .env.example .env          # อยู่ใน pipeline/
```

เปิด `pipeline/.env` แก้ค่า ทุกตัวมีคำอธิบายกำกับอยู่ในไฟล์ ที่สำคัญคือ:

| ตัวแปร | ต้องใส่ไหม | คืออะไร |
|---|---|---|
| `SLACK_TOKEN` | เฉพาะเมื่อจะดึงข้อมูลจริง | Bot token `xoxb-...` หรือ user token `xoxp-...` |
| `SLACK_CHANNEL_ID` | เฉพาะเมื่อจะดึงข้อมูลจริง | รหัสช่อง `C...` ไม่ใช่ชื่อช่อง |
| `EMBEDDING_MODEL` | ไม่ | มีค่า default อยู่แล้ว |
| `SUMMARIZER` | ไม่ | `template` (default, ออฟไลน์) หรือ `claude` |

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

ก่อนเสิร์ฟ มันจะพิมพ์ `Ready: 27 record(s), 5 topic(s), 1 blocked` แล้วเปิด
<http://localhost:8899> ได้เลย — หน้า digest มีงาน 5 ชิ้น ติดอยู่ 1 ชิ้น และมีชิ้นหนึ่ง
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
INFO Ready: 27 record(s), 5 topic(s), 1 blocked, summariser template
```

บรรทัดถัดจากนั้นมันพิมพ์ URL ทุกหน้า และ **token สำหรับ route ที่เขียนข้อมูล**
(`X-TAM-Token`) ตั้ง `TAM_ADMIN_TOKEN` ไว้ถ้าอยากให้ token เดิมอยู่ข้าม restart

### หน้าจอ

| URL | เห็นอะไร |
|---|---|
| `/` | Digest — งานที่ขยับ เรียงใหม่สุดก่อน |
| `/blockers` | เฉพาะที่ติด พร้อมข้อความที่เป็นหลักฐาน |
| `/item/{key}` | งานหนึ่งชิ้น — timeline ข้าม Slack และที่ประชุม · `{key}` ใช้ `item_id` ที่คงที่ (ticket key หรือ `c30a929`) ส่วนเลข cluster ยังเปิดได้แต่ rebuild แล้วเปลี่ยนความหมาย |
| `/search` | วางประโยคที่สงสัย ระบบหาข้อความต้นทางให้ |
| `/upload` | อัปโหลด `.vtt` / `.srt` รวมเข้า corpus |

### API

ข้อมูลชุดเดียวกันออกเป็น JSON ได้ ไม่ต้อง scrape HTML:

```bash
curl localhost:8899/api/digest
curl localhost:8899/api/blockers
curl localhost:8899/api/item/c30a929       # {key} คือ item_id ที่ /api/digest ส่งมา — คงที่ข้าม rebuild
curl localhost:8899/api/item/1             # เลข cluster ก็ยังใช้ได้ แต่ rebuild แล้วมันจะชี้งานคนละชิ้น
curl "localhost:8899/api/search?q=Android&k=10"
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
npm test                       # ชุดทดสอบฝั่งบอท
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

> **ขั้นสุดท้ายของ `check-api` คือ calibration และกับข้อมูลตัวอย่าง 27 record มัน "ไม่ผ่าน"**
> มันยิง query ขยะสามแบบแล้วพิมพ์ cosine ของแต่ละอันให้เห็น บน corpus ตัวอย่างจะได้
> ประมาณนี้:
>
> ```text
>   0.481  ✕ ผ่าน gate  "qqqzzzxxx wvwvwv jjjkkk zzzqqq"
>   0.210  · ถูกกรอง   "zxqv frobnicate wibble plumbus grommet"
>   0.767  ✕ ผ่าน gate  "ฟฟฟกกก ผผผ ฃฃฃ ฅฅฅ"
>   0.726  · query จริง "Profile module bug บน Android"
> ```
>
> ขยะภาษาไทยได้ 0.767 **สูงกว่า query จริงที่ได้ 0.726** — ไม่มีเกณฑ์ไหนแยกสองอันนี้ได้
> **นี่ไม่ใช่การติดตั้งพัง** เป็นข้อจำกัดของโมเดลกับ corpus เล็ก (corpus ยิ่งเล็ก เพื่อนบ้าน
> ที่ใกล้สุดของข้อความขยะยิ่งใกล้ บน corpus 42 record ขยะชุดเดียวกันถูกกรองหมด)
> ทางแก้คือเปลี่ยนโมเดล **ไม่ใช่ดัน `TAM_MIN_COSINE` ขึ้น** เพื่อกลบอาการ
>
> ข้อนี้ **ไม่ทำให้ `check-api` exit 1** เพราะมันวัดคุณสมบัติของโมเดล ไม่ใช่ว่าการต่อสองฝั่ง
> สำเร็จหรือไม่ — exit code ตอบเฉพาะเรื่องการต่อ ถ้าอยากให้ calibration ทำให้ fail ด้วย
> (เช่นใน CI บน corpus จริง) ใส่ `--strict-gate`

**ตั้งแล้วจะไม่มี fallback** ถ้า pipeline ตอบไม่ได้ บอทจะไม่สตาร์ตและบอกวิธีแก้ —
เพราะการแอบเสิร์ฟข้อมูลเก่าที่หน้าตาเหมือนของจริงอันตรายกว่าการไม่สตาร์ต

| มาจาก pipeline | บอทเติมเอง (ไม่มีตัวเทียบใน pipeline) |
|---|---|
| work item · สถานะ · หลักฐาน · timeline · ข้อความ · ประโยคสรุป · recall | decision — อ่านจากไฟล์ที่คนกดบันทึกไว้จริง (`data/decisions.json`) ว่างจนกว่าจะมีคนกด · standup draft — คำนวณจาก item ที่ได้มา · drift — **ไม่มีแหล่งจริงเลย** ว่างจนกว่าจะต่อ ticket system |

ชื่อสถานะสองฝั่งไม่เหมือนกัน pipeline ใช้ `active` / `blocked` / `resolved`
บอร์ดของบอทใช้ `blocked` / `stalled` / `moving` / `done` แปลงกันแบบนี้:
`blocked → blocked`, `resolved → done`, `active → moving` และ `active` ที่เงียบเกิน
`TAM_STALE_DAYS` → `stalled` มีแค่ `stalled` ที่เป็นข้อมูลใหม่ อีกสองอันเป็นการเปลี่ยนชื่อ

ดูภาพประกอบทั้งหมดได้ที่ [architecture.html](architecture.html)

---

## 8. คำสั่งที่ใช้บ่อย

คำสั่งข้างล่างใช้ `data/processed/sample_combined.json` จากข้อ 4 เปลี่ยนเป็นไฟล์ของคุณ
ได้ทุกอัน (แล้วเปลี่ยน `--days 3650` เป็น `--days 3` หรือ `7` ตามจริง)

```bash
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

ทุกโมดูลรับ `--help` — ทั้ง 22 ตัว

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
| `check-api` ขึ้น `✕ gate ไม่ทำงาน` | คาดไว้แล้วบน corpus ตัวอย่าง — โมเดล/corpus ชุดนั้นไม่แยก query ขยะออกจาก query จริง ดูข้อ 7.6 · ไม่ทำให้ exit 1 · เปลี่ยนโมเดล ไม่ใช่ดัน `TAM_MIN_COSINE` |
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
| `pipeline/models/` | โมเดลที่ fine-tune แล้ว (465 MB) | **ไม่** — เกิน limit GitHub |
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
- **เกณฑ์ตัดความเกี่ยวข้องดีได้เท่าโมเดลกับ corpus ที่เสิร์ฟ** corpus เล็กทำให้ query ขยะ
  อยู่ใกล้ข้อความจริงเกินไป บนข้อมูลตัวอย่าง 27 record query ขยะได้ cosine 0.481 เทียบเกณฑ์
  0.45 คือแยกไม่ออก `npm run check-api` รายงานให้เห็นก่อนขึ้นเวที
- **ยังไม่แปลง user id เป็นชื่อ** ผลลัพธ์แสดง `U01FE` ไม่ใช่ชื่อจริง ต้องเพิ่ม scope `users:read`
- **ค้นหาแบบ brute-force** ทุก query คิดคะแนนกับทุก record ไหวระดับหลายพันข้อความ
  ไม่ไหวระดับล้าน
- **คะแนน cosine เทียบข้ามช่องไม่ได้** 0.6 ในช่องหนึ่งไม่เท่ากับ 0.6 ในอีกช่อง ให้ดูลำดับ
  ไม่ใช่ตัวเลขดิบ
- **ไม่ export reaction, ไฟล์, attachment, ประวัติการแก้ข้อความ**
- **การกรอง noise เป็น word list** ไม่ได้เข้าใจประชด คำพูดอ้างอิง หรือบทสนทนานอกเรื่องยาวๆ

รายละเอียดเชิงเทคนิคทั้งหมด ผลการวัด และเหตุผลเบื้องหลังการออกแบบแต่ละอย่าง
อยู่ใน [pipeline/README.md](../pipeline/README.md)
