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

    "state_file": "scanner_state.json",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scanner")


# Data structures, session helpers, calculate_atr, calculate_quality_score, 
# detect_bearish_setup, detect_bullish_setup remain the same as previous version.
# (Paste them from the last full code I gave you)

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


# (Include all helper functions: get_session_name, find_previous_session_extremes, calculate_atr, calculate_quality_score,
# detect_bearish_setup, detect_bullish_setup from the previous complete response)

def build_exchange() -> ccxt.Exchange:
    ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "future"}})
    ex.load_markets()
    return ex


def get_usdt_pairs(ex: ccxt.Exchange, cfg: dict) -> List[str]:
    """Try to get all futures pairs with volume threshold, fallback to good list"""
    try:
        tickers = ex.fetch_tickers()
        if tickers:
            ranked = []
            for sym, t in tickers.items():
                m = ex.markets.get(sym)
                if m and m.get("active") and m.get("quote") == "USDT" and m.get("type") == "swap":
                    vol = float(t.get("quoteVolume") or 0)
                    if vol >= cfg["min_volume_threshold"]:
                        ranked.append((sym, vol))
            if ranked:
                ranked.sort(key=lambda x: x[1], reverse=True)
                symbols = [s for s, _ in ranked]
                log.info("Loaded %d futures pairs above $%d volume", len(symbols), cfg["min_volume_threshold"]//1_000_000)
                return symbols[:300]  # cap for performance
    except Exception as e:
        log.error("Ticker fetch failed: %s. Using expanded fallback list.", e)

    # Expanded high-volume fallback
    fallback = [
        "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT",
        "BNB/USDT:USDT", "DOGE/USDT:USDT", "TON/USDT:USDT", "ADA/USDT:USDT",
        "AVAX/USDT:USDT", "TRX/USDT:USDT", "SHIB/USDT:USDT", "LINK/USDT:USDT",
        "SUI/USDT:USDT", "NEAR/USDT:USDT", "HBAR/USDT:USDT"
    ]
    log.info("Using fallback list of %d major futures pairs", len(fallback))
    return fallback


# Rest of the code (fetch_candles, send_telegram, format_signal_message, load_state, save_state, signal_key, scan_once, main) 
# — same as the last working version I provided.

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


# load_state, save_state, signal_key, scan_once, main functions (copy from previous full code)

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
            if not candles: continue

            for detector, direction in [(detect_bullish_setup, "bullish"), (detect_bearish_setup, "bearish")]:
                sig = detector(candles, cfg)
                if not sig: continue

                key = signal_key(symbol, tf, direction)
                if state.get(key) == sig.entry_bar_ts: continue

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
