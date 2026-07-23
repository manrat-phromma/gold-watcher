"""Gold Price Watcher - เวอร์ชัน Cloud Loop (สำหรับ Render)
- รันค้างตลอด เช็กราคาทุก 5 นาที (เวลาเป๊ะ ไม่ต้องรอใครปลุก)
- เก็บราคาฐานถาวรบน GitHub (รีสตาร์ทแล้วฐานไม่หาย)
- มี web server จำลองไว้ให้ Render/cron-job.org ตรวจสถานะ
"""
import os
import time
import json
import base64
import threading
import requests
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

# ==================== CONFIGURATION ====================
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
COINBASE_API_URL = "https://api.coinbase.com/v2/prices/PAXG-USD/spot"
KRAKEN_API_URL = "https://api.kraken.com/0/public/Ticker?pair=PAXGUSD"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
HSH_API_URL = "https://apicheckprice.huasengheng.com/api/values/getprice/"

PRICE_THRESHOLD_USD = 5.0
THAI_GOLD_FACTOR = 0.4729
CHECK_INTERVAL_SECONDS = 300        # เช็กทุก 5 นาที (แก้ได้)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")   # เช่น manrat-phromma/gold-watcher
STATE_PATH = "state.json"

RESET_HOURS = [0, 6, 12, 18]
QUIET_HOURS = [0, 6]
TZ_TH = timezone(timedelta(hours=7))

STATE = {"base_price": None, "last_reset": None, "sha": None}

# ============ WEB SERVER (ให้ Render/cron-job ตรวจสถานะ) ============
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        base = STATE.get("base_price")
        now = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
        msg = f"Gold Watcher OK\nBase: {base}\nTime(TH): {now}\n"
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, *args):
        pass   # ไม่ต้อง log ทุก request ให้รก


def start_web_server():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), StatusHandler).serve_forever()


# ==================== STATE บน GITHUB ====================
def gh_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"}


def load_state_from_github():
    """ดึงราคาฐานล่าสุดจาก GitHub (ทำตอนเริ่มทำงาน/หลังรีสตาร์ท)"""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print("[WARN] ไม่ได้ตั้งค่า GitHub -> ฐานจะไม่ถาวร")
        return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"
        r = requests.get(url, headers=gh_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        content = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        STATE["base_price"] = content.get("base_price")
        lr = content.get("last_reset")
        STATE["last_reset"] = datetime.fromisoformat(lr) if lr else None
        STATE["sha"] = data["sha"]
        print(f"[OK] โหลดฐานจาก GitHub: {STATE['base_price']:,.2f} USD "
              f"(รีเซ็ตล่าสุด {STATE['last_reset']})")
    except Exception as e:
        print(f"[WARN] โหลดฐานจาก GitHub ไม่ได้ ({e}) จะเริ่มนับใหม่")


def save_state_to_github():
    """บันทึกราคาฐานกลับขึ้น GitHub"""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return
    try:
        body = json.dumps({
            "base_price": STATE["base_price"],
            "last_reset": STATE["last_reset"].isoformat() if STATE["last_reset"] else None,
        })
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{STATE_PATH}"
        payload = {
            "message": "update base price (render loop)",
            "content": base64.b64encode(body.encode("utf-8")).decode("utf-8"),
        }
        if STATE.get("sha"):
            payload["sha"] = STATE["sha"]
        r = requests.put(url, headers=gh_headers(), json=payload, timeout=15)
        r.raise_for_status()
        STATE["sha"] = r.json()["content"]["sha"]
        print("[OK] บันทึกฐานขึ้น GitHub แล้ว")
    except Exception as e:
        print(f"[WARN] บันทึกฐานขึ้น GitHub ไม่สำเร็จ: {e}")


# ==================== ดึงราคา ====================
def get_gold_price():
    for name, url, extract in [
        ("Binance", BINANCE_API_URL, lambda j: float(j["price"])),
        ("Coinbase", COINBASE_API_URL, lambda j: float(j["data"]["amount"])),
        ("Kraken", KRAKEN_API_URL,
         lambda j: float(j["result"][next(iter(j["result"]))]["c"][0])),
    ]:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            price = extract(r.json())
            print(f"[OK] ราคาจาก {name}: {price:,.2f} USD")
            return price
        except Exception as e:
            print(f"[WARN] {name} ใช้ไม่ได้ ({e})")
    print("[ERROR] ดึงราคาไม่ได้เลยในรอบนี้")
    return None


def get_hsh_prices():
    try:
        r = requests.get(HSH_API_URL, timeout=10)
        r.raise_for_status()
        out = {}
        for item in r.json():
            g = item.get("GoldType")
            if g in ("HSH", "REF"):
                out[g] = {
                    "buy": float(str(item["Buy"]).replace(",", "")),
                    "sell": float(str(item["Sell"]).replace(",", "")),
                    "time": item.get("StrTimeUpdate", ""),
                }
        return out if "HSH" in out else None
    except Exception as e:
        print(f"[WARN] ดึงราคาฮั่วเซ่งเฮงไม่ได้: {e}")
        return None


def get_usd_thb_rate():
    try:
        r = requests.get(EXCHANGE_RATE_API_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["THB"])
    except Exception:
        return None


def build_thb_section(current):
    hsh = get_hsh_prices()
    if hsh:
        lines = (f"🏪 **ฮั่วเซ่งเฮง (ทองแท่ง 96.5%)**\n"
                 f"　🟢 รับซื้อ: `{hsh['HSH']['buy']:,.0f} THB` "
                 f"| 🔴 ขายออก: `{hsh['HSH']['sell']:,.0f} THB`\n")
        if "REF" in hsh:
            lines += (f"🏛️ **ราคาสมาคมค้าทองคำ**\n"
                      f"　🟢 รับซื้อ: `{hsh['REF']['buy']:,.0f} THB` "
                      f"| 🔴 ขายออก: `{hsh['REF']['sell']:,.0f} THB`\n")
        if hsh["HSH"].get("time"):
            lines += f"　_{hsh['HSH']['time']}_\n"
        return lines
    rate = get_usd_thb_rate()
    if rate:
        return (f"🏅 **เทียบทองไทยบาทละ (ประมาณ):** "
                f"`{current * rate * THAI_GOLD_FACTOR:,.0f} THB`\n"
                f"💱 **เรทที่ใช้:** `{rate:.2f} THB/USD`\n")
    return "🇹🇭 _ดึงราคาฝั่งไทยไม่ได้ในรอบนี้_\n"


# ==================== DISCORD ====================
def post_discord(content):
    if not DISCORD_WEBHOOK_URL:
        print("[SKIP] ไม่พบ DISCORD_WEBHOOK_URL")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL,
                          json={"content": content,
                                "username": "Gold Price Watcher 🥇"},
                          timeout=10)
        r.raise_for_status()
        print("[OK] ส่ง Discord สำเร็จ")
    except Exception as e:
        print(f"[ERROR] ส่ง Discord ไม่สำเร็จ: {e}")


def send_alert(current, change, base):
    emoji = "🚀📈" if change > 0 else "🔻📉"
    ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
    post_discord(
        f"@everyone\n"
        f"## {emoji} แจ้งเตือนราคาทองคำ (Gold Price Alert)\n"
        f"> 🔔 **ราคาทองคำเปลี่ยนแปลงเกินเกณฑ์ที่ตั้งไว้!**\n\n"
        f"💰 **ราคาทองโลก:** `{current:,.2f} USD` "
        f"(เปลี่ยนแปลง `{change:+,.2f}` จากฐาน `{base:,.2f}`)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n"
    )


def send_reset_notice(current, old_base, hour):
    ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
    mention = "" if hour in QUIET_HOURS else "@everyone\n"
    nxt = RESET_HOURS[(RESET_HOURS.index(hour) + 1) % len(RESET_HOURS)]
    post_discord(
        f"{mention}"
        f"## 🔄 ปรับฐานราคาตามเวลา {hour:02d}:00 น.\n"
        f"💰 **ราคาทองโลก (ฐานใหม่):** `{current:,.2f} USD` "
        f"(ขยับ `{current - old_base:+,.2f}` ในรอบที่ผ่านมา)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n\n"
        f"_ระบบทำงานปกติ ✅ | รอบถัดไป: {nxt:02d}:00 น._"
    )


# ==================== LOGIC ====================
def latest_reset_boundary(now_th):
    cands = []
    for off in [0, -1]:
        d = (now_th + timedelta(days=off)).date()
        for h in RESET_HOURS:
            b = datetime(d.year, d.month, d.day, h, 0, 0, tzinfo=TZ_TH)
            if b <= now_th:
                cands.append(b)
    return max(cands)


def check_once():
    now_th = datetime.now(TZ_TH)
    current = get_gold_price()
    if current is None:
        return

    boundary = latest_reset_boundary(now_th)

    if STATE["base_price"] is None:
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state_to_github()
        post_discord(
            f"✅ **Gold Watcher เริ่มทำงานแล้ว (Render Loop Mode)**\n"
            f"ราคาฐานตั้งต้น: `{current:,.2f} USD` | Threshold: `±{PRICE_THRESHOLD_USD} USD`\n"
            f"เช็กทุก {CHECK_INTERVAL_SECONDS // 60} นาที | "
            f"ปรับฐาน 00:00 / 06:00 / 12:00 / 18:00 น."
        )
        return

    base = STATE["base_price"]
    diff = current - base
    need_reset = STATE["last_reset"] is None or STATE["last_reset"] < boundary
    print(f"[{now_th.strftime('%H:%M:%S')}] ปัจจุบัน {current:,.2f} | "
          f"ฐาน {base:,.2f} | ต่าง {diff:+,.2f}")

    if abs(diff) >= PRICE_THRESHOLD_USD:
        send_alert(current, diff, base)
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state_to_github()
        return

    if need_reset:
        print(f"[RESET] ปรับฐานตามเวลา {boundary.strftime('%H:%M')} น.")
        send_reset_notice(current, base, boundary.hour)
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state_to_github()


if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()
    print("🚀 Gold Watcher (Render Loop) เริ่มทำงาน")
    load_state_from_github()
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[ERROR] ลูปพังชั่วคราว: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
