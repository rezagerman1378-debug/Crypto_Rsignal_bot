import os
import time
import statistics
import requests
from collections import defaultdict, deque

# Early Pump + Dump Scanner V4
# Conservative alerts only. This script does NOT place trades.

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_HOUR", "5"))
TOP = int(os.getenv("TOP_CANDIDATES", "30"))

BITUNIX = "https://fapi.bitunix.com"
TOOBIT = "https://api.toobit.com"

session = requests.Session()
session.headers.update({"User-Agent": "EarlyScanner-V4"})

alert_history = deque()
sent = set()
stats = {
    "Bitunix": {"candidates": 0, "analyzed": 0, "errors": 0, "signals": 0},
    "Toobit": {"candidates": 0, "analyzed": 0, "errors": 0, "signals": 0},
}
best_scores = {"PUMP": [], "DUMP": []}


def get(url, params=None):
    response = session.get(url, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict):
        code = data.get("code")
        if code not in (None, 0, "0"):
            raise ValueError(str(data.get("msg", "API error")))
        return data.get("data", data)

    return data


def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM NOT CONFIGURED: token or chat_id is empty.")
        return False

    response = session.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=15
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected message: " + str(result))

    print("TELEGRAM SENT: message accepted by Telegram.")
    return True


def number(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def normalize_candles(raw):
    result = []

    if isinstance(raw, dict):
        raw = raw.get("list", raw.get("rows", []))

    for candle in raw or []:
        try:
            if isinstance(candle, dict):
                opening = number(candle.get("open"))
                closing = number(candle.get("close"))
                quote_volume = number(candle.get("quoteVol"))
                if not quote_volume:
                    quote_volume = number(candle.get("baseVol")) * closing
            else:
                opening = number(candle[1])
                closing = number(candle[4])
                quote_volume = number(
                    candle[7] if len(candle) > 7 else candle[5]
                )

            if opening > 0 and closing > 0:
                result.append({
                    "open": opening,
                    "close": closing,
                    "volume": quote_volume
                })
        except (IndexError, TypeError, KeyError):
            continue

    return result


def volume_ratio(candles):
    if len(candles) < 8:
        return 1.0

    previous_volumes = [x["volume"] for x in candles[-8:-1]]
    current_volume = candles[-1]["volume"]
    average_volume = statistics.mean(previous_volumes)

    return current_volume / average_volume if average_volume > 0 else 1.0


def price_move(candles, periods=5):
    if len(candles) <= periods:
        return 0.0

    old_price = candles[-periods - 1]["close"]
    new_price = candles[-1]["close"]

    return (new_price / old_price - 1) * 100 if old_price else 0.0


def book_imbalance(book):
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    bid_value = sum(number(x[0]) * number(x[1]) for x in bids[:10])
    ask_value = sum(number(x[0]) * number(x[1]) for x in asks[:10])
    total = bid_value + ask_value

    return bid_value / total if total else 0.5


def evaluate(vr, move, imbalance, kind):
    score = 0
    reasons = []

    if vr >= 5:
        score += 28
        reasons.append("حجم معاملات بسیار غیرعادی")
    elif vr >= 3:
        score += 23
        reasons.append("افزایش شدید حجم")
    elif vr >= 2:
        score += 16
        reasons.append("افزایش حجم معاملات")
    elif vr >= 1.5:
        score += 9

    if kind == "PUMP":
        if 0.15 <= move <= 1.5:
            score += 24
            reasons.append("شتاب اولیه صعودی")
        elif 1.5 < move <= 3:
            score += 17
        elif 3 < move <= 5:
            score += 7
        elif move > 5:
            score -= 20
            reasons.append("حرکت صعودی بیش از حد")
        elif move < -0.5:
            score -= 12

        if imbalance >= 0.70:
            score += 23
            reasons.append("غلبه سفارش‌های خرید")
        elif imbalance >= 0.60:
            score += 14
        elif imbalance <= 0.35:
            score -= 12
    else:
        if -1.5 <= move <= -0.15:
            score += 24
            reasons.append("شتاب اولیه نزولی")
        elif -3 <= move < -1.5:
            score += 17
        elif -5 <= move < -3:
            score += 7
        elif move < -5:
            score -= 20
            reasons.append("حرکت نزولی بیش از حد")
        elif move > 0.5:
            score -= 12

        if imbalance <= 0.30:
            score += 23
            reasons.append("غلبه سفارش‌های فروش")
        elif imbalance <= 0.40:
            score += 14
        elif imbalance >= 0.65:
            score -= 12

    if vr < 1.5:
        score = min(score, 55)

    return max(0, min(100, score)), reasons


def remember_best(exchange, symbol, kind, score, vr, move, imbalance):
    item = {
        "exchange": exchange,
        "symbol": symbol,
        "score": score,
        "volume": vr,
        "move": move,
        "imbalance": imbalance
    }
    best_scores[kind].append(item)
    best_scores[kind].sort(key=lambda x: x["score"], reverse=True)
    del best_scores[kind][5:]


def notify(exchange, symbol, kind, score, vr, move, imbalance, reasons):
    now = time.time()

    while alert_history and now - alert_history[0] > 3600:
        alert_history.popleft()

    key = (exchange, symbol, kind)

    if key in sent or len(alert_history) >= MAX_ALERTS or score < MIN_SCORE:
        return

    icon = "🟢" if kind == "PUMP" else "🔴"
    title = "EARLY PUMP" if kind == "PUMP" else "EARLY DUMP"

    message = (
        f"{icon} {title} V4\n\n"
        f"Exchange: {exchange}\n"
        f"Contract: {symbol}\n"
        f"Direction: {kind}\n"
        f"Score: {score}/100\n\n"
        f"Volume: {vr:.2f}x\n"
        f"5m Change: {move:+.2f}%\n"
        f"Order Book Imbalance: {imbalance:.2f}\n\n"
        f"Detection reasons:\n"
        + "\n".join("• " + reason for reason in reasons)
        + "\n\n⚠️ Early warning, not guaranteed prediction. No automatic trading."
    )

    # Mark as sent only after Telegram confirms acceptance.
    if telegram_send(message):
        sent.add(key)
        alert_history.append(now)
        stats[exchange]["signals"] += 1


def process_symbol(exchange, symbol, candle_url, candle_params,
                   book_url, book_params):
    candles_raw = get(candle_url, candle_params)
    book = get(book_url, book_params)

    if not isinstance(book, dict):
        raise ValueError("Unexpected order-book response")

    candles = normalize_candles(candles_raw)
    if len(candles) < 8:
        raise ValueError("Not enough valid candles")

    vr = volume_ratio(candles)
    move = price_move(candles)
    imbalance = book_imbalance(book)

    stats[exchange]["analyzed"] += 1

    for kind in ("PUMP", "DUMP"):
        score, reasons = evaluate(vr, move, imbalance, kind)
        remember_best(exchange, symbol, kind, score, vr, move, imbalance)
        notify(exchange, symbol, kind, score, vr, move, imbalance, reasons)


def scan_bitunix():
    tickers = get(f"{BITUNIX}/api/v1/futures/market/tickers")
    if not isinstance(tickers, list):
        raise ValueError("Unexpected Bitunix ticker response")

    candidates = []
    for item in tickers:
        symbol = item.get("symbol", "")
        volume = number(item.get("quoteVol"))
        if symbol.endswith("USDT") and volume > 0:
            candidates.append((volume, symbol))

    candidates.sort(reverse=True)
    stats["Bitunix"]["candidates"] = len(candidates)
    print("Bitunix candidates:", len(candidates), "| selected:", min(TOP, len(candidates)))

    for _, symbol in candidates[:TOP]:
        try:
            process_symbol(
                "Bitunix", symbol,
                f"{BITUNIX}/api/v1/futures/market/kline",
                {"symbol": symbol, "interval": "1m", "limit": 20},
                f"{BITUNIX}/api/v1/futures/market/depth",
                {"symbol": symbol, "limit": 15}
            )
        except Exception as error:
            stats["Bitunix"]["errors"] += 1
            print("Bitunix error:", symbol, str(error)[:200])


def scan_toobit():
    tickers = get(f"{TOOBIT}/quote/v1/contract/ticker/24hr")
    if not isinstance(tickers, list):
        raise ValueError("Unexpected Toobit ticker response")

    candidates = []
    for item in tickers:
        symbol = item.get("s", "")
        volume = number(item.get("qv"))
        if symbol.endswith("-SWAP-USDT") and volume > 0:
            candidates.append((volume, symbol))

    candidates.sort(reverse=True)
    stats["Toobit"]["candidates"] = len(candidates)
    print("Toobit candidates:", len(candidates), "| selected:", min(TOP, len(candidates)))

    for _, symbol in candidates[:TOP]:
        try:
            process_symbol(
                "Toobit", symbol,
                f"{TOOBIT}/quote/v1/klines",
                {"symbol": symbol, "interval": "1m", "limit": 20},
                f"{TOOBIT}/quote/v1/depth",
                {"symbol": symbol, "limit": 20}
            )
        except Exception as error:
            stats["Toobit"]["errors"] += 1
            print("Toobit error:", symbol, str(error)[:200])


def print_report():
    print("\n========== V4 SCAN REPORT ==========")
    print("Telegram token configured:", bool(TOKEN))
    print("Telegram chat ID configured:", bool(CHAT_ID))
    print("Minimum score:", MIN_SCORE)
    print("Candidates selected per exchange:", TOP)

    for exchange, data in stats.items():
        print(
            f"{exchange}: candidates={data['candidates']}, "
            f"analyzed={data['analyzed']}, errors={data['errors']}, "
            f"signals_sent={data['signals']}"
        )

    for kind, label in (("PUMP", "TOP PUMP SCORES"), ("DUMP", "TOP DUMP SCORES")):
        print("\n" + label)
        if not best_scores[kind]:
            print("No valid scores recorded.")
        for item in best_scores[kind]:
            print(
                f"{item['exchange']} {item['symbol']} "
                f"score={item['score']} volume={item['volume']:.2f}x "
                f"move={item['move']:+.2f}% imbalance={item['imbalance']:.2f}"
            )

    print("====================================")
    print("No automatic trading.")


def main():
    print("EARLY PUMP & DUMP SCANNER V4")
    print("One-shot scan started.")

    if not TOKEN or not CHAT_ID:
        print("WARNING: Telegram credentials are missing from environment.")

    try:
        scan_bitunix()
    except Exception as error:
        stats["Bitunix"]["errors"] += 1
        print("Bitunix scan failed:", str(error)[:250])

    try:
        scan_toobit()
    except Exception as error:
        stats["Toobit"]["errors"] += 1
        print("Toobit scan failed:", str(error)[:250])

    print_report()


if __name__ == "__main__":
    main()
