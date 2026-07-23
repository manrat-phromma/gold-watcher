"""Gold Price Watcher - Render Loop + สถิติ (2 ข้อความต่อกัน)"""
import os
import time
import json
import base64
import threading
import requests
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
COINBASE_API_URL = "https://api.coinbase.com/v2/prices/PAXG-USD/spot"
KRAKEN_API_URL = "https://api.kraken.com/0/public/Ticker?pair=PAXGUSD"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
HSH_API_URL = "https://apicheckprice.huasengheng.com/api/values/getprice/"

PRICE_THRESHOLD_USD = 5.0
THAI_GOLD_FACTOR = 0.4729
CHECK_INTERVAL_SECONDS = 300
HISTORY_DAYS = 7                    # เก็บประวัติราคากี่วัน

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
STATE_PATH = "state.json"
HISTORY_PATH = "history.json"

RESET_HOURS = [0, 6, 12, 18]
QUIET_HOURS = [0, 6]
TZ_TH = timezone(timedelta(hours=7))

STATE = {"base_price": None, "last_reset": None, "sha": None}
HISTORY = {"points": [], "sha": None}   # points = [[iso_time, price], ...]


# ==================== WEB SERVER ====================
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        now = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
        msg = (f"Gold Watcher OK\nBase: {STATE.get('base_price')}\n"
               f"History points: {len(HISTORY['points'])}\nTime(TH): {now}\n")
        self.wfile.write(msg.encode("utf-8"))

    def log_message(self, *args):
        pass


def start_web_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))),
               StatusHandler).serve_forever()


# ==================== GITHUB STORAGE ====================
def gh_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"}


def gh_load(path):
    """คืนค่า (data, sha) หรือ (None, None)"""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return None, None
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        r = requests.get(url, headers=gh_headers(), timeout=15)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        d = r.json()
        return json.loads(base64.b64decode(d["content"]).decode("utf-8")), d["sha"]
    except Exception as e:
        print(f"[WARN] โหลด {path} ไม่ได้: {e}")
        return None, None


def gh_save(path, obj, sha, message):
    if not (GITHUB_TOKEN and GITHUB_REPO):
        return sha
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        payload = {"message": message,
                   "content": base64.b64encode(
                       json.dumps(obj).encode("utf-8")).decode("utf-8")}
        if sha:
            payload["sha"] = sha
        r = requests.put(url, headers=gh_headers(), json=payload, timeout=15)
        r.raise_for_status()
        return r.json()["content"]["sha"]
    except Exception as e:
        print(f"[WARN] บันทึก {path} ไม่สำเร็จ: {e}")
        return sha


def load_all():
    data, sha = gh_load(STATE_PATH)
    if data:
        STATE["base_price"] = data.get("base_price")
        lr = data.get("last_reset")
        STATE["last_reset"] = datetime.fromisoformat(lr) if lr else None
        STATE["sha"] = sha
        print(f"[OK] โหลดฐาน: {STATE['base_price']}")
    hist, hsha = gh_load(HISTORY_PATH)
    if hist:
        HISTORY["points"] = hist.get("points", [])
        HISTORY["sha"] = hsha
        print(f"[OK] โหลดประวัติ {len(HISTORY['points'])} จุด")


def save_state():
    STATE["sha"] = gh_save(STATE_PATH, {
        "base_price": STATE["base_price"],
        "last_reset": STATE["last_reset"].isoformat() if STATE["last_reset"] else None,
    }, STATE["sha"], "update base price")


def save_history():
    HISTORY["sha"] = gh_save(HISTORY_PATH, {"points": HISTORY["points"]},
                             HISTORY["sha"], "update price history")


def add_history_point(price, now_th):
    HISTORY["points"].append([now_th.isoformat(), round(price, 2)])
    cutoff = now_th - timedelta(days=HISTORY_DAYS)
    HISTORY["points"] = [p for p in HISTORY["points"]
                         if datetime.fromisoformat(p[0]) >= cutoff]


# ==================== ดึงราคา ====================
def get_gold_price():
    for name, url, ex in [
        ("Binance", BINANCE_API_URL, lambda j: float(j["price"])),
        ("Coinbase", COINBASE_API_URL, lambda j: float(j["data"]["amount"])),
        ("Kraken", KRAKEN_API_URL,
         lambda j: float(j["result"][next(iter(j["result"]))]["c"][0])),
    ]:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return ex(r.json())
        except Exception:
            continue
    print("[ERROR] ดึงราคาไม่ได้เลย")
    return None


def get_hsh_prices():
    try:
        r = requests.get(HSH_API_URL, timeout=10)
        r.raise_for_status()
        out = {}
        for item in r.json():
            g = item.get("GoldType")
            if g in ("HSH", "REF"):
                out[g] = {"buy": float(str(item["Buy"]).replace(",", "")),
                          "sell": float(str(item["Sell"]).replace(",", "")),
                          "time": item.get("StrTimeUpdate", "")}
        return out if "HSH" in out else None
    except Exception:
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
                f"`{current * rate * THAI_GOLD_FACTOR:,.0f} THB`\n")
    return "🇹🇭 _ดึงราคาฝั่งไทยไม่ได้ในรอบนี้_\n"


# ==================== สถิติ (ข้อความชุดที่ 2) ====================
def prices_within(now_th, hours):
    cutoff = now_th - timedelta(hours=hours)
    return [p[1] for p in HISTORY["points"]
            if datetime.fromisoformat(p[0]) >= cutoff]


def price_at_hours_ago(now_th, hours):
    """หาราคาที่ใกล้เคียงจุดเวลา X ชม.ที่แล้วที่สุด"""
    target = now_th - timedelta(hours=hours)
    best, best_gap = None, None
    for iso, price in HISTORY["points"]:
        gap = abs((datetime.fromisoformat(iso) - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = price, gap
    # ยอมรับได้ถ้าห่างจากเป้าไม่เกิน 45 นาที
    return best if best_gap is not None and best_gap <= 2700 else None


def build_stats_message(current, now_th):
    """สร้างข้อความสถิติ - คำนวณจากข้อมูลจริงล้วน ไม่มีการทำนาย"""
    pts = HISTORY["points"]
    if len(pts) < 3:
        return (f"📊 **บทวิเคราะห์เชิงสถิติ**\n"
                f"_กำลังเก็บข้อมูลราคา ({len(pts)} จุด) — "
                f"สถิติจะสมบูรณ์ขึ้นเรื่อยๆ เมื่อระบบทำงานครบ 24 ชม._")

    lines = [f"📊 **บทวิเคราะห์เชิงสถิติ** _(คำนวณจากข้อมูลจริง {len(pts)} จุด)_\n"]

    # กรอบราคา 24 ชม.
    d1 = prices_within(now_th, 24)
    if len(d1) >= 3:
        lo, hi, avg = min(d1), max(d1), sum(d1) / len(d1)
        pos = (current - lo) / (hi - lo) * 100 if hi > lo else 50
        lines.append(
            f"📈 **กรอบ 24 ชม.:** `{lo:,.2f}` – `{hi:,.2f} USD` "
            f"(กว้าง `{hi - lo:,.2f}`)\n"
            f"　ตอนนี้อยู่ที่ **{pos:.0f}%** ของกรอบ "
            f"{'(ใกล้ยอดบน)' if pos >= 75 else '(ใกล้ยอดล่าง)' if pos <= 25 else '(กลางกรอบ)'}\n"
            f"〰️ **เฉลี่ย 24 ชม.:** `{avg:,.2f} USD` "
            f"(ตอนนี้ {'สูงกว่า' if current >= avg else 'ต่ำกว่า'} "
            f"`{abs(current - avg):,.2f}`)\n"
        )

    # กรอบ 7 วัน
    d7 = prices_within(now_th, 24 * 7)
    if len(d7) >= 10 and len(d7) > len(d1):
        lines.append(f"🗓️ **กรอบ 7 วัน:** `{min(d7):,.2f}` – `{max(d7):,.2f} USD`\n")

    # ทิศทางย้อนหลัง
    dirs = []
    for label, h in [("1 ชม.", 1), ("6 ชม.", 6), ("24 ชม.", 24)]:
        old = price_at_hours_ago(now_th, h)
        if old:
            ch = current - old
            pct = ch / old * 100
            arrow = "🟢▲" if ch > 0 else "🔴▼" if ch < 0 else "⚪️="
            dirs.append(f"　{arrow} **{label}:** `{ch:+,.2f} USD` (`{pct:+.2f}%`)")
    if dirs:
        lines.append("🧭 **ทิศทางย้อนหลัง**\n" + "\n".join(dirs) + "\n")

    # ความผันผวน
    h1 = prices_within(now_th, 1)
    if len(h1) >= 2 and len(d1) >= 6:
        range_1h = max(h1) - min(h1)
        # ค่าเฉลี่ยช่วงกว้างรายชั่วโมงของ 24 ชม.
        hourly_ranges = []
        for i in range(24):
            seg = [p[1] for p in HISTORY["points"]
                   if now_th - timedelta(hours=i + 1) <=
                   datetime.fromisoformat(p[0]) < now_th - timedelta(hours=i)]
            if len(seg) >= 2:
                hourly_ranges.append(max(seg) - min(seg))
        if hourly_ranges:
            avg_range = sum(hourly_ranges) / len(hourly_ranges)
            ratio = range_1h / avg_range if avg_range > 0 else 1
            note = ("ผันผวนสูงกว่าปกติมาก" if ratio >= 2 else
                    "ผันผวนสูงกว่าปกติ" if ratio >= 1.3 else
                    "ผันผวนต่ำกว่าปกติ" if ratio <= 0.6 else "ผันผวนระดับปกติ")
            lines.append(
                f"⚡ **ความผันผวน 1 ชม.ล่าสุด:** ช่วงกว้าง `{range_1h:,.2f} USD` "
                f"| เฉลี่ยรายชั่วโมง `{avg_range:,.2f}` → **{note}**\n")

    # ส่วนต่างราคาไทย
    hsh = get_hsh_prices()
    if hsh:
        spread = hsh["HSH"]["sell"] - hsh["HSH"]["buy"]
        line = (f"💰 **ส่วนต่างซื้อ-ขาย ฮั่วเซ่งเฮง:** `{spread:,.0f} THB`/บาททอง\n")
        if "REF" in hsh:
            gap_buy = hsh["HSH"]["buy"] - hsh["REF"]["buy"]
            line += (f"　เทียบสมาคมฯ: รับซื้อ{'สูงกว่า' if gap_buy >= 0 else 'ต่ำกว่า'} "
                     f"`{abs(gap_buy):,.0f} THB`\n")
        lines.append(line)

    lines.append("\n_ตัวเลขทั้งหมดคำนวณจากราคาที่บันทึกไว้จริง "
                 "ไม่ใช่การทำนายหรือคำแนะนำการลงทุน_")
    return "".join(lines)


# ==================== DISCORD ====================
def post_discord(content):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL,
                          json={"content": content,
                                "username": "Gold Price Watcher 🥇"}, timeout=10)
        r.raise_for_status()
        print("[OK] ส่ง Discord สำเร็จ")
    except Exception as e:
        print(f"[ERROR] ส่ง Discord ไม่สำเร็จ: {e}")


def send_alert(current, change, base, now_th):
    emoji = "🚀📈" if change > 0 else "🔻📉"
    ts = now_th.strftime("%Y-%m-%d %H:%M:%S")
    post_discord(
        f"@everyone\n"
        f"## {emoji} แจ้งเตือนราคาทองคำ (Gold Price Alert)\n"
        f"> 🔔 **ราคาทองคำเปลี่ยนแปลงเกินเกณฑ์ที่ตั้งไว้!**\n\n"
        f"💰 **ราคาทองโลก:** `{current:,.2f} USD` "
        f"(เปลี่ยนแปลง `{change:+,.2f}` จากฐาน `{base:,.2f}`)\n"
        f"{build_thb_section(current)}"
        f"⏰ **เวลา (ไทย):** `{ts}`\n"
    )
    time.sleep(1)   # เว้นจังหวะให้ข้อความเรียงถูกลำดับ
    post_discord(build_stats_message(current, now_th))


def send_reset_notice(current, old_base, hour, now_th):
    ts = now_th.strftime("%Y-%m-%d %H:%M:%S")
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
    time.sleep(1)
    post_discord(build_stats_message(current, now_th))


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

    add_history_point(current, now_th)
    boundary = latest_reset_boundary(now_th)

    if STATE["base_price"] is None:
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state()
        save_history()
        post_discord(
            f"✅ **Gold Watcher เริ่มทำงานแล้ว**\n"
            f"ราคาฐานตั้งต้น: `{current:,.2f} USD` | Threshold: `±{PRICE_THRESHOLD_USD} USD`"
        )
        return

    base = STATE["base_price"]
    diff = current - base
    need_reset = STATE["last_reset"] is None or STATE["last_reset"] < boundary
    print(f"[{now_th.strftime('%H:%M:%S')}] {current:,.2f} | ฐาน {base:,.2f} "
          f"| ต่าง {diff:+,.2f} | ประวัติ {len(HISTORY['points'])} จุด")

    triggered = False
    if abs(diff) >= PRICE_THRESHOLD_USD:
        send_alert(current, diff, base, now_th)
        triggered = True
    elif need_reset:
        send_reset_notice(current, base, boundary.hour, now_th)
        triggered = True

    if triggered:
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state()
        save_history()
    elif len(HISTORY["points"]) % 6 == 0:
        # บันทึกประวัติทุกๆ ~30 นาที เพื่อไม่ให้เขียน GitHub ถี่เกินไป
        save_history()


if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()
    print("🚀 Gold Watcher (Render Loop + Stats) เริ่มทำงาน")
    load_all()
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[ERROR] ลูปพังชั่วคราว: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
