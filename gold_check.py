"""Gold Price Watcher - เวอร์ชัน GitHub Actions
- ดึงราคาทองโลก (PAXG/USD) ไล่ทีละแหล่ง: Binance -> Coinbase -> Kraken
- ดึงราคารับซื้อ/ขายออกจริงจากฮั่วเซ่งเฮง (ถ้าดึงไม่ได้ ใช้สูตรประมาณแทน)
- แจ้งเตือนเมื่อราคาขยับเกิน Threshold จากราคาฐาน
- ปรับฐานราคาตามเวลา ทุก 00:00 / 06:00 / 12:00 / 18:00 (เวลาไทย)
  * รอบ 12:00 / 18:00 แท็ก @everyone | รอบ 00:00 / 06:00 เงียบ
"""
import os
import json
import requests
from datetime import datetime, timezone, timedelta

# ==================== CONFIGURATION ====================

BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
COINBASE_API_URL = "https://api.coinbase.com/v2/prices/PAXG-USD/spot"
KRAKEN_API_URL = "https://api.kraken.com/0/public/Ticker?pair=PAXGUSD"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
HSH_API_URL = "https://apicheckprice.huasengheng.com/api/values/getprice/"

PRICE_THRESHOLD_USD = 5.0          # แจ้งเตือนเมื่อราคาขยับเกินกี่ USD
THAI_GOLD_FACTOR = 0.4729          # สูตรประมาณ (ใช้เป็นตัวสำรองถ้า API ฮั่วเซ่งเฮงล่ม)
STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

RESET_HOURS = [0, 6, 12, 18]       # ชั่วโมงปรับฐานราคา (เวลาไทย)
QUIET_HOURS = [0, 6]               # รอบที่ไม่แท็ก @everyone

TZ_TH = timezone(timedelta(hours=7))

# ========================================================


def get_gold_price():
    """ดึงราคาทองโลก (PAXG/USD) ไล่ทีละแหล่ง"""
    try:
        r = requests.get(BINANCE_API_URL, timeout=10)
        r.raise_for_status()
        price = float(r.json()["price"])
        print(f"[OK] ได้ราคาจาก Binance: {price:,.2f} USD")
        return price
    except Exception as e:
        print(f"[WARN] Binance ใช้ไม่ได้ ({e}) ลองแหล่งถัดไป...")

    try:
        r = requests.get(COINBASE_API_URL, timeout=10)
        r.raise_for_status()
        price = float(r.json()["data"]["amount"])
        print(f"[OK] ได้ราคาจาก Coinbase: {price:,.2f} USD")
        return price
    except Exception as e:
        print(f"[WARN] Coinbase ใช้ไม่ได้ ({e}) ลองแหล่งถัดไป...")

    try:
        r = requests.get(KRAKEN_API_URL, timeout=10)
        r.raise_for_status()
        result = r.json()["result"]
        first_pair = next(iter(result))
        price = float(result[first_pair]["c"][0])
        print(f"[OK] ได้ราคาจาก Kraken: {price:,.2f} USD")
        return price
    except Exception as e:
        print(f"[ERROR] ทุกแหล่งราคาใช้ไม่ได้ในรอบนี้: {e}")

    return None


def get_hsh_prices():
    """ดึงราคารับซื้อ/ขายออกจากฮั่วเซ่งเฮง (ทองแท่ง 96.5%)
    คืนค่า dict หรือ None ถ้าดึงไม่ได้"""
    try:
        r = requests.get(HSH_API_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        out = {}
        for item in data:
            gtype = item.get("GoldType")
            if gtype in ("HSH", "REF"):
                out[gtype] = {
                    "buy": float(str(item["Buy"]).replace(",", "")),
                    "sell": float(str(item["Sell"]).replace(",", "")),
                    "time": item.get("StrTimeUpdate", ""),
                }
        if "HSH" in out:
            print(f"[OK] ได้ราคาฮั่วเซ่งเฮง: รับซื้อ {out['HSH']['buy']:,.0f} / "
                  f"ขายออก {out['HSH']['sell']:,.0f} THB")
            return out
    except Exception as e:
        print(f"[WARN] ดึงราคาฮั่วเซ่งเฮงไม่ได้ ({e}) จะใช้สูตรประมาณแทน")
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
    for day_offset in [0, -1]:
        d = (now_th + timedelta(days=day_offset)).date()
        for h in RESET_HOURS:
            b = datetime(d.year, d.month, d.day, h, 0, 0, tzinfo=TZ_TH)
            if b <= now_th:
                candidates.append(b)
    return max(candidates)


def build_thb_section(current):
    """สร้างข้อความส่วนราคาไทย: ใช้ราคาจริงฮั่วเซ่งเฮงก่อน ถ้าไม่ได้ใช้สูตรประมาณ"""
    hsh = get_hsh_prices()
    if hsh:
        lines = (
            f"🏪 **ฮั่วเซ่งเฮง (ทองแท่ง 96.5%)**\n"
            f"　🟢 รับซื้อ: `{hsh['HSH']['buy']:,.0f} THB` "
            f"| 🔴 ขายออก: `{hsh['HSH']['sell']:,.0f} THB`\n"
        )
        if "REF" in hsh:
            lines += (
                f"🏛️ **ราคาสมาคมค้าทองคำ**\n"
                f"　🟢 รับซื้อ: `{hsh['REF']['buy']:,.0f} THB` "
                f"| 🔴 ขายออก: `{hsh['REF']['sell']:,.0f} THB`\n"
            )
        if hsh["HSH"].get("time"):
            lines += f"　_{hsh['HSH']['time']}_\n"
        return lines

    # สำรอง: สูตรประมาณจากราคาโลก
    rate = get_usd_thb_rate()
    if rate:
        thai_baht = current * rate * THAI_GOLD_FACTOR
        return (
            f"🏅 **เทียบทองไทยบาทละ (ประมาณจากราคาโลก):** `{thai_baht:,.0f} THB`\n"
            f"💱 **เรทที่ใช้:** `{rate:.2f} THB/USD`\n"
        )
    return "🇹🇭 _ดึงราคาฝั่งไทยไม่ได้ในรอบนี้_\n"


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
        f"💰 **ราคาทองโลก:** `{current:,.2f} USD` "
        f"(เปลี่ยนแปลง `{change:+,.2f}` จากฐาน `{base:,.2f}`)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n\n"
        f"_ราคาโลกจาก PAXG | ราคาไทยจากฮั่วเซ่งเฮง | GitHub Actions ☁️_"
    )
    post_discord(content)


def send_reset_notice(current, old_base, reset_hour):
    ts = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
    diff = current - old_base
    mention = "" if reset_hour in QUIET_HOURS else "@everyone\n"
    next_hour = RESET_HOURS[(RESET_HOURS.index(reset_hour) + 1) % len(RESET_HOURS)]

    content = (
        f"{mention}"
        f"## 🔄 ปรับฐานราคาตามเวลา {reset_hour:02d}:00 น.\n"
        f"💰 **ราคาทองโลก (ฐานใหม่):** `{current:,.2f} USD` "
        f"(ขยับ `{diff:+,.2f}` ในรอบที่ผ่านมา)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n\n"
        f"_ระบบทำงานปกติ ✅ | รอบถัดไป: {next_hour:02d}:00 น._"
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

    if state is None or "base_price" not in state:
        save_state(current, boundary.isoformat())
        print(f"[INFO] รอบแรก จดราคาฐาน = {current:,.2f} USD")
        post_discord(
            f"✅ **Gold Watcher เริ่มทำงานแล้ว!**\n"
            f"ราคาฐานตั้งต้น: `{current:,.2f} USD` | Threshold: `±{PRICE_THRESHOLD_USD} USD`\n"
            f"แสดงราคารับซื้อ/ขายออกจริงจากฮั่วเซ่งเฮงในทุกการแจ้งเตือน"
        )
        return

    base = state["base_price"]
    last_reset_str = state.get("last_reset", "")

    need_reset = True
    if last_reset_str:
        try:
            last_reset = datetime.fromisoformat(last_reset_str)
            need_reset = last_reset < boundary
        except Exception:
            need_reset = True

    diff = current - base
    print(f"[INFO] ปัจจุบัน {current:,.2f} | ฐาน {base:,.2f} | ต่าง {diff:+,.2f} USD")

    if abs(diff) >= PRICE_THRESHOLD_USD:
        print("[ALERT] เกิน Threshold -> แจ้งเตือน!")
        send_alert(current, diff, base)
        save_state(current, boundary.isoformat())
        return

    if need_reset:
        print(f"[RESET] ปรับฐานราคาตามเวลา ({boundary.strftime('%H:%M')} น.) "
              f"{base:,.2f} -> {current:,.2f} USD")
        save_state(current, boundary.isoformat())
        send_reset_notice(current, base, boundary.hour)
        return

    print("[INFO] ยังไม่เกิน Threshold และยังไม่ถึงรอบปรับฐาน")


if __name__ == "__main__":
    main()
