import os
import time
from datetime import datetime, timezone
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME", "").strip()

GATE_BASE = "https://api.gateio.ws/api/v4"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CryptoRadarScanner/1.0"})

MIN_24H_VOL_USD = 5_000_000
MAX_CANDIDATES = 45
VOL_SURGE_THRESHOLD = 2.0
OI_CHANGE_THRESHOLD = 1.5
TOP_ALERTS_LIMIT = 5

EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "USDE", "DAI", "BUSD", "USDP", "PYUSD", "EUR"
}


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def gate_get(path, params=None, timeout=12):
    url = f"{GATE_BASE}{path}"
    try:
        resp = SESSION.get(url, params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def describe_cvd_state(imbalance: float, market_type: str) -> str:
    if market_type == "perp":
        if imbalance <= -0.08:
            return "🔻 فروش هیجانی اهرمی"
        if imbalance <= -0.03:
            return "🔽 فروش ملایم فیوچرز"
        if imbalance >= 0.08:
            return "🟢 خرید هیجانی اهرمی"
        if imbalance >= 0.03:
            return "🔼 خرید ملایم فیوچرز"
        return "⚪ متعادل"
    else:
        if imbalance >= 0.05:
            return "🟢 خرید واقعی اسپات"
        if imbalance <= -0.05:
            return "🔻 فروش واقعی اسپات"
        return "⚪ متعادل"


def get_perp_cvd(contract: str, quanto_mult: float, lookback_sec: int = 900):
    data = gate_get(
        "/futures/usdt/trades",
        params={"contract": contract, "limit": 1000},
    )
    if not isinstance(data, list) or not data:
        return {"available": False, "cvd": 0.0, "vol": 0.0, "imbalance": 0.0, "source": "N/A"}

    cutoff = time.time() - lookback_sec
    buy_vol = 0.0
    sell_vol = 0.0

    try:
        for tr in data:
            ts = safe_float(tr.get("create_time"))
            if ts < cutoff:
                continue
            size = safe_float(tr.get("size"))
            price = safe_float(tr.get("price"))
            usd_val = abs(size) * price * quanto_mult
            if size > 0:
                buy_vol += usd_val
            elif size < 0:
                sell_vol += usd_val

        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            cvd = buy_vol - sell_vol
            return {
                "available": True,
                "cvd": cvd,
                "vol": total_vol,
                "imbalance": cvd / total_vol,
                "source": "Gate",
            }
    except Exception:
        pass

    return {"available": False, "cvd": 0.0, "vol": 0.0, "imbalance": 0.0, "source": "N/A"}


def get_spot_cvd(pair: str, lookback_sec: int = 900):
    data = gate_get(
        "/spot/trades",
        params={"currency_pair": pair, "limit": 1000},
    )
    if not isinstance(data, list) or not data:
        return {"available": False, "cvd": 0.0, "vol": 0.0, "imbalance": 0.0, "source": "N/A"}

    cutoff = time.time() - lookback_sec
    buy_vol = 0.0
    sell_vol = 0.0

    try:
        for tr in data:
            ts = safe_float(tr.get("create_time"))
            if ts < cutoff:
                continue
            usd_val = safe_float(tr.get("amount")) * safe_float(tr.get("price"))
            if tr.get("side") == "buy":
                buy_vol += usd_val
            else:
                sell_vol += usd_val

        total_vol = buy_vol + sell_vol
        if total_vol > 0:
            cvd = buy_vol - sell_vol
            return {
                "available": True,
                "cvd": cvd,
                "vol": total_vol,
                "imbalance": cvd / total_vol,
                "source": "Gate",
            }
    except Exception:
        pass

    return {"available": False, "cvd": 0.0, "vol": 0.0, "imbalance": 0.0, "source": "N/A"}


def get_oi_metrics(contract: str):
    data = gate_get(
        "/futures/usdt/contract_stats",
        params={"contract": contract, "interval": "15m", "limit": 8},
    )
    if not isinstance(data, list) or len(data) < 2:
        return {"available": False, "oi_usd": 0.0, "oi_15m_pct": 0.0, "oi_1h_pct": 0.0}

    data.sort(key=lambda x: safe_float(x.get("time")))

    def extract_oi(item):
        return safe_float(item.get("open_interest_usd")) or safe_float(item.get("open_interest"))

    oi_now = extract_oi(data[-1])
    oi_prev_15m = extract_oi(data[-2])
    oi_prev_1h = extract_oi(data[-5] if len(data) >= 5 else data[0])

    oi_15m_pct = ((oi_now - oi_prev_15m) / oi_prev_15m * 100.0) if oi_prev_15m > 0 else 0.0
    oi_1h_pct = ((oi_now - oi_prev_1h) / oi_prev_1h * 100.0) if oi_prev_1h > 0 else 0.0

    return {
        "available": oi_now > 0,
        "oi_usd": safe_float(data[-1].get("open_interest_usd")),
        "oi_15m_pct": oi_15m_pct,
        "oi_1h_pct": oi_1h_pct,
    }


def analyze_candles(contract: str):
    data = gate_get(
        "/futures/usdt/candlesticks",
        params={"contract": contract, "interval": "15m", "limit": 24},
    )
    if not isinstance(data, list) or len(data) < 12:
        return None

    data.sort(key=lambda x: safe_float(x.get("t")))

    def candle_vol(k):
        s = safe_float(k.get("sum"))
        return s if s > 0 else safe_float(k.get("v")) * safe_float(k.get("c"))

    base_candles = data[:-2]
    avg_vol = sum(candle_vol(k) for k in base_candles) / max(len(base_candles), 1)
    if avg_vol <= 0:
        return None

    last_closed = data[-2]
    current = data[-1]

    ratio_closed = candle_vol(last_closed) / avg_vol
    ratio_current = candle_vol(current) / avg_vol
    vol_ratio = max(ratio_closed, ratio_current)

    ref_candle = current if ratio_current >= ratio_closed else last_closed
    open_p = safe_float(ref_candle.get("o"))
    close_p = safe_float(current.get("c"))
    pct_15m = ((close_p - open_p) / open_p * 100.0) if open_p > 0 else 0.0

    prev_high = max(safe_float(k.get("h")) for k in base_candles)
    prev_low = min(safe_float(k.get("l")) for k in base_candles)

    breakout_status = "داخل رنج"
    if close_p > prev_high:
        breakout_status = "🚀 شکست سقف ۵ ساعته"
    elif close_p < prev_low:
        breakout_status = "🔻 شکست کف ۵ ساعته"
    elif prev_high > 0 and (prev_high - close_p) / prev_high <= 0.006:
        breakout_status = "⚡ نزدیک سقف (آستانه بریک‌اوت)"
    elif prev_low > 0 and (close_p - prev_low) / prev_low <= 0.006:
        breakout_status = "⚠️ نزدیک کف حمایتی"

    return {
        "vol_ratio": vol_ratio,
        "pct_15m": pct_15m,
        "close": close_p,
        "breakout_status": breakout_status,
    }


def classify_setup(pct_15m, vol_ratio, oi_15m, funding_pct, basis_pct, spot_cvd, perp_cvd):
    score = 0
    tags = []

    if vol_ratio >= 3.0:
        score += 3
    elif vol_ratio >= 2.0:
        score += 2
    else:
        score += 1

    if abs(oi_15m) >= 3.0:
        score += 2
    elif abs(oi_15m) >= 1.5:
        score += 1

    spot_imb = spot_cvd["imbalance"] if spot_cvd["available"] else 0.0
    perp_imb = perp_cvd["imbalance"] if perp_cvd["available"] else 0.0

    if funding_pct <= -0.02 and pct_15m > 0.5 and oi_15m > 0.8:
        tags.append("🔥 احتمال شورت اسکوئیز")
        score += 2
    elif pct_15m > 0.8 and oi_15m > 1.0 and spot_imb >= 0.04:
        tags.append("🚀 ورود پول واقعی و صعودی")
        score += 2
    elif pct_15m < -0.8 and oi_15m > 1.0 and perp_imb <= -0.05:
        tags.append("📉 فشار فروش و ریزش سنگین")
        score += 2
    elif abs(basis_pct) >= 0.35:
        tags.append("⚖️ شکاف قیمتی اسپات/فیوچرز")
        score += 1

    if not tags:
        if pct_15m >= 0:
            tags.append("📈 جهش حجم و تحرک صعودی")
        else:
            tags.append("🔻 جهش حجم و تحرک نزولی")

    return score, " | ".join(tags)


def fmt_usd(val: float) -> str:
    abs_v = abs(val)
    sign = "-" if val < 0 else ""
    if abs_v >= 1_000_000_000:
        return f"{sign}${abs_v / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"{sign}${abs_v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"{sign}${abs_v / 1_000:.1f}K"
    return f"{sign}${abs_v:.0f}"


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN یا TELEGRAM_CHAT_ID تنظیم نشده است.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = SESSION.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return True
        print(f"Telegram API error {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"Telegram send exception: {exc}")
    return False


def run_scan():
    tickers = gate_get("/futures/usdt/tickers")
    contracts_meta = gate_get("/futures/usdt/contracts")
    spot_tickers = gate_get("/spot/tickers")

    if not isinstance(tickers, list):
        print("دریافت لیست ارزهای فیوچرز ناموفق بود.")
        return

    multipliers = {}
    if isinstance(contracts_meta, list):
        for c in contracts_meta:
            name = c.get("name")
            if name:
                multipliers[name] = safe_float(c.get("quanto_multiplier"), 1.0)

    spot_prices = {}
    if isinstance(spot_tickers, list):
        for st in spot_tickers:
            pair = st.get("currency_pair")
            if pair:
                spot_prices[pair] = safe_float(st.get("last"))

    liquid_tickers = []
    for t in tickers:
        contract = t.get("contract", "")
        if not contract.endswith("_USDT"):
            continue
        base = contract.replace("_USDT", "")
        if base in EXCLUDED_BASES:
            continue
        vol_24h = (
            safe_float(t.get("volume_24h_settle"))
            or safe_float(t.get("volume_24h_quote"))
            or safe_float(t.get("volume_24h_usd"))
        )
        if vol_24h < MIN_24H_VOL_USD:
            continue

        change_24h = abs(safe_float(t.get("change_percentage")))
        funding_abs = abs(safe_float(t.get("funding_rate"))) * 100.0
        priority = vol_24h / 1_000_000.0 + change_24h * 2.5 + funding_abs * 150.0
        liquid_tickers.append((priority, vol_24h, t))

    liquid_tickers.sort(key=lambda x: x[0], reverse=True)
    candidates = liquid_tickers[:MAX_CANDIDATES]

    alerts = []
    for _, vol_24h, t in candidates:
        contract = t["contract"]
        candle_info = analyze_candles(contract)
        time.sleep(0.08)
        if not candle_info:
            continue

        vol_ratio = candle_info["vol_ratio"]
        pct_15m = candle_info["pct_15m"]
        funding_pct = safe_float(t.get("funding_rate")) * 100.0

        is_interesting = (
            vol_ratio >= VOL_SURGE_THRESHOLD
            or (vol_ratio >= 1.6 and abs(pct_15m) >= 1.8)
            or (abs(funding_pct) >= 0.04 and vol_ratio >= 1.5)
        )
        if not is_interesting:
            continue

        oi_info = get_oi_metrics(contract)
        if vol_ratio < VOL_SURGE_THRESHOLD and abs(oi_info["oi_15m_pct"]) < OI_CHANGE_THRESHOLD:
            continue

        quanto_mult = multipliers.get(contract, 1.0)
        perp_cvd = get_perp_cvd(contract, quanto_mult)
        spot_cvd = get_spot_cvd(contract)
        time.sleep(0.1)

        perp_price = safe_float(t.get("last")) or candle_info["close"]
        spot_price = spot_prices.get(contract, 0.0)
        basis_pct = ((perp_price - spot_price) / spot_price * 100.0) if spot_price > 0 else 0.0

        score, setup_tag = classify_setup(
            pct_15m=pct_15m,
            vol_ratio=vol_ratio,
            oi_15m=oi_info["oi_15m_pct"],
            funding_pct=funding_pct,
            basis_pct=basis_pct,
            spot_cvd=spot_cvd,
            perp_cvd=perp_cvd,
        )

        alerts.append({
            "score": score,
            "contract": contract,
            "setup_tag": setup_tag,
            "price": perp_price,
            "pct_15m": pct_15m,
            "pct_24h": safe_float(t.get("change_percentage")),
            "vol_ratio": vol_ratio,
            "vol_24h": vol_24h,
            "breakout": candle_info["breakout_status"],
            "oi_15m": oi_info["oi_15m_pct"],
            "oi_1h": oi_info["oi_1h_pct"],
            "oi_usd": oi_info["oi_usd"],
            "funding_pct": funding_pct,
            "basis_pct": basis_pct,
            "perp_cvd": perp_cvd,
            "spot_cvd": spot_cvd,
        })

    alerts.sort(key=lambda x: (x["score"], x["vol_ratio"]), reverse=True)
    top_alerts = alerts[:TOP_ALERTS_LIMIT]

    now_utc = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if not top_alerts:
        print(f"[{now_utc}] No strong signals found.")
        if GITHUB_EVENT_NAME == "workflow_dispatch":
            send_telegram(
                f"✅ <b>رادار کریپتو با موفقیت اجرا شد</b> ({now_utc})\n"
                f"اتصال به گیت‌هاب و صرافی Gate برقرار است؛ در این لحظه سیگنال پرقدرتی در بازار دیده نشد."
            )
        return

    blocks = [f"📡 <b>هشدار رادار حجم و OI</b> ({now_utc})\n"]
    for a in top_alerts:
        symbol = a["contract"].replace("_USDT", "")
        perp_state = (
            describe_cvd_state(a["perp_cvd"]["imbalance"], "perp")
            if a["perp_cvd"]["available"] else "نامشخص"
        )
        spot_state = (
            describe_cvd_state(a["spot_cvd"]["imbalance"], "spot")
            if a["spot_cvd"]["available"] else "نامشخص"
        )
        oi_str = f"{a['oi_15m']:+.2f}% (15m) | {a['oi_1h']:+.2f}% (1h)"
        basis_str = f"{a['basis_pct']:+.2f}%" if a["basis_pct"] != 0 else "N/A"

        block = (
            f"━━━━━━━━━━━━━━━\n"
            f"🪙 <b>#{symbol}</b> — <code>{a['price']}</code>\n"
            f"🎯 <b>{a['setup_tag']}</b>\n"
            f"📊 تغییر قیمت: <b>{a['pct_15m']:+.2f}%</b> (15m) | {a['pct_24h']:+.1f}% (24h)\n"
            f"🔥 جهش حجم 15m: <b>{a['vol_ratio']:.1f}x</b> (حجم 24h: {fmt_usd(a['vol_24h'])})\n"
            f"📍 موقعیت: {a['breakout']}\n"
            f"📈 تغییر OI: <b>{oi_str}</b>\n"
            f"💸 فاندینگ: <b>{a['funding_pct']:+.4f}%</b> | شکاف اسپات: <b>{basis_str}</b>\n"
            f"⚡ فیوچرز CVD: {perp_state}\n"
            f"💎 اسپات CVD: {spot_state}"
        )
        blocks.append(block)

    send_telegram("\n".join(blocks))
    print(f"[{now_utc}] Sent {len(top_alerts)} alerts.")


if __name__ == "__main__":
    run_scan()
