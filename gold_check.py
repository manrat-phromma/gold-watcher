"""Gold Price Watcher - เวอร์ชัน GitHub Actions
- แจ้งเตือนเมื่อราคาขยับเกิน Threshold จากราคาฐาน
- ปรับฐานราคาตามเวลา ทุก 00:00 / 06:00 / 12:00 / 18:00 (เวลาไทย)
  * รอบ 12:00 / 18:00 แท็ก @everyone
  * รอบ 00:00 / 06:00 ไม่แท็ก (ช่วงเวลานอน จะได้ไม่มีเสียงรบกวน)
"""
import os
import json
import requests
from datetime import datetime, timezone, timedelta

BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
PRICE_THRESHOLD_USD = 5.0          # แจ้งเตือนเมื่อราคาขยับเกินกี่ USD
THAI_GOLD_FACTOR = 0.4729
STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ชั่วโมงที่จะปรับฐานราคา (เวลาไทย)
RESET_HOURS = [0, 6, 12, 18]

# ชั่วโมงที่ "ไม่ต้องแท็ก @everyone" ตอนปรับฐาน (ช่วงเวลานอน)
QUIET_HOURS = [0, 6]

# เวลาไทย (UTC+7)
TZ_TH = timezone(timedelta(hours=7))


def get_gold_price():
    try:
        r = requests.get(BINANCE_API_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        print(f"[ERROR] ดึงราคาทองไม่สำเร็จ: {e}")
        return None


def get_usd_thb_rate():
    try:
        r = requests.get(EXCHANGE_RATE_API_URL, timeout=10)
        r.raise_for_status()
        return float(r.json()["rates"]["THB"])
    except Exception as e:
        print(f"[ERROR] ดึงเรท USD/THB ไม่สำเร็จ: {e}")
        return None


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(base_price, last_reset_iso):
    with open(STATE_FILE, "w") as f:
        json.dump({"base_price": base_price, "last_reset": last_reset_iso}, f)


def latest_reset_boundary(now_th):
    """หาจุดเวลาปรับฐานล่าสุดที่ผ่านมาแล้ว (00/06/12/18 น. เวลาไทย)"""
    candidates = []
    for day_offset in [0, -1]:  # วันนี้ และเมื่อวาน (เผื่อกรณีหลังเที่ยงคืน)
        d = (now_th + timedelta(days=day_offset)).date()
        for h in RESET_HOURS:
            b = datetime(d.year, d.month, d.day, h, 0, 0, tzinfo=TZ_TH)
            if b <= now_th:
                candidates.append(b)
    return max(candidates)


def build_thb_section(current):
    rate = get_usd_thb_rate()
    if rate:
        thb_oz = current * rate
        thai_baht = thb_oz * THAI_GOLD_FACTOR
        return (
            f"🇹🇭 **ราคาเป็นเงินบาท:** `{thb_oz:,.0f} THB/ออนซ์`\n"
            f"🏅 **เทียบทองไทยบาทละ (โดยประมาณ):** `{thai_baht:,.0f} THB`\n"
            f"💱 **เรทที่ใช้:** `{rate:.2f} THB/USD`\n"
        )
    return "🇹🇭 _ดึงเรท THB ไม่ได้ในรอบนี้_\n"


def post_discord(content):
    if not DISCORD_WEBHOOK_URL:
        print("[SKIP] ไม่พบ DISCORD_WEBHOOK_URL ใน Secrets")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL,
                          json={"content": content, "username": "Gold Price Watcher 🥇"},
                          timeout=10)
        r.raise_for_status()
        print("[OK] ส่งข้อความเข้า Discord สำเร็จ")
    except Exception as e:
        print(f"[ERROR] ส่ง Discord ไม่สำเร็จ: {e}")


def send_alert(current, change, base):
    emoji = "🚀📈" if change > 0 else "🔻📉"
    ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"@everyone\n"
        f"## {emoji} แจ้งเตือนราคาทองคำ (Gold Price Alert)\n"
        f"> 🔔 **ราคาทองคำเปลี่ยนแปลงเกินเกณฑ์ที่ตั้งไว้!**\n\n"
        f"💰 **ราคาปัจจุบัน:** `{current:,.2f} USD`\n"
        f"📊 **เปลี่ยนแปลง:** `{change:+,.2f} USD`\n"
        f"📌 **ราคาฐานก่อนหน้า:** `{base:,.2f} USD`\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n\n"
        f"_ข้อมูลจาก Binance PAXGUSDT | รันบน GitHub Actions ☁️_"
    )
    post_discord(content)


def send_reset_notice(current, old_base, reset_hour):
    """ข้อความปรับฐานราคาตามเวลา - แท็ก @everyone เฉพาะรอบกลางวัน"""
    ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
    diff = current - old_base
    mention = "" if reset_hour in QUIET_HOURS else "@everyone\n"

    content = (
        f"{mention}"
        f"## 🔄 ปรับฐานราคาตามเวลา {reset_hour:02d}:00 น.\n"
        f"💰 **ราคาปัจจุบัน (ฐานใหม่):** `{current:,.2f} USD`\n"
        f"📌 **ฐานเดิม:** `{old_base:,.2f} USD` "
        f"(ขยับ `{diff:+,.2f} USD` ในรอบที่ผ่านมา)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n\n"
        f"_ระบบทำงานปกติ ✅ | รอบถัดไป: "
        f"{RESET_HOURS[(RESET_HOURS.index(reset_hour) + 1) % len(RESET_HOURS)]:02d}:00 น._"
    )
    post_discord(content)


def main():
    now_th = datetime.now(TZ_TH)
    current = get_gold_price()
    if current is None:
        print("[INFO] ข้ามรอบนี้ รอรอบถัดไป")
        return

    boundary = latest_reset_boundary(now_th)
    state = load_state()

    # ----- รอบแรกสุด -----
    if state is None or "base_price" not in state:
        save_state(current, boundary.isoformat())
        print(f"[INFO] รอบแรก จดราคาฐาน = {current:,.2f} USD")
        post_discord(
            f"✅ **Gold Watcher เริ่มทำงานบน GitHub Actions แล้ว!**\n"
            f"ราคาฐานตั้งต้น: `{current:,.2f} USD` | Threshold: `±{PRICE_THRESHOLD_USD} USD`\n"
            f"ปรับฐานราคาตามเวลา ทุก 00:00 / 06:00 / 12:00 / 18:00 น. "
            f"(แท็ก @everyone เฉพาะ 12:00 / 18:00)"
        )
        return

    base = state["base_price"]
    last_reset_str = state.get("last_reset", "")

    # ----- เช็กว่าถึงรอบปรับฐานหรือยัง -----
    need_reset = True
    if last_reset_str:
        try:
            last_reset = datetime.fromisoformat(last_reset_str)
            need_reset = last_reset < boundary
        except Exception:
            need_reset = True

    diff = current - base
    print(f"[INFO] ปัจจุบัน {current:,.2f} | ฐาน {base:,.2f} | ต่าง {diff:+,.2f} USD")

    # ----- Threshold มาก่อน: ถ้าราคาขยับเกินเกณฑ์ แจ้งเตือนตามปกติ -----
    if abs(diff) >= PRICE_THRESHOLD_USD:
        print("[ALERT] เกิน Threshold -> แจ้งเตือน!")
        send_alert(current, diff, base)
        save_state(current, boundary.isoformat())
        return

    # ----- ไม่เกินเกณฑ์ แต่ถึงรอบปรับฐานตามเวลา -----
    if need_reset:
        print(f"[RESET] ปรับฐานราคาตามเวลา ({boundary.strftime('%H:%M')} น.) "
              f"{base:,.2f} -> {current:,.2f} USD")
        save_state(current, boundary.isoformat())
        send_reset_notice(current, base, boundary.hour)
        return

    print("[INFO] ยังไม่เกิน Threshold และยังไม่ถึงรอบปรับฐาน")


if __name__ == "__main__":
    main()
