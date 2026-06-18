# คู่มือติดตั้ง (ภาษาไทย)

คู่มือติดตั้งและเริ่มใช้งาน **AI Tokens Observability** (`pulse`) — แดชบอร์ดดูการใช้ token
แบบ live สำหรับ Claude Code, Codex CLI และ Gemini CLI

> ฉบับภาษาอังกฤษอยู่ในหัวข้อ *Installation* ของ [`README.md`](../README.md)

---

## 1. ความต้องการของระบบ

- **Python 3.9 ขึ้นไป** — ใช้แค่ stdlib ล้วน ๆ **ไม่ต้อง `pip install` อะไรเลย**
- **Claude Code** ติดตั้งอยู่ในเครื่อง (transcript จะอยู่ใต้ `~/.claude/projects/`)
- **macOS หรือ Linux**
- *(ทางเลือก)* มี [`rtk`](https://github.com/) อยู่ใน `PATH` เพื่อแสดงแผง savings
  — ถ้าไม่มี แผงนั้นจะขึ้น `n/a` เฉย ๆ
- *(ทางเลือก)* อินเทอร์เน็ต — ใช้แค่โหลด Chart.js จาก CDN กับดึงเรต USD→THB
  เท่านั้น (ออฟไลน์ก็ยังทำงานได้ แค่ฟีเจอร์สองอย่างนี้ถูกข้ามไป)

ตรวจเวอร์ชัน Python:

```bash
python3 --version    # ต้อง >= 3.9
```

---

## 2. ติดตั้งแบบ git clone (แนะนำ — ไม่ต้องตั้งค่าอะไร)

```bash
git clone https://github.com/supachai-j/ai-tokens-observability.git
cd ai-tokens-observability
python3 pulse.py scan          # สร้าง index รอบแรก (~ <1 วินาทีต่อข้อมูล 300MB)
python3 pulse.py serve --open  # เปิดแดชบอร์ด + เด้ง browser ให้อัตโนมัติ
```

เท่านี้เลย — ไม่ต้องทำ virtualenv ไม่มี dependency ใด ๆ

แดชบอร์ดจะอยู่ที่ <http://localhost:8377> (เปลี่ยน port ได้ด้วย `--port`)
ตัว server **bind แค่ `127.0.0.1`** เท่านั้น จึงไม่เปิดออกสู่เน็ตเวิร์กภายนอก

---

## 3. ติดตั้งแบบ pipx (ได้คำสั่ง `rtk-pulse` แบบ global)

```bash
pipx install git+https://github.com/supachai-j/ai-tokens-observability
rtk-pulse serve --open
```

วิธีนี้จะติดตั้งคำสั่ง `rtk-pulse` ให้เรียกได้จากทุกที่ โดย `rtk-pulse` เป็น
alias ของ `python3 pulse.py` ทุกประการ — subcommand และ env var ทั้งหมดใช้ได้เหมือนกันเป๊ะ

---

## 4. คำสั่งใช้งานหลัก

```bash
python3 pulse.py serve --open      # แดชบอร์ดที่ http://localhost:8377
python3 pulse.py report [--days N] # รายงานสรุปบน terminal
python3 pulse.py digest [--days N] [--format text|json|html]  # สรุปราย week-over-week (html ไว้ส่งอีเมล)
python3 pulse.py save              # บันทึก snapshot ของวันนี้ลง history.jsonl
python3 pulse.py scan [--force]    # สร้าง/อัปเดต index (--force = rebuild ใหม่ทั้งหมด)
python3 pulse.py export            # export snapshot ของเครื่องนี้ไป ~/.config/rtk-pulse/nodes/
```

> ใช้ pipx ก็แทน `python3 pulse.py` ด้วย `rtk-pulse` ได้เลย

---

## 5. การตั้งค่าเพิ่มเติม (ทางเลือก) — Environment Variables

```bash
# alias ใน shell ให้เรียกสั้น ๆ
alias pulse='python3 ~/workspace/ai-tokens-observability/pulse.py'

# ปักเรต USD->THB เอง (ข้ามการดึงเรตสด)
export RTK_PULSE_THB=33.0

# ย้ายโฟลเดอร์เก็บข้อมูล (index, history, fx cache); ค่า default = ~/.config/rtk-pulse
export RTK_PULSE_HOME=~/somewhere/else

# งบรายเดือน (หน่วย USD) — เปิดการ์ด budget + มิเตอร์สี
# พร้อมประมาณยอดสิ้นเดือน และบอกวันที่คาดว่าจะเกินงบถ้าใช้ในอัตราเดิม
export RTK_PULSE_BUDGET=20.0

# threshold แจ้งเตือนงบ คิดเป็น % ของงบ (default: 80,100)
# เด้ง banner บนแดชบอร์ด + OS notification (เตือนครั้งเดียวต่อ threshold ต่อเดือน)
export RTK_PULSE_BUDGET_ALERT=80,100

# แจ้งเตือนค่าใช้จ่ายพุ่ง: เตือนเมื่อยอดวันนี้ >= N เท่าของค่าเฉลี่ย 7 วันย้อนหลัง
# (นับเฉพาะวันที่มีการใช้งาน และมีพื้น $ กำกับ) default = 3 เท่า / $5; ใส่ 0 เพื่อปิด
export RTK_PULSE_SPIKE=3
export RTK_PULSE_SPIKE_MIN=5

# จำนวน trace step สูงสุดที่โชว์ใน session drilldown (default 600, ต่ำสุด 50)
export RTK_PULSE_TRACE_MAX=600
```

---

## 6. ปรับราคาเอง (`pricing.json`)

วางไฟล์ `pricing.json` ไว้ใน `~/.config/rtk-pulse/` (หรือ `$RTK_PULSE_HOME`)
เพื่อ override หรือเพิ่มตารางราคา — เหมาะกับเรต enterprise ที่ต่อรองมา
หรือโมเดลใหม่ที่ยังไม่อยู่ในตาราง

รูปแบบ: JSON object ที่ key เป็น **ส่วนหนึ่งของชื่อโมเดล (substring)** และ value
เป็น `[ราคา_input_ต่อ_MTok, ราคา_output_ต่อ_MTok]` หน่วย USD:

```json
{
  "opus-4-8": [3.5, 17.5],
  "my-new-model": [1.0, 4.0]
}
```

กติกาการ match:
- เทียบ key แบบ **ไม่สนตัวพิมพ์ใหญ่เล็ก** เป็น substring ของชื่อโมเดล
- ถ้าหลาย key match พร้อมกัน → **key ที่ยาวที่สุดชนะ** (เจาะจงสุดมาก่อน)
- ค่าที่ override ชนะทั้งเรต built-in และบล็อก special-case ของ gpt-5

> **สำคัญ:** ค่าใช้จ่ายถูกคำนวณ**ตอน scan** แล้วเก็บลง index — session ใหม่จะใช้เรตที่
> override โดยอัตโนมัติ แต่ถ้าจะให้คำนวณราคาของ history เดิมใหม่ ต้องรัน
> `python3 pulse.py scan --force` หลังแก้ `pricing.json`

---

## 7. แก้ปัญหาที่พบบ่อย (Troubleshooting)

### `OSError: [Errno 48] Address already in use`
port `8377` (ค่า default) มี process เก่าค้างถืออยู่ — มักเป็น `pulse.py serve`
ที่รันค้างไว้รอบก่อน

**ทางแก้ที่ 1 — ใช้ port อื่น:**
```bash
python3 pulse.py serve --port 8378 --open
```

**ทางแก้ที่ 2 — หา process เก่าแล้ว kill ทิ้ง:**
```bash
lsof -nP -iTCP:8377 -sTCP:LISTEN   # ดู PID ที่ถือ port อยู่
kill <PID>                          # ปิด process นั้น แล้วค่อยรันใหม่
```

### แดชบอร์ดเปิดมาแล้วว่างเปล่า / ไม่มีข้อมูล
- ตรวจว่ามี transcript อยู่จริงใต้ `~/.claude/projects/` (ต้องเคยใช้ Claude Code มาก่อน)
- รัน `python3 pulse.py scan --force` เพื่อ rebuild index ใหม่

### แผง savings ขึ้น `n/a`
ปกติ — แปลว่าไม่มี `rtk` อยู่ใน `PATH` ติดตั้ง `rtk` ถ้าต้องการแผงนี้

### Browser ไม่เด้งขึ้นเองตอนใส่ `--open`
เปิดเองได้ที่ <http://localhost:8377> (หรือ port ที่กำหนดด้วย `--port`)

---

## 8. ถอนการติดตั้ง (Uninstall)

- **แบบ clone:** ลบโฟลเดอร์ที่ clone มา และลบ `~/.config/rtk-pulse/`
- **แบบ pipx:** `pipx uninstall ai-tokens-observability` แล้วลบ `~/.config/rtk-pulse/`
