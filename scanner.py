import os
import json
import time
import requests
from datetime import datetime, timezone
from collections import defaultdict

VERSION = "V5"

BITUNIX = "https://fapi.bitunix.com"
TOOBIT = "https://api.toobit.com"

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TOP_CANDIDATES = int(os.getenv("TOP_CANDIDATES", "100"))
MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "5"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "90"))

STATE_FILE = "scanner_state.json"
TIMEOUT = 12

session = requests.Session()
session.headers.update({
    "User-Agent": "Early-Pump-Dump-Scanner-V5",
    "Accept": "application/json"
})


# ==========================================
# GENERAL HELPERS
# ==========================================

def now_ts():
    return int(time.time())


def safe_float(value, default=0.0):
    try:
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return default
        return result
    except (TypeError, ValueError):
        return default


def get_json(url, params=None):
    try:
        response = session.get(
            url,
            params=params,
            timeout=TIMEOUT
        )
        response.raise_for_status()
        data = response.json()

        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, 0, "0", 200, "200"):
                return None

            if "data" in data:
                return data["data"]

        return data

    except Exception as exc:
        print("API error:", url, str(exc)[:180])
        return None


def normalize_base(exchange, symbol):
    symbol = str(symbol).upper().strip()

    if exchange == "Toobit":
        symbol = symbol.replace("-SWAP-USDT", "")

    if symbol.endswith("USDT"):
        symbol = symbol[:-4]

    return symbol.replace("-", "").replace("_", "")


def toobit_symbol(base):
    return base + "-SWAP-USDT"


# ==========================================
# TELEGRAM
# ==========================================

def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": True
            },
            timeout=TIMEOUT
        )
        response.raise_for_status()
        result = response.json()
        return bool(result.get("ok"))

    except Exception as exc:
        print("Telegram error:", str(exc)[:180])
        return False


# ==========================================
# BITUNIX MARKET DATA
# ==========================================

def bitunix_tickers():
    data = get_json(
        BITUNIX + "/api/v1/futures/market/tickers"
    )

    if not isinstance(data, list):
        return []

    markets = []

    for item in data:
        symbol = item.get("symbol", "")
        base = normalize_base("Bitunix", symbol)

        if not base or not symbol.endswith("USDT"):
            continue

        volume = safe_float(item.get("quoteVol"))
        price = safe_float(item.get("lastPrice"))

        if volume <= 0 or price <= 0:
            continue

        markets.append({
            "exchange": "Bitunix",
            "symbol": symbol,
            "base": base,
            "price": price,
            "volume": volume,
            "change24": (
                (price / safe_float(item.get("open")) - 1) * 100
                if safe_float(item.get("open")) > 0 else 0
            )
        })

    return sorted(
        markets,
        key=lambda x: x["volume"],
        reverse=True
    )


def bitunix_candles(symbol):
    data = get_json(
        BITUNIX + "/api/v1/futures/market/kline",
        {
            "symbol": symbol,
            "interval": "5m",
            "limit": 100
        }
    )

    if not isinstance(data, list):
        return []

    candles = []

    for item in data:
        if not isinstance(item, dict):
            continue

        candles.append({
            "time": safe_float(item.get("time")),
            "open": safe_float(item.get("open")),
            "high": safe_float(item.get("high")),
            "low": safe_float(item.get("low")),
            "close": safe_float(item.get("close")),
            "volume": safe_float(item.get("quoteVol"))
        })

    return sorted(candles, key=lambda x: x["time"])


def bitunix_book(symbol):
    data = get_json(
        BITUNIX + "/api/v1/futures/market/depth",
        {
            "symbol": symbol,
            "limit": "15"
        }
    )

    if not isinstance(data, dict):
        return None

    return {
        "bids": data.get("bids", []),
        "asks": data.get("asks", [])
    }


def bitunix_funding_all():
    data = get_json(
        BITUNIX + "/api/v1/futures/market/funding_rate/batch"
    )

    result = {}

    if isinstance(data, list):
        for item in data:
            symbol = item.get("symbol", "")
            result[symbol] = safe_float(item.get("fundingRate"))

    return result


# ==========================================
# TOOBIT MARKET DATA
# ==========================================

def toobit_tickers():
    data = get_json(
        TOOBIT + "/quote/v1/contract/ticker/24hr"
    )

    if not isinstance(data, list):
        return []

    markets = []

    for item in data:
        symbol = item.get("s", "")

        if not symbol.endswith("-SWAP-USDT"):
            continue

        base = normalize_base("Toobit", symbol)
        price = safe_float(item.get("c"))
        volume = safe_float(item.get("qv"))

        if not base or price <= 0 or volume <= 0:
            continue

        markets.append({
            "exchange": "Toobit",
            "symbol": symbol,
            "base": base,
            "price": price,
            "volume": volume,
            "change24": safe_float(item.get("pcp"))
        })

    return sorted(
        markets,
        key=lambda x: x["volume"],
        reverse=True
    )


def toobit_candles(symbol):
    end_time = int(time.time() * 1000)
    start_time = end_time - (100 * 5 * 60 * 1000)

    data = get_json(
        TOOBIT + "/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": "5m",
            "startTime": start_time,
            "endTime": end_time,
            "limit": 100
        }
    )

    if not isinstance(data, list):
        return []

    candles = []

    for item in data:
        if not isinstance(item, list) or len(item) < 6:
            continue

        candles.append({
            "time": safe_float(item[0]),
            "open": safe_float(item[1]),
            "high": safe_float(item[2]),
            "low": safe_float(item[3]),
            "close": safe_float(item[4]),
            "volume": safe_float(item[7] if len(item) > 7 else item[5])
        })

    return sorted(candles, key=lambda x: x["time"])


def toobit_book(symbol):
    data = get_json(
        TOOBIT + "/quote/v1/depth",
        {
            "symbol": symbol,
            "limit": 20
        }
    )

    if not isinstance(data, dict):
        return None

    return {
        "bids": data.get("b", []),
        "asks": data.get("a", [])
    }


def toobit_funding(symbol):
    data = get_json(
        TOOBIT + "/api/v1/futures/fundingRate",
        {"symbol": symbol}
    )

    if isinstance(data, list) and data:
        return safe_float(data[0].get("rate"))

    return None


def toobit_open_interest(symbol):
    data = get_json(
        TOOBIT + "/quote/v1/openInterest",
        {"symbol": symbol}
    )

    if not isinstance(data, dict):
        return None

    rows = data.get("openInterestList", [])

    for item in rows:
        if item.get("symbol") == symbol:
            return safe_float(item.get("size"))

    return None


# ==========================================
# CANDIDATE COLLECTION
# ==========================================

def get_candidates():
    bitunix = bitunix_tickers()
    toobit = toobit_tickers()

    print("Bitunix markets:", len(bitunix))
    print("Toobit markets:", len(toobit))

    bitunix = bitunix[:TOP_CANDIDATES]
    toobit = toobit[:TOP_CANDIDATES]

    combined = bitunix + toobit

    grouped = defaultdict(list)

    for market in combined:
        grouped[market["base"]].append(market)

    unique = []

    for base, choices in grouped.items():
        # Toobit has documented public OI data.
        # Prefer it when the same asset exists on both venues.
        choices.sort(
            key=lambda x: (
                x["exchange"] == "Toobit",
                x["volume"]
            ),
            reverse=True
        )

        unique.append(choices[0])

    unique.sort(
        key=lambda x: x["volume"],
        reverse=True
    )

    print("Unique assets selected:", len(unique))

    return unique

# ==========================================
# STATE MANAGEMENT
# ==========================================

def load_state():
    default = {
        "alerts": {},
        "oi_history": {},
        "sent_times": []
    }

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)

        if isinstance(saved, dict):
            for key in default:
                if key in saved:
                    default[key] = saved[key]

    except Exception:
        pass

    return default


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("State save error:", exc)


# ==========================================
# MARKET CALCULATIONS
# ==========================================

def volume_ratio(candles):
    if len(candles) < 22:
        return 0

    previous = [
        c["volume"] for c in candles[-22:-2]
        if c["volume"] > 0
    ]

    if len(previous) < 10:
        return 0

    average = sum(previous) / len(previous)
    latest = candles[-1]["volume"]

    if average <= 0:
        return 0

    return latest / average


def price_momentum(candles):
    if len(candles) < 5:
        return 0

    old_price = candles[-5]["close"]
    new_price = candles[-1]["close"]

    if old_price <= 0:
        return 0

    return ((new_price / old_price) - 1) * 100


def recent_move(candles, count=8):
    if len(candles) < count + 1:
        return 0

    old_price = candles[-count-1]["close"]
    new_price = candles[-1]["close"]

    if old_price <= 0:
        return 0

    return ((new_price / old_price) - 1) * 100


def book_imbalance(book):
    if not book:
        return None

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    def total_qty(rows):
        total = 0

        for row in rows[:15]:
            try:
                if isinstance(row, dict):
                    qty = safe_float(
                        row.get("qty", row.get("volume"))
                    )
                else:
                    qty = safe_float(row[1])

                total += qty

            except Exception:
                continue

        return total

    bid_total = total_qty(bids)
    ask_total = total_qty(asks)

    if bid_total <= 0 or ask_total <= 0:
        return None

    return bid_total / (bid_total + ask_total)


# ==========================================
# OPEN INTEREST CHANGE
# ==========================================

def update_oi_history(base, current_oi, state):
    if current_oi is None or current_oi <= 0:
        return None

    history = state.setdefault("oi_history", {})
    old = history.get(base)

    history[base] = {
        "value": current_oi,
        "time": now_ts()
    }

    if not old:
        return None

    old_value = safe_float(old.get("value"))
    old_time = int(old.get("time", 0))

    if old_value <= 0 or now_ts() - old_time < 240:
        return None

    change = ((current_oi / old_value) - 1) * 100

    return change


# ==========================================
# SCORING
# ==========================================

def evaluate_signal(
    candles,
    book,
    funding=None,
    oi_change=None
):
    if len(candles) < 25:
        return None

    momentum = price_momentum(candles)
    volume = volume_ratio(candles)
    move = recent_move(candles, 8)
    imbalance = book_imbalance(book)

    direction = "PUMP" if momentum >= 0 else "DUMP"

    score = 0
    reasons = []

    # Volume spike
    if volume >= 3:
        score += 25
        reasons.append("حجم بسیار بالا")
    elif volume >= 2:
        score += 20
        reasons.append("افزایش حجم")
    elif volume >= 1.5:
        score += 12
        reasons.append("رشد نسبی حجم")

    # Price momentum
    abs_momentum = abs(momentum)

    if abs_momentum >= 1.8:
        score += 25
        reasons.append("شتاب قیمت قوی")
    elif abs_momentum >= 1.0:
        score += 18
        reasons.append("شتاب قیمت")
    elif abs_momentum >= 0.5:
        score += 10
        reasons.append("حرکت اولیه قیمت")

    # Order book imbalance
    if imbalance is not None:
        if direction == "PUMP" and imbalance >= 0.65:
            score += 20
            reasons.append("برتری سفارش خرید")
        elif direction == "DUMP" and imbalance <= 0.35:
            score += 20
            reasons.append("برتری سفارش فروش")
        elif direction == "PUMP" and imbalance >= 0.57:
            score += 10
        elif direction == "DUMP" and imbalance <= 0.43:
            score += 10

    # Open interest change
    if oi_change is not None:
        if oi_change >= 2:
            score += 15
            reasons.append("افزایش Open Interest")
        elif oi_change >= 0.8:
            score += 8
            reasons.append("رشد نسبی Open Interest")
        elif oi_change <= -2:
            score -= 8
            reasons.append("کاهش Open Interest")

    # Funding is contextual, not a standalone signal
    if funding is not None:
        if direction == "PUMP" and funding > 0.001:
            score -= 5
            reasons.append("Funding مثبت و نسبتاً بالا")
        elif direction == "DUMP" and funding < -0.001:
            score -= 5
            reasons.append("Funding منفی و نسبتاً پایین")

    # Avoid chasing an already extended move
    if direction == "PUMP" and move >= 5:
        score -= 25
        reasons.append("رشد اخیر بیش از حد")
    elif direction == "DUMP" and move <= -5:
        score -= 25
        reasons.append("ریزش اخیر بیش از حد")
    elif abs(move) >= 3.5:
        score -= 12
        reasons.append("حرکت اخیر کشیده شده")

    score = max(0, min(100, score))

    if score < 40:
        stage = 0
    elif score < 55:
        stage = 1
    elif score < 70:
        stage = 2
    else:
        stage = 3

    return {
        "score": score,
        "direction": direction,
        "stage": stage,
        "momentum": momentum,
        "volume_ratio": volume,
        "oi_change": oi_change,
        "funding": funding,
        "imbalance": imbalance,
        "reasons": reasons
    }


# ==========================================
# ANALYZE ONE MARKET
# ==========================================

def analyze_market(market, state, bitunix_funding):
    exchange = market["exchange"]
    symbol = market["symbol"]
    base = market["base"]

    if exchange == "Bitunix":
        candles = bitunix_candles(symbol)
        book = bitunix_book(symbol)
        funding = bitunix_funding.get(symbol)
        oi_change = None

    else:
        candles = toobit_candles(symbol)
        book = toobit_book(symbol)
        funding = toobit_funding(symbol)

        current_oi = toobit_open_interest(symbol)
        oi_change = update_oi_history(
            base,
            current_oi,
            state
        )

        # Small delay to respect public API rate limits
        time.sleep(0.15)

    signal = evaluate_signal(
        candles,
        book,
        funding,
        oi_change
    )

    if not signal:
        return None

    signal.update({
        "exchange": exchange,
        "symbol": symbol,
        "base": base,
        "price": market["price"],
        "volume24": market["volume"]
    })

    return signal


# ==========================================
# ALERT STAGES
# ==========================================

def stage_name(stage):
    return {
        1: "🟡 آماده‌باش",
        2: "🟠 هشدار قوی",
        3: "🔴 سیگنال نهایی"
    }.get(stage, "")


def should_send_alert(result, state):
    if result["stage"] == 0:
        return False

    key = result["base"] + "_" + result["direction"]
    alerts = state.setdefault("alerts", {})
    sent_times = state.setdefault("sent_times", [])

    current = now_ts()

    # Keep only the last hour
    sent_times[:] = [
        t for t in sent_times
        if current - int(t) < 3600
    ]

    if len(sent_times) >= MAX_ALERTS_PER_HOUR:
        return False

    previous = alerts.get(key, {})
    old_stage = int(previous.get("stage", 0))
    old_score = int(previous.get("score", 0))
    old_time = int(previous.get("time", 0))

    cooldown = COOLDOWN_MINUTES * 60

    if current - old_time < cooldown:
        # During cooldown, only allow a meaningful upgrade
        if result["stage"] <= old_stage:
            return False

    if result["stage"] < old_stage:
        return False

    # Same stage: only notify on a meaningful score increase
    if result["stage"] == old_stage:
        if result["score"] < old_score + 10:
            return False

    return True


def process_alert(result, state):
    if not should_send_alert(result, state):
        return False

    direction_text = (
        "احتمال حرکت صعودی"
        if result["direction"] == "PUMP"
        else "احتمال حرکت نزولی"
    )

    reasons = result.get("reasons", [])
    reason_text = "\n".join(
        "• " + reason for reason in reasons
    ) or "نشانه‌های اولیه بازار"

    message = (
        f"{stage_name(result['stage'])}\n"
        f"EARLY {result['direction']} — {VERSION}\n\n"
        f"ارز: {result['base']}\n"
        f"صرافی: {result['exchange']}\n"
        f"قیمت: {result['price']}\n"
        f"امتیاز: {result['score']}/100\n"
        f"جهت احتمالی: {direction_text}\n\n"
        f"شتاب ۵ دقیقه‌ای: {result['momentum']:.2f}%\n"
        f"نسبت حجم: {result['volume_ratio']:.2f}x\n"
        f"Funding: "
        f"{result['funding'] if result['funding'] is not None else 'ناموجود'}\n"
        f"تغییر OI: "
        f"{result['oi_change'] if result['oi_change'] is not None else 'ناموجود'}\n\n"
        f"دلایل:\n{reason_text}\n\n"
        "⚠️ این هشدار پیشنهاد قطعی خرید یا فروش نیست."
    )

    if not telegram_send(message):
        return False

    key = result["base"] + "_" + result["direction"]

    state["alerts"][key] = {
        "stage": result["stage"],
        "score": result["score"],
        "time": now_ts()
    }

    state["sent_times"].append(now_ts())

    return True

# ==========================================
# SCAN SUMMARY
# ==========================================

def send_scan_summary(stats, scanned, alerts_sent, errors):
    message = (
        f"📊 گزارش اسکن {VERSION}\n"
        f"زمان: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"🔎 نامزدهای انتخاب‌شده: {stats['candidates']}\n"
        f"✅ تحلیل موفق: {scanned}\n"
        f"⚠️ خطاها: {errors}\n"
        f"📨 هشدارهای ارسال‌شده: {alerts_sent}\n\n"
        f"Bitunix: {stats['Bitunix']}\n"
        f"Toobit: {stats['Toobit']}\n\n"
        "سطوح هشدار:\n"
        "🟡 40 تا 54: آماده‌باش\n"
        "🟠 55 تا 69: هشدار قوی\n"
        "🔴 70 تا 100: سیگنال نهایی\n\n"
        "ربات فقط تحلیل و هشدار می‌دهد؛ معامله خودکار ندارد."
    )

    telegram_send(message)


# ==========================================
# MAIN SCANNER
# ==========================================

def main():
    print("=" * 45)
    print("EARLY PUMP & DUMP SCANNER", VERSION)
    print("No automatic trading")
    print("=" * 45)

    state = load_state()

    stats = {
        "candidates": 0,
        "Bitunix": 0,
        "Toobit": 0
    }

    scanned = 0
    alerts_sent = 0
    errors = 0

    try:
        candidates = get_candidates()

        stats["candidates"] = len(candidates)

        bitunix_funding = bitunix_funding_all()

        for index, market in enumerate(candidates, start=1):
            exchange = market["exchange"]
            stats[exchange] += 1

            print(
                f"[{index}/{len(candidates)}] "
                f"{exchange} {market['symbol']}"
            )

            try:
                result = analyze_market(
                    market,
                    state,
                    bitunix_funding
                )

                if result is None:
                    print("Not enough valid market data")
                    continue

                scanned += 1

                print(
                    "Score:",
                    result["score"],
                    "Direction:",
                    result["direction"],
                    "Stage:",
                    result["stage"]
                )

                if process_alert(result, state):
                    alerts_sent += 1
                    print("Telegram alert sent")

            except Exception as exc:
                errors += 1
                print(
                    "Market analysis error:",
                    market.get("symbol"),
                    str(exc)[:180]
                )

    except Exception as exc:
        errors += 1
        print("Main scan error:", str(exc)[:250])

    finally:
        save_state(state)

        send_scan_summary(
            stats,
            scanned,
            alerts_sent,
            errors
        )

        save_state(state)

    print("=" * 45)
    print("Scan completed")
    print("Analyzed:", scanned)
    print("Alerts:", alerts_sent)
    print("Errors:", errors)
    print("=" * 45)


if __name__ == "__main__":
    main()
