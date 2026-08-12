import os
import time
import math
import sqlite3
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

DEX_URL = "https://api.dexscreener.com/latest/dex/search"
TELEGRAM_URL = "https://api.telegram.org/bot{}/sendMessage"

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "78"))
MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "25000"))
MIN_VOLUME_5M_USD = float(os.getenv("MIN_VOLUME_5M_USD", "5000"))
MAX_MC_USD = float(os.getenv("MAX_MC_USD", "100000000"))
CHAIN = os.getenv("CHAIN", "solana")
SEARCH_TERMS = [x.strip() for x in os.getenv(
    "SEARCH_TERMS",
    "SOL,USDC,SOLANA"
).split(",") if x.strip()]

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

DB_PATH = os.getenv("DB_PATH", "scanner.db")
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.execute("""
CREATE TABLE IF NOT EXISTS observations (
    ts INTEGER NOT NULL,
    pair_address TEXT NOT NULL,
    chain TEXT,
    symbol TEXT,
    price REAL,
    liquidity REAL,
    volume_5m REAL,
    volume_1h REAL,
    buys_5m INTEGER,
    sells_5m INTEGER,
    price_change_5m REAL,
    price_change_1h REAL,
    market_cap REAL
)
""")
DB.execute("""
CREATE TABLE IF NOT EXISTS alerts (
    pair_address TEXT PRIMARY KEY,
    last_alert_ts INTEGER NOT NULL,
    last_score REAL NOT NULL
)
""")
DB.commit()

session = requests.Session()
session.headers.update({"User-Agent": "BreakoutScanner/1.0"})


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def now():
    return int(datetime.now(timezone.utc).timestamp())


def fetch_pairs(term):
    r = session.get(DEX_URL, params={"q": term}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("pairs", []) or []


def save_observation(p):
    tx = p.get("txns", {})
    v = p.get("volume", {})
    pc = p.get("priceChange", {})
    a = tx.get("m5", {}) or {}

    DB.execute("""
        INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now(),
        p.get("pairAddress", ""),
        p.get("chainId", ""),
        (p.get("baseToken") or {}).get("symbol", "?"),
        num(p.get("priceUsd")),
        num((p.get("liquidity") or {}).get("usd")),
        num(v.get("m5")),
        num(v.get("h1")),
        int(a.get("buys", 0) or 0),
        int(a.get("sells", 0) or 0),
        num(pc.get("m5")),
        num(pc.get("h1")),
        num(p.get("marketCap") or p.get("fdv"))
    ))
    DB.commit()


def previous_observation(address):
    return DB.execute("""
        SELECT * FROM observations
        WHERE pair_address=?
        ORDER BY ts DESC LIMIT 1 OFFSET 1
    """, (address,)).fetchone()


def score_pair(p):
    """
    Heuristic score, deliberately conservative.
    It is NOT a prediction of future price.
    """
    liq = num((p.get("liquidity") or {}).get("usd"))
    vol5 = num((p.get("volume") or {}).get("m5"))
    vol1 = num((p.get("volume") or {}).get("h1"))
    pc5 = num((p.get("priceChange") or {}).get("m5"))
    pc1 = num((p.get("priceChange") or {}).get("h1"))
    tx5 = (p.get("txns") or {}).get("m5") or {}
    buys = int(tx5.get("buys", 0) or 0)
    sells = int(tx5.get("sells", 0) or 0)
    mc = num(p.get("marketCap") or p.get("fdv"))

    if liq < MIN_LIQUIDITY_USD or vol5 < MIN_VOLUME_5M_USD:
        return 0, {}

    # 0-20: short-term momentum
    momentum = max(0, min(20, pc5 * 1.5 + max(0, pc1) * 0.15))

    # 0-25: volume relative to liquidity and 1h activity
    vol_ratio = (vol5 * 12 / vol1) if vol1 > 0 else 0
    volume_score = min(15, vol_ratio * 15)
    liquidity_activity = min(10, (vol5 / liq) * 100)
    volume_score += liquidity_activity

    # 0-20: buy pressure
    total = buys + sells
    buy_ratio = buys / total if total else 0
    buy_score = max(0, min(20, (buy_ratio - 0.5) * 80))

    # 0-20: liquidity quality, with diminishing returns
    liquidity_score = min(20, 5 * math.log10(max(liq, 1) / 1000))

    # 0-15: small/mid cap opportunity, but don't reward microscopic caps
    if 0 < mc <= 2_000_000:
        cap_score = 15
    elif mc <= 10_000_000:
        cap_score = 12
    elif mc <= 50_000_000:
        cap_score = 8
    elif mc <= MAX_MC_USD:
        cap_score = 4
    else:
        cap_score = 0

    score = min(100, momentum + volume_score + buy_score +
                liquidity_score + cap_score)

    return score, {
        "liq": liq, "vol5": vol5, "vol1": vol1, "pc5": pc5,
        "pc1": pc1, "buys": buys, "sells": sells, "buy_ratio": buy_ratio,
        "mc": mc
    }


def should_alert(address, score):
    row = DB.execute(
        "SELECT last_alert_ts, last_score FROM alerts WHERE pair_address=?",
        (address,)
    ).fetchone()

    # Avoid repeated alerts for the same coin for 30 minutes unless score
    # improves by at least 8 points.
    if not row:
        return True
    last_ts, last_score = row
    return (now() - last_ts >= 1800) and score >= last_score + 3


def mark_alert(address, score):
    DB.execute("""
        INSERT INTO alerts(pair_address,last_alert_ts,last_score)
        VALUES(?,?,?)
        ON CONFLICT(pair_address) DO UPDATE SET
            last_alert_ts=excluded.last_alert_ts,
            last_score=excluded.last_score
    """, (address, now(), score))
    DB.commit()


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        logging.warning("Telegram credentials not configured.")
        return False

    url = TELEGRAM_URL.format(TG_TOKEN)
    r = session.post(url, json={
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False
    }, timeout=15)
    r.raise_for_status()
    return True


def format_alert(p, score, s):
    base = p.get("baseToken") or {}
    symbol = base.get("symbol", "?")
    name = base.get("name", "?")
    chain = p.get("chainId", "?")
    url = p.get("url", "")
    return (
        f"🚨 BREAKOUT WATCH — {symbol}\n"
        f"{name}\n\n"
        f"Score: {score:.0f}/100\n"
        f"Chain: {chain}\n"
        f"Price: ${num(p.get('priceUsd')):.10g}\n"
        f"Market cap: ${s['mc']:,.0f}\n"
        f"Liquidity: ${s['liq']:,.0f}\n"
        f"5m: {s['pc5']:+.2f}% | 1h: {s['pc1']:+.2f}%\n"
        f"5m volume: ${s['vol5']:,.0f}\n"
        f"5m buys/sells: {s['buys']}/{s['sells']}\n"
        f"Buy ratio: {s['buy_ratio']:.1%}\n\n"
        f"⚠️ Signal only — not a guarantee of a breakout.\n"
        f"{url}"
    )


def process():
    seen = set()

    for term in SEARCH_TERMS:
        try:
            pairs = fetch_pairs(term)
        except Exception as e:
            logging.error("API error for %s: %s", term, e)
            continue

        for p in pairs:
            if p.get("chainId") != CHAIN:
                continue

            address = p.get("pairAddress")
            if not address or address in seen:
                continue
            seen.add(address)

            try:
                save_observation(p)
                score, details = score_pair(p)
                if score < MIN_SCORE:
                    continue

                if should_alert(address, score):
                    msg = format_alert(p, score, details)
                    if send_telegram(msg):
                        mark_alert(address, score)
                        logging.info(
                            "ALERT %s score=%.1f", 
                            (p.get("baseToken") or {}).get("symbol", "?"),
                            score
                        )
            except Exception as e:
                logging.exception("Processing error: %s", e)


if __name__ == "__main__":
    logging.info("Starting Solana breakout scanner...")
    logging.info(
        "poll=%ss min_score=%s min_liquidity=$%s",
        POLL_SECONDS, MIN_SCORE, MIN_LIQUIDITY_USD
    )

    while True:
        started = time.time()
        process()
        elapsed = time.time() - started
        time.sleep(max(1, POLL_SECONDS - elapsed))
