import os
import time
import json
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

import requests
import ccxt

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

CONFIG = {
    "telegram_bot_token": "8864441483:AAGa3UpekRTIIBF6djF9wjRkNEhc8SmRK14",
    "telegram_chat_id": "1405093484",

    "timeframes": ["15m", "1h", "4h"],
    "candle_limit": 400,
    "poll_interval_seconds": 12 * 3600,
    "symbol_refresh_interval_seconds": 6 * 3600,
    "per_symbol_delay_seconds": 0.3,

    "min_volume_threshold": 2_000_000,
    "min_quality_score": 55,
    "sl_atr_mult": 1.0,
    "max_bars_between_sweep_and_bos": 40,
    "ob_lookback_max_bars": 12,
    "min_bos_displacement_atr_mult": 0.6,

    "state_file": "scanner_state.json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scanner")


# Data structures and helper functions (same as before)
@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    symbol: str = ""
    timeframe: str = ""
    direction: str = ""
    sweep_price: float = 0.0
    bos_price: float = 0.0
    ob_low: float = 0.0
    ob_high: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    sl_price: float = 0.0
    quality_score: float = 0.0
    entry_bar_ts: int = 0
    current_price: float = 0.0
    session: str = ""


def get_session_name(dt: datetime) -> str:
    h = dt.hour
    if 0 <= h < 8: return "Asian"
    if 8 <= h < 13: return "London"
    return "New York"


def find_previous_session_extremes(candles: List[Candle]) -> Tuple[float, float, str]:
    if len(candles) < 100:
        return 0.0, 0.0, ""
    session_data: Dict[str, dict] = {}
    for c in candles:
        dt = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc)
        sess = get_session_name(dt)
        if sess not in session_data:
            session_data[sess] = {"high": c.high, "low": c.low}
        else:
            session_data[sess]["high"] = max(session_data[sess]["high"], c.high)
            session_data[sess]["low"] = min(session_data[sess]["low"], c.low)
    sessions = list(session_data.keys())
    if len(sessions) < 2:
        return 0.0, 0.0, ""
    prev = sessions[-2]
    return session_data[prev]["high"], session_data[prev]["low"], prev


def calculate_atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return candles[-1].high - candles[-1].low if candles else 0.0
    trs = [max(c.high - c.low, abs(c.high - candles[i-1].close), abs(c.low - candles[i-1].close))
           for i, c in enumerate(candles[1:], 1)]
    return sum(trs[-period:]) / period


def calculate_quality_score(candles: List[Candle], sig: Signal, bos_idx: int) -> float:
    score = 55.0
    displacement = abs(sig.bos_price - sig.sweep_price)
    atr = calculate_atr(candles[max(0, bos_idx-30):bos_idx+10])
    if atr > 0:
        score += min(30, (displacement / atr) * 10)
    for c in candles:
        if abs(c.low - sig.ob_low) < 1e-8:
            body = abs(c.close - c.open)
            rng = c.high - c.low
            if rng > 0:
                score += 15 * (body / rng)
            break
    return max(0, min(100, score))


# Pattern Detection (Bearish + Bullish) - unchanged from last fixed version
# ... (copy the full detect_bearish_setup and detect_bullish_setup from my previous message)

# (To save space here, paste the full detect_bullish_setup and detect_bearish_setup functions from the last complete response I gave you.)

# Exchange & Main functions
def build_exchange() -> ccxt.Exchange:
    ex = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "future"}
    })
    ex.load_markets()
    return ex


def get_usdt_pairs(ex: ccxt.Exchange, cfg: dict) -> List[str]:
    try:
        tickers = ex.fetch_tickers() or {}
    except Exception as e:
        log.error("Ticker fetch failed: %s. Using fallback popular pairs.", e)
        return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT"]

    ranked = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if m and m.get("active") and m.get("quote") == "USDT" and m.get("type") == "swap":
            vol = float(t.get("quoteVolume") or 0)
            if vol >= cfg["min_volume_threshold"]:
                ranked.append((sym, vol))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:250]]


def fetch_candles(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[List[Candle]]:
    try:
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not raw or len(raw) < 100:
            return None
        return [Candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in raw]
    except Exception as e:
        log.debug(f"Fetch error {symbol} {timeframe}: {e}")
        return None


# Telegram, State, Scan, Main (same as previous)
def send_telegram(cfg: dict, text: str) -> None:
    try:
        requests.post(f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
                      json={"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error("Telegram error: %s", e)


def format_signal_message(symbol: str, tf: str, sig: Signal) -> str:
    arrow = "🔴 BEARISH" if sig.direction == "bearish" else "🟢 BULLISH"
    return f"""{arrow} Session Sweep Setup
*Pair:* {symbol}
*TF:* {tf}
*Session:* {sig.session}
*Sweep:* {sig.sweep_price:.2f}
*OB:* {sig.ob_low:.2f} - {sig.ob_high:.2f}
*SL:* {sig.sl_price:.2f}
*TP1:* {sig.tp1:.2f}
*Quality:* {sig.quality_score:.1f}/100"""


# load_state, save_state, signal_key, scan_once, main functions - copy from my previous full response

if __name__ == "__main__":
    main()
