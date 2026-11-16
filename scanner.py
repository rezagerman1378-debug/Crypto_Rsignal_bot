
import os
import time
import statistics
import requests
from collections import defaultdict, deque

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
MAX_ALERTS = int(os.getenv("MAX_ALERTS_PER_HOUR", "5"))
TOP = int(os.getenv("TOP_CANDIDATES", "60"))

BITUNIX = "https://fapi.bitunix.com"
TOOBIT = "https://api.toobit.com"

session = requests.Session()
session.headers.update({"User-Agent": "EarlyScanner-V3"})

alert_history = deque()
sent = set()
errors = 0


def get(url, params=None):
    r = session.get(url, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict):
        if data.get("code") not in (None, 0, "0"):
            raise ValueError(str(data.get("msg", "API error")))
        return data.get("data", data)

    return data


def send(message):
    if not TOKEN or not CHAT_ID:
        print(message)
        return

    r = session.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        },
        timeout=12
    )
    r.raise_for_status()


def number(value, default=0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def normalize_candles(raw):
    result = []

    if isinstance(raw, dict):
        raw = raw.get("list", raw.get("rows", []))

    for x in raw or []:
        try:
            if isinstance(x, dict):
                o = number(x.get("open"))
                c = number(x.get("close"))
                qv = number(x.get("quoteVol"))
                if not qv:
                    qv = number(x.get("baseVol")) * c
            else:
                o = number(x[1])
                c = number(x[4])
                qv = number(x[7] if len(x) > 7 else x[5])
            if o > 0 and c > 0:
                result.append({"open": o, "close": c, "volume": qv})
        except Exception:
            continue

    return result


def volume_ratio(candles):
    if len(candles) < 8:
        return 1

    volumes = [x["volume"] for x in candles[-8:-1]]
    current = candles[-1]["volume"]
    avg = statistics.mean(volumes)

    return current / avg if avg > 0 else 1


def price_move(candles, periods=5):
    if len(candles) <= periods:
        return 0

    old = candles[-periods-1]["close"]
    new = candles[-1]["close"]

    return (new / old - 1) * 100 if old else 0


def book_imbalance(book):
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    bid_value = sum(
        number(x[0]) * number(x[1]) for x in bids[:10]
    )
    ask_value = sum(
        number(x[0]) * number(x[1]) for x in asks[:10]
    )

    total = bid_value + ask_value
    return bid_value / total if total else 0.5


def evaluate(vr, move, imbalance, kind):
    score = 0
    reasons = []

    # Unusual volume
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

    # Early price momentum
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


def notify(exchange, symbol, kind, score, vr, move, imbalance, reasons):
    global alert_history

    now = time.time()

    while alert_history and now - alert_history[0] > 3600:
        alert_history.popleft()

    key = (exchange, symbol, kind)

    if key in sent or len(alert_history) >= MAX_ALERTS:
        return

    if score < MIN_SCORE:
        return

    sent.add(key)
    alert_history.append(now)

    icon = "🟢" if kind == "PUMP" else "🔴"
    title = "EARLY PUMP" if kind == "PUMP" else "EARLY DUMP"

    message = (
        f"{icon} {title} V3\n\n"
        f"Exchange: {exchange}\n"
        f"Contract: {symbol}\n"
        f"Direction: {kind}\n"
        f"Score: {score}/100\n\n"
        f"Volume: {vr:.2f}x\n"
        f"5m Change: {move:+.2f}%\n"
        f"Order Book Imbalance: {imbalance:.2f}\n\n"
        f"Detection reasons:\n"
        + "\n".join("• " + x for x in reasons)
        + "\n\n⚠️ Early warning, not guaranteed prediction."
    )

    send(message)


def scan_bitunix():
    global errors

    tickers = get(f"{BITUNIX}/api/v1/futures/market/tickers")
    if not isinstance(tickers, list):
        raise ValueError("Unexpected Bitunix ticker response")

    candidates = []

    for x in tickers:
        try:
            symbol = x["symbol"]
            volume = number(x.get("quoteVol"))

            if symbol.endswith("USDT") and volume > 0:
                candidates.append((volume, symbol))
        except Exception:
            continue

    candidates.sort(reverse=True)
    print("Bitunix candidates:", len(candidates))

    for _, symbol in candidates[:TOP]:
        try:
            candles = get(
                f"{BITUNIX}/api/v1/futures/market/kline",
                {"symbol": symbol, "interval": "1m", "limit": 20}
            )

            book = get(
                f"{BITUNIX}/api/v1/futures/market/depth",
                {"symbol": symbol, "limit": 15}
            )

            if not isinstance(book, dict):
                continue

            candles = normalize_candles(candles)

            if len(candles) < 8:
                continue

            vr = volume_ratio(candles)
            move = price_move(candles)
            imbalance = book_imbalance(book)

            for kind in ("PUMP", "DUMP"):
                score, reasons = evaluate(
                    vr, move, imbalance, kind
                )

                notify(
                    "Bitunix", symbol, kind,
                    score, vr, move, imbalance, reasons
                )

        except Exception as e:
            errors += 1
            print("Bitunix error:", symbol, str(e)[:200])


def scan_toobit():
    global errors

    try:
        tickers = get(
            f"{TOOBIT}/quote/v1/contract/ticker/24hr"
        )
    except Exception as e:
        print("Toobit ticker error:", e)
        errors += 1
        return

    if not isinstance(tickers, list):
        print("Unexpected Toobit ticker format")
        return

    candidates = []

    for x in tickers:
        try:
            symbol = x["s"]
            volume = number(x.get("qv"))

            if symbol.endswith("-SWAP-USDT") and volume > 0:
                candidates.append((volume, symbol))
        except Exception:
            continue

    candidates.sort(reverse=True)
    print("Toobit candidates:", len(candidates))

    for _, symbol in candidates[:TOP]:
        try:
            raw = get(
                f"{TOOBIT}/quote/v1/klines",
                {"symbol": symbol, "interval": "1m", "limit": 20}
            )

            book = get(
                f"{TOOBIT}/quote/v1/depth",
                {"symbol": symbol, "limit": 20}
            )

            if not isinstance(book, dict):
                continue

            candles = normalize_candles(raw)

            if len(candles) < 8:
                continue

            vr = volume_ratio(candles)
            move = price_move(candles)
            imbalance = book_imbalance(book)

            for kind in ("PUMP", "DUMP"):
                score, reasons = evaluate(
                    vr, move, imbalance, kind
                )

                notify(
                    "Toobit", symbol, kind,
                    score, vr, move, imbalance, reasons
                )

        except Exception as e:
            errors += 1
            print("Toobit error:", symbol, str(e)[:200])


def main():
    print("EARLY PUMP & DUMP SCANNER V3")
    print("One-shot scan started.")
    print("No automatic trading.")

    try:
        scan_bitunix()
    except Exception as e:
        print("Bitunix scan failed:", e)

    try:
        scan_toobit()
    except Exception as e:
        print("Toobit scan failed:", e)

    print("Scan completed.")
    print("Errors:", errors)


if __name__ == "__main__":
    main()
