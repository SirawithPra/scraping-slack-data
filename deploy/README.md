# ให้มันรันเองทุกเช้า (macOS · launchd)

ปัญหาที่แก้: `tam.ingest.daily` ต้องมีคนพิมพ์ และ dashboard เป็น process ที่เปิดค้างไว้
ปิดเครื่องคือหาย — ระบบที่ต้องรอคนพิมพ์ทุกเช้าคือระบบที่จะไม่ได้รัน

ใช้ **launchd** ไม่ใช่ `cron` เพราะบน macOS ยุคใหม่ `cron` ไม่ได้รับสิทธิ์เท่า และ
launchd จัดการ "เครื่องหลับตอนถึงเวลา" ให้เอง — `StartCalendarInterval` จะรันงานที่พลาดไป
เมื่อเครื่องตื่น ซึ่ง `cron` ทำไม่ได้

## ติดตั้ง

```bash
cd deploy && ./install.sh
```

สร้างสองงาน:

| งาน | ทำอะไร | เมื่อไหร่ |
|---|---|---|
| `com.tam.dashboard` | เปิด dashboard ที่ port 8899 และเปิดใหม่ถ้าตาย | ตอน login และตลอดเวลา |
| `com.tam.daily` | `python3 -m tam.ingest.daily` — ดึงทุกช่อง → merge → rebuild | **08:30 ทุกวัน** |

log อยู่ที่ `deploy/logs/` — `dashboard.log` และ `daily.log` เก็บทั้ง stdout และ stderr
เพราะงานที่รันเองแล้วเงียบตอนพัง คือสิ่งที่แย่ที่สุด

## ตรวจว่าทำงานอยู่

```bash
launchctl list | grep com.tam          # เห็นสองบรรทัด = โหลดแล้ว
tail -20 deploy/logs/daily.log         # เช้าล่าสุดทำอะไรไป
curl -s localhost:8899/api/health      # dashboard ตอบไหม
```

`launchctl list` คอลัมน์แรกคือ PID (`-` คือไม่ได้รันอยู่ ปกติสำหรับงานตามเวลา)
คอลัมน์ที่สองคือ exit code ของรอบล่าสุด — **ไม่ใช่ 0 คือรอบล่าสุดพัง ให้ไปดู log**

## สั่งรันเดี๋ยวนี้ ไม่ต้องรอ 08:30

```bash
launchctl kickstart -k gui/$(id -u)/com.tam.daily
tail -f deploy/logs/daily.log
```

## ถอนออก

```bash
cd deploy && ./uninstall.sh
```

ถอนแล้วไม่เหลืออะไรค้าง — ไม่แตะข้อมูล ไม่แตะ `.env` และ dashboard ที่รันอยู่จะถูกหยุด

## ข้อควรรู้

- **token อ่านจาก `pipeline/.env`** ไม่ได้ฝังใน plist — plist อ่านได้ด้วย user อื่นบนเครื่อง
  ส่วน `.env` เป็นไฟล์ที่ gitignore แล้วและสิทธิ์เป็นของคุณ
- **`daily` ข้ามเช้าที่ไม่มีข้อความใหม่** ไม่เขียน corpus และไม่ rebuild — log จะบอกว่าข้าม
- **ยังไม่ส่งอะไรเข้า Slack** เพราะ `ENABLE_SCHEDULE` ในบอทยังปิด งานนี้แค่ทำให้ข้อมูลสด
  การส่ง digest ให้ทีมเป็นการตัดสินใจแยก และ `postguard` ยังบล็อคทุกคนยกเว้นเจ้าของเครื่อง
