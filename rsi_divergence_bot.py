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
    "per_symbol_delay_seconds": 0.25,

    "min_volume_threshold": 2_000_000,
    "min_quality_score": 55,
    "sl_atr_mult": 1.0,
    "max_bars_between_sweep_and_bos": 40,
    "ob_lookback_max_bars": 12,
    "min_bos_displacement_atr_mult": 0.6,

    # Missing key that caused crash
    "state_file": "scanner_state.json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scanner")


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# SESSION & HELPERS
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# PATTERN DETECTION (Full Bearish + Bullish)
# ---------------------------------------------------------------------------

def detect_bearish_setup(candles: List[Candle], cfg: dict) -> Optional[Signal]:
    prev_high, _, session_name = find_previous_session_extremes(candles)
    if prev_high == 0:
        return None

    n = len(candles)
    sweep_idx = None
    for j in range(n-80, n):
        if candles[j].high > prev_high and candles[j].close < prev_high:
            sweep_idx = j
            break
    if sweep_idx is None:
        return None

    inducement_idx = None
    for j in range(sweep_idx + 1, n):
        if candles[j].high > max((c.high for c in candles[sweep_idx:j]), default=0):
            inducement_idx = j
            break
    if inducement_idx is None:
        return None

    bos_idx = None
    min_disp = calculate_atr(candles) * cfg["min_bos_displacement_atr_mult"]
    for j in range(inducement_idx + 1, n):
        if candles[j].close < inducement_idx - min_disp:   # improved
            bos_idx = j
            break
    if bos_idx is None:
        return None

    ob_candle = None
    lookback_start = max(inducement_idx, bos_idx - cfg["ob_lookback_max_bars"])
    for j in range(bos_idx - 1, lookback_start - 1, -1):
        if candles[j].close > candles[j].open:
            ob_candle = candles[j]
            break
    if ob_candle is None:
        return None

    ob_low, ob_high = ob_candle.low, ob_candle.high

    retraced = invalidated = False
    for j in range(bos_idx + 1, n):
        if candles[j].high > prev_high:
            invalidated = True
            break
        if ob_low <= candles[j].high and candles[j].low <= ob_high:
            retraced = True
    if invalidated or not retraced:
        return None

    atr = calculate_atr(candles)
    sig = Signal(
        direction="bearish",
        sweep_price=prev_high,
        bos_price=candles[inducement_idx].high,
        ob_low=ob_low,
        ob_high=ob_high,
        tp1=candles[bos_idx].close * 0.985,
        tp2=candles[bos_idx].close * 0.96,
        sl_price=prev_high + atr * cfg["sl_atr_mult"],
        entry_bar_ts=candles[-1].ts,
        current_price=candles[-1].close,
        session=session_name
    )
    sig.quality_score = calculate_quality_score(candles, sig, bos_idx)
    if sig.quality_score < cfg["min_quality_score"]:
        return None
    return sig


def detect_bullish_setup(candles: List[Candle], cfg: dict) -> Optional[Signal]:
    _, prev_low, session_name = find_previous_session_extremes(candles)
    if prev_low == 0:
        return None

    n = len(candles)
    sweep_idx = None
    for j in range(n-80, n):
        if candles[j].low < prev_low and candles[j].close > prev_low:
            sweep_idx = j
            break
    if sweep_idx is None:
        return None

    inducement_idx = None
    for j in range(sweep_idx + 1, n):
        if candles[j].low < min((c.low for c in candles[sweep_idx:j]), default=float('inf')):
            inducement_idx = j
            break
    if inducement_idx is None:
        return None

    bos_idx = None
    min_disp = calculate_atr(candles) * cfg["min_bos_displacement_atr_mult"]
    for j in range(inducement_idx + 1, n):
        if candles[j].close > inducement_idx + min_disp:
            bos_idx = j
            break
    if bos_idx is None:
        return None

    ob_candle = None
    lookback_start = max(inducement_idx, bos_idx - cfg["ob_lookback_max_bars"])
    for j in range(bos_idx - 1, lookback_start - 1, -1):
        if candles[j].close < candles[j].open:
            ob_candle = candles[j]
            break
    if ob_candle is None:
        return None

    ob_low, ob_high = ob_candle.low, ob_candle.high

    retraced = invalidated = False
    for j in range(bos_idx + 1, n):
        if candles[j].low < prev_low:
            invalidated = True
            break
        if ob_low <= candles[j].high and candles[j].low <= ob_high:
            retraced = True
    if invalidated or not retraced:
        return None

    atr = calculate_atr(candles)
    sig = Signal(
        direction="bullish",
        sweep_price=prev_low,
        bos_price=candles[inducement_idx].low,
        ob_low=ob_low,
        ob_high=ob_high,
        tp1=candles[bos_idx].close * 1.015,
        tp2=candles[bos_idx].close * 1.04,
        sl_price=prev_low - atr * cfg["sl_atr_mult"],
        entry_bar_ts=candles[-1].ts,
        current_price=candles[-1].close,
        session=session_name
    )
    sig.quality_score = calculate_quality_score(candles, sig, bos_idx)
    if sig.quality_score < cfg["min_quality_score"]:
        return None
    return sig


# ---------------------------------------------------------------------------
# EXCHANGE & UTILS
# ---------------------------------------------------------------------------

def build_exchange() -> ccxt.Exchange:
    ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "future"}})
    ex.load_markets()
    return ex


def get_usdt_pairs(ex: ccxt.Exchange, cfg: dict) -> List[str]:
    try:
        tickers = ex.fetch_tickers()
        if not tickers:
            raise ValueError("No tickers returned")
    except Exception as e:
        log.error("Ticker fetch failed: %s. Using fallback.", e)
        # Fallback: return some popular futures
        return ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

    ranked = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if m and m.get("active") and m.get("quote") == "USDT" and m.get("type") == "swap":
            vol = float(t.get("quoteVolume") or 0)
            if vol >= cfg["min_volume_threshold"]:
                ranked.append((sym, vol))

    ranked.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in ranked[:300]]  # safety cap


def fetch_candles(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[List[Candle]]:
    try:
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not raw or len(raw) < 100:
            return None
        return [Candle(r[0], r[1], r[2], r[3], r[4], r[5]) for r in raw]
    except Exception as e:
        log.debug(f"Fetch error {symbol}: {e}")
        return None


def send_telegram(cfg: dict, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage",
            json={"chat_id": cfg["telegram_chat_id"], "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error("Telegram error: %s", e)


def format_signal_message(symbol: str, tf: str, sig: Signal) -> str:
    arrow = "🔴 BEARISH" if sig.direction == "bearish" else "🟢 BULLISH"
    return f"""{arrow} Session Sweep
*Pair:* {symbol}
*TF:* {tf}
*Session:* {sig.session}
*Sweep:* {sig.sweep_price:.2f}
*OB:* {sig.ob_low:.2f} - {sig.ob_high:.2f}
*SL:* {sig.sl_price:.2f}
*TP1:* {sig.tp1:.2f}
*Quality:* {sig.quality_score:.1f}/100"""


# State functions (unchanged)
def load_state(path: str) -> Dict[str, int]:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_state(path: str, state: Dict[str, int]):
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("Save state error: %s", e)


def signal_key(symbol: str, tf: str, direction: str) -> str:
    return f"{symbol}|{tf}|{direction}"


def scan_once(ex, symbols, cfg, state):
    for symbol in symbols:
        for tf in cfg["timeframes"]:
            candles = fetch_candles(ex, symbol, tf, cfg["candle_limit"])
            if not candles:
                continue

            for detector, direction in [(detect_bullish_setup, "bullish"), (detect_bearish_setup, "bearish")]:
                sig = detector(candles, cfg)
                if not sig:
                    continue
                key = signal_key(symbol, tf, direction)
                if state.get(key) == sig.entry_bar_ts:
                    continue

                sig.symbol = symbol
                sig.timeframe = tf
                msg = format_signal_message(symbol, tf, sig)
                log.info("SIGNAL: %s %s %s", symbol, tf, direction)
                send_telegram(cfg, msg)

                state[key] = sig.entry_bar_ts
                save_state(cfg["state_file"], state)

            time.sleep(cfg["per_symbol_delay_seconds"])


def main():
    cfg = CONFIG
    ex = build_exchange()
    symbols = get_usdt_pairs(ex, cfg)
    log.info("Scanning %d futures pairs every 12h", len(symbols))

    state = load_state(cfg["state_file"])
    last_refresh = time.time()

    while True:
        start = time.time()
        if time.time() - last_refresh > cfg["symbol_refresh_interval_seconds"]:
            try:
                ex.load_markets(reload=True)
                symbols = get_usdt_pairs(ex, cfg)
                log.info("Refreshed %d pairs", len(symbols))
            except Exception as e:
                log.error("Refresh failed: %s", e)
            last_refresh = time.time()

        try:
            scan_once(ex, symbols, cfg, state)
        except Exception as e:
            log.exception("Scan error: %s", e)

        elapsed = time.time() - start
        sleep_for = max(60, cfg["poll_interval_seconds"] - elapsed)
        log.info("Cycle done in %.1fs, sleeping %.1fs", elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
