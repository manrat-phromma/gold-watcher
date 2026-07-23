"""Gold Price Watcher - Render Loop + สถิติ + กราฟ 24 ชม."""
import os
import io
import time
import json
import base64
import threading
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
matplotlib.rcParams["axes.unicode_minus"] = False
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT"
BINANCE_KLINES_URL = ("https://api.binance.com/api/v3/klines"
                      "?symbol=PAXGUSDT&interval=5m&limit=288")
COINBASE_API_URL = "https://api.coinbase.com/v2/prices/PAXG-USD/spot"
KRAKEN_API_URL = "https://api.kraken.com/0/public/Ticker?pair=PAXGUSD"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC?pair=PAXGUSD&interval=5"
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"
HSH_API_URL = "https://apicheckprice.huasengheng.com/api/values/getprice/"

PRICE_THRESHOLD_USD = 5.0
THAI_GOLD_FACTOR = 0.4729
CHECK_INTERVAL_SECONDS = 300
HISTORY_DAYS = 7
EVENT_KEEP_DAYS = 3

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
STATE_PATH = "state.json"
HISTORY_PATH = "history.json"
EVENTS_PATH = "events.json"

RESET_HOURS = [0, 6, 12, 18]
QUIET_HOURS = [0, 6]
TZ_TH = timezone(timedelta(hours=7))

STATE = {"base_price": None, "last_reset": None, "sha": None}
HISTORY = {"points": [], "sha": None}
EVENTS = {"items": [], "sha": None}   # [iso_time, price, kind]  kind: up/down/reset


# ==================== WEB SERVER ====================
class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        now = datetime.now(TZ_TH).strftime("%Y-%m-%d %H:%M:%S")
        msg = (f"Gold Watcher OK\nBase: {STATE.get('base_price')}\n"
               f"History points: {len(HISTORY['points'])}\n"
               f"Events: {len(EVENTS['items'])}\nTime(TH): {now}\n")
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
    ev, esha = gh_load(EVENTS_PATH)
    if ev:
        EVENTS["items"] = ev.get("items", [])
        EVENTS["sha"] = esha
        print(f"[OK] โหลดเหตุการณ์ {len(EVENTS['items'])} รายการ")


def save_state():
    STATE["sha"] = gh_save(STATE_PATH, {
        "base_price": STATE["base_price"],
        "last_reset": STATE["last_reset"].isoformat() if STATE["last_reset"] else None,
    }, STATE["sha"], "update base price")


def save_history():
    HISTORY["sha"] = gh_save(HISTORY_PATH, {"points": HISTORY["points"]},
                             HISTORY["sha"], "update price history")


def save_events():
    EVENTS["sha"] = gh_save(EVENTS_PATH, {"items": EVENTS["items"]},
                            EVENTS["sha"], "update events")


def add_event(now_th, price, kind):
    EVENTS["items"].append([now_th.isoformat(), round(price, 2), kind])
    cutoff = now_th - timedelta(days=EVENT_KEEP_DAYS)
    EVENTS["items"] = [e for e in EVENTS["items"]
                       if datetime.fromisoformat(e[0]) >= cutoff]


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


def get_klines_24h():
    """ดึงแท่งเทียน 5 นาที ย้อนหลัง 24 ชม. -> [(เวลาไทย, ราคาปิด), ...]"""
    try:
        r = requests.get(BINANCE_KLINES_URL, timeout=15)
        r.raise_for_status()
        out = []
        for k in r.json():
            t = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).astimezone(TZ_TH)
            out.append((t, float(k[4])))
        if out:
            print(f"[OK] ดึงแท่งเทียนจาก Binance {len(out)} แท่ง")
            return out
    except Exception as e:
        print(f"[WARN] Binance klines ไม่ได้ ({e}) ลอง Kraken...")

    try:
        r = requests.get(KRAKEN_OHLC_URL, timeout=15)
        r.raise_for_status()
        result = r.json()["result"]
        key = next(k for k in result if k != "last")
        out = []
        for k in result[key][-288:]:
            t = datetime.fromtimestamp(int(k[0]), tz=timezone.utc).astimezone(TZ_TH)
            out.append((t, float(k[4])))
        if out:
            print(f"[OK] ดึงแท่งเทียนจาก Kraken {len(out)} แท่ง")
            return out
    except Exception as e:
        print(f"[WARN] Kraken OHLC ไม่ได้: {e}")
    return []


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


# ==================== วาดกราฟ ====================
def _draw_chart(times, prices, current, now_th, title, show_events_from):
    """วาดกราฟ 1 ใบ -> bytes PNG"""
    if len(times) < 5:
        return None
    try:
        avg = sum(prices) / len(prices)
        sd = (sum((p - avg) ** 2 for p in prices) / len(prices)) ** 0.5

        fig, ax = plt.subplots(figsize=(11, 5.5), dpi=110)
        fig.patch.set_facecolor("#2b2d31")
        ax.set_facecolor("#1e1f22")

        ax.axhspan(avg - sd, avg + sd, color="#5865f2", alpha=0.13,
                   label=f"Normal zone {avg-sd:,.0f}-{avg+sd:,.0f}")
        ax.axhline(avg, color="#5865f2", ls="--", lw=1.2, alpha=0.85,
                   label=f"Average {avg:,.2f}")
        ax.plot(times, prices, color="#f0b232", lw=1.9, label="PAXG/USD (5m)")

        base = STATE.get("base_price")
        if base:
            ax.axhline(base, color="#43b581", lw=1.5, alpha=0.9,
                       label=f"Base {base:,.2f}")
            ax.axhspan(base - PRICE_THRESHOLD_USD, base + PRICE_THRESHOLD_USD,
                       color="#43b581", alpha=0.08)

        seen = set()
        for iso, ep, kind in EVENTS["items"]:
            et = datetime.fromisoformat(iso)
            if et < show_events_from or et > now_th:
                continue
            style = {"up": ("^", "#43b581", "Alert UP"),
                     "down": ("v", "#ed4245", "Alert DOWN"),
                     "reset": ("o", "#faa61a", "Time reset")}.get(kind)
            if not style:
                continue
            marker, color, lbl = style
            ax.scatter([et], [ep], marker=marker, s=110, color=color,
                       edgecolors="white", linewidths=0.8, zorder=5,
                       label=lbl if lbl not in seen else None)
            seen.add(lbl)
            if kind == "reset":
                ax.axvline(et, color="#faa61a", ls=":", lw=1.0, alpha=0.55)

        ax.scatter([now_th], [current], marker="*", s=300, color="#ffffff",
                   edgecolors="#f0b232", linewidths=1.5, zorder=6,
                   label=f"Now {current:,.2f}")

        ax.set_title(title, color="white", fontsize=13, pad=12)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ_TH))
        ax.tick_params(colors="#b5bac1", labelsize=10)
        for s in ax.spines.values():
            s.set_color("#3f4147")
        ax.grid(color="#3f4147", ls="--", lw=0.5, alpha=0.6)
        ax.set_ylabel("USD / oz", color="#b5bac1", fontsize=10)
        leg = ax.legend(loc="best", fontsize=8.5, framealpha=0.9,
                        facecolor="#2b2d31", edgecolor="#3f4147", ncol=2)
        for t in leg.get_texts():
            t.set_color("#dbdee1")

        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f"[WARN] วาดกราฟไม่สำเร็จ: {e}")
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def make_charts(current, now_th):
    """คืนค่า (กราฟ 24 ชม., กราฟซูม 6 ชม.ของวันนี้)"""
    candles = get_klines_24h()
    if len(candles) < 10:
        return None, None

    times = [c[0] for c in candles]
    prices = [c[1] for c in candles]

    chart24 = _draw_chart(
        times, prices, current, now_th,
        f"Gold (PAXG/USD) 24h  |  {now_th.strftime('%d %b %Y %H:%M')} TH",
        now_th - timedelta(hours=24))

    # ช่วง 6 ชม.ของวันปัจจุบัน: 00-06 / 06-12 / 12-18 / 18-24
    block = (now_th.hour // 6) * 6
    start = now_th.replace(hour=block, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=6)

    zt, zp = [], []
    for t, p in zip(times, prices):
        if start <= t <= end:
            zt.append(t)
            zp.append(p)

    chart6 = None
    if len(zt) >= 5:
        chart6 = _draw_chart(
            zt, zp, current, now_th,
            f"Gold Zoom {block:02d}:00-{(block+6) % 24:02d}:00  |  "
            f"{now_th.strftime('%d %b %Y')} TH",
            start)
        if chart6:
            print(f"[OK] กราฟซูมช่วง {block:02d}:00-{block+6:02d}:00")
    return chart24, chart6

# ==================== สถิติ ====================
def prices_within(now_th, hours):
    cutoff = now_th - timedelta(hours=hours)
    return [p[1] for p in HISTORY["points"]
            if datetime.fromisoformat(p[0]) >= cutoff]


def price_at_hours_ago(now_th, hours):
    target = now_th - timedelta(hours=hours)
    best, best_time, best_gap = None, None, None
    for iso, price in HISTORY["points"]:
        dt = datetime.fromisoformat(iso)
        gap = abs((dt - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_time, best_gap = price, dt, gap
    if best_gap is not None and best_gap <= 2700:
        return best, best_time
    return None, None


def build_stats_message(current, now_th):
    pts = HISTORY["points"]
    if len(pts) < 3:
        return (f"📊 **บทวิเคราะห์เชิงสถิติ**\n"
                f"_กำลังเก็บข้อมูลราคา ({len(pts)} จุด) — "
                f"สถิติจะสมบูรณ์ขึ้นเรื่อยๆ เมื่อระบบทำงานครบ 24 ชม._")

    lines = [f"📊 **บทวิเคราะห์เชิงสถิติ** _(คำนวณจากข้อมูลจริง {len(pts)} จุด)_\n"]

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

    d7 = prices_within(now_th, 24 * 7)
    if len(d7) >= 10 and len(d7) > len(d1):
        lines.append(f"🗓️ **กรอบ 7 วัน:** `{min(d7):,.2f}` – `{max(d7):,.2f} USD`\n")

    dirs = []
    for label, h in [("1 ชม.", 1), ("6 ชม.", 6), ("24 ชม.", 24)]:
        old, old_time = price_at_hours_ago(now_th, h)
        if old:
            ch = current - old
            pct = ch / old * 100
            arrow = "🟢▲" if ch > 0 else "🔴▼" if ch < 0 else "⚪️="
            dirs.append(
                f"　{arrow} **{label}:** `{ch:+,.2f} USD` (`{pct:+.2f}%`)\n"
                f"　　_เทียบกับ `{old:,.2f} USD` เมื่อ {old_time.strftime('%H:%M')} น._"
            )
    if dirs:
        lines.append("🧭 **ทิศทางย้อนหลัง**\n" + "\n".join(dirs) + "\n")

    h1 = prices_within(now_th, 1)
    if len(h1) >= 2 and len(d1) >= 6:
        range_1h = max(h1) - min(h1)
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

    hsh = get_hsh_prices()
    if hsh:
        spread = hsh["HSH"]["sell"] - hsh["HSH"]["buy"]
        line = f"💰 **ส่วนต่างซื้อ-ขาย ฮั่วเซ่งเฮง:** `{spread:,.0f} THB`/บาททอง\n"
        if "REF" in hsh:
            gap_buy = hsh["HSH"]["buy"] - hsh["REF"]["buy"]
            line += (f"　เทียบสมาคมฯ: รับซื้อ{'สูงกว่า' if gap_buy >= 0 else 'ต่ำกว่า'} "
                     f"`{abs(gap_buy):,.0f} THB`\n")
        lines.append(line)

    lines.append("\n_ตัวเลขทั้งหมดคำนวณจากราคาที่บันทึกไว้จริง "
                 "ไม่ใช่การทำนายหรือคำแนะนำการลงทุน_")
    return "".join(lines)


# ==================== DISCORD ====================
def post_discord(content, image_bytes=None):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        if image_bytes:
            files = {"file": ("gold_chart.png", image_bytes, "image/png")}
            data = {"payload_json": json.dumps(
                {"content": content, "username": "Gold Price Watcher 🥇"})}
            r = requests.post(DISCORD_WEBHOOK_URL, data=data, files=files, timeout=25)
        else:
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
    time.sleep(1)
    c24, c6 = make_charts(current, now_th)
    post_discord(build_stats_message(current, now_th), c24)
    if c6:
        time.sleep(1)
        post_discord("🔍 **ซูมช่วง 6 ชั่วโมงของวันนี้**", c6)


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
    c24, c6 = make_charts(current, now_th)
    post_discord(build_stats_message(current, now_th), c24)
    if c6:
        time.sleep(1)
        post_discord("🔍 **ซูมช่วง 6 ชั่วโมงของวันนี้**", c6)


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
        add_event(now_th, current, "up" if diff > 0 else "down")
        send_alert(current, diff, base, now_th)
        triggered = True
    elif need_reset:
        add_event(now_th, current, "reset")
        send_reset_notice(current, base, boundary.hour, now_th)
        triggered = True

    if triggered:
        STATE["base_price"] = current
        STATE["last_reset"] = boundary
        save_state()
        save_history()
        save_events()
    elif len(HISTORY["points"]) % 6 == 0:
        save_history()


if __name__ == "__main__":
    threading.Thread(target=start_web_server, daemon=True).start()
    print("🚀 Gold Watcher (Loop + Stats + Chart) เริ่มทำงาน")
    load_all()
    while True:
        try:
            check_once()
        except Exception as e:
            print(f"[ERROR] ลูปพังชั่วคราว: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)
