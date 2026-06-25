import os
import time
import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict

import requests
import ccxt

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

CONFIG = {
    # Telegram (your credentials)
    "telegram_bot_token": "8864441483:AAGa3UpekRTIIBF6djF9wjRkNEhc8SmRK14",
    "telegram_chat_id": "1405093484",

    # Timeframes to scan
    "timeframes": ["15m", "1h", "4h"],

    # How many candles of history to pull per scan
    "candle_limit": 300,

    # Fractal swing detection
    "fractal_strength": 2,

    # Pattern parameters
    "max_bars_between_sweep_and_bos": 30,
    "ob_lookback_max_bars": 10,
    "min_bos_displacement_atr_mult": 0.5,

    # New features
    "min_quality_score": 55,
    "sl_atr_mult": 1.0,
    "atr_period": 14,

    # State & filtering
    "state_file": "scanner_state.json",
    "quote_currency": "USDT",
    "symbol_excludes": ["UP/", "DOWN/", "BULL/", "BEAR/"],
    
    # CHANGED: Scan every 1 hour
    "poll_interval_seconds": 3600,
    
    "symbol_refresh_interval_seconds": 6 * 3600,
    "per_symbol_delay_seconds": 0.2,   # slightly higher for hourly runs
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
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
class Swing:
    index: int
    price: float
    kind: str  # "high" or "low"


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


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def calculate_atr(candles: List[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return candles[-1].high - candles[-1].low if candles else 0.0
    trs = []
    for i in range(1, len(candles)):
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i-1].close),
            abs(candles[i].low - candles[i-1].close)
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def find_swings(candles: List[Candle], strength: int) -> List[Swing]:
    swings: List[Swing] = []
    n = len(candles)
    for i in range(strength, n - strength):
        center = candles[i]
        left = candles[i - strength:i]
        right = candles[i + 1:i + strength + 1]

        if all(center.high > c.high for c in left + right):
            swings.append(Swing(index=i, price=center.high, kind="high"))
        if all(center.low < c.low for c in left + right):
            swings.append(Swing(index=i, price=center.low, kind="low"))
    return swings


def calculate_quality_score(candles: List[Candle], sig: Signal, bos_idx: int, sweep_idx: int) -> float:
    score = 50.0
    displacement = abs(sig.bos_price - sig.sweep_price)
    recent_atr = calculate_atr(candles[max(0, bos_idx-20):bos_idx+1])
    if recent_atr > 0:
        score += min(25, (displacement / recent_atr) * 8)

    # OB body strength
    for c in candles:
        if abs(c.low - sig.ob_low) < 1e-8 and abs(c.high - sig.ob_high) < 1e-8:
            body = abs(c.close - c.open)
            rng = c.high - c.low
            if rng > 0:
                score += 15 * (body / rng)
            break

    # Retracement quality
    impulse = abs(sig.bos_price - sig.sweep_price)
    if impulse > 0:
        retrace_pct = abs(sig.current_price - sig.bos_price) / impulse
        score += 10 * (1 - abs(retrace_pct - 0.5) * 1.5)

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# PATTERN DETECTION (Bullish + Bearish)
# ---------------------------------------------------------------------------

def detect_bullish_setup(candles: List[Candle], swings: List[Swing], cfg: dict) -> Optional[Signal]:
    lows = [s for s in swings if s.kind == "low"]
    highs = [s for s in swings if s.kind == "high"]
    if not lows or not highs:
        return None
    n = len(candles)

    for sweep_low in sorted(lows, key=lambda s: -s.index):
        sweep_idx = sweep_low.index
        sweep_confirm_idx = None
        for j in range(sweep_idx + 1, n):
            if candles[j].low < sweep_low.price and candles[j].close > sweep_low.price:
                sweep_confirm_idx = j
                break
        if sweep_confirm_idx is None:
            continue

        candidate_highs = [h for h in highs if sweep_confirm_idx < h.index <= sweep_confirm_idx + cfg["max_bars_between_sweep_and_bos"]]
        if not candidate_highs:
            continue
        inducement = min(candidate_highs, key=lambda h: h.index)

        avg_range = sum(c.high - c.low for c in candles[max(0, inducement.index - 10):inducement.index + 1]) / max(1, len(candles[max(0, inducement.index - 10):inducement.index + 1]))
        min_disp = avg_range * cfg["min_bos_displacement_atr_mult"]

        bos_idx = None
        for j in range(inducement.index + 1, n):
            if candles[j].close > inducement.price + min_disp:
                bos_idx = j
                break
        if bos_idx is None:
            continue

        ob_candle = None
        lookback_start = max(inducement.index, bos_idx - cfg["ob_lookback_max_bars"])
        for j in range(bos_idx - 1, lookback_start - 1, -1):
            if candles[j].close < candles[j].open:
                ob_candle = candles[j]
                break
        if ob_candle is None:
            continue

        ob_low, ob_high = ob_candle.low, ob_candle.high

        retraced = invalidated = False
        for j in range(bos_idx + 1, n):
            if candles[j].low < sweep_low.price:
                invalidated = True
                break
            if candles[j].low <= ob_high and candles[j].high >= ob_low:
                retraced = True
        if invalidated or not retraced:
            continue

        future_highs = sorted([h for h in highs if h.index > inducement.index and h.price > inducement.price], key=lambda h: h.price)
        if not future_highs:
            continue
        tp1 = future_highs[0].price
        tp2 = future_highs[1].price if len(future_highs) > 1 else tp1 * 1.01

        atr = calculate_atr(candles)
        sl_price = sweep_low.price - atr * cfg["sl_atr_mult"]

        sig = Signal(
            direction="bullish", sweep_price=sweep_low.price, bos_price=inducement.price,
            ob_low=ob_low, ob_high=ob_high, tp1=tp1, tp2=tp2, sl_price=sl_price,
            entry_bar_ts=candles[-1].ts, current_price=candles[-1].close
        )
        sig.quality_score = calculate_quality_score(candles, sig, bos_idx, sweep_idx)

        if sig.quality_score < cfg.get("min_quality_score", 50):
            continue

        return sig
    return None


def detect_bearish_setup(candles: List[Candle], swings: List[Swing], cfg: dict) -> Optional[Signal]:
    lows = [s for s in swings if s.kind == "low"]
    highs = [s for s in swings if s.kind == "high"]
    if not lows or not highs:
        return None
    n = len(candles)

    for sweep_high in sorted(highs, key=lambda s: -s.index):
        sweep_idx = sweep_high.index
        sweep_confirm_idx = None
        for j in range(sweep_idx + 1, n):
            if candles[j].high > sweep_high.price and candles[j].close < sweep_high.price:
                sweep_confirm_idx = j
                break
        if sweep_confirm_idx is None:
            continue

        candidate_lows = [l for l in lows if sweep_confirm_idx < l.index <= sweep_confirm_idx + cfg["max_bars_between_sweep_and_bos"]]
        if not candidate_lows:
            continue
        inducement = min(candidate_lows, key=lambda l: l.index)

        avg_range = sum(c.high - c.low for c in candles[max(0, inducement.index - 10):inducement.index + 1]) / max(1, len(candles[max(0, inducement.index - 10):inducement.index + 1]))
        min_disp = avg_range * cfg["min_bos_displacement_atr_mult"]

        bos_idx = None
        for j in range(inducement.index + 1, n):
            if candles[j].close < inducement.price - min_disp:
                bos_idx = j
                break
        if bos_idx is None:
            continue

        ob_candle = None
        lookback_start = max(inducement.index, bos_idx - cfg["ob_lookback_max_bars"])
        for j in range(bos_idx - 1, lookback_start - 1, -1):
            if candles[j].close > candles[j].open:
                ob_candle = candles[j]
                break
        if ob_candle is None:
            continue

        ob_low, ob_high = ob_candle.low, ob_candle.high

        retraced = invalidated = False
        for j in range(bos_idx + 1, n):
            if candles[j].high > sweep_high.price:
                invalidated = True
                break
            if candles[j].low <= ob_high and candles[j].high >= ob_low:
                retraced = True
        if invalidated or not retraced:
            continue

        future_lows = sorted([l for l in lows if l.index > inducement.index and l.price < inducement.price], key=lambda l: -l.price)
        if not future_lows:
            continue
        tp1 = future_lows[0].price
        tp2 = future_lows[1].price if len(future_lows) > 1 else tp1 * 0.99

        atr = calculate_atr(candles)
        sl_price = sweep_high.price + atr * cfg["sl_atr_mult"]

        sig = Signal(
            direction="bearish", sweep_price=sweep_high.price, bos_price=inducement.price,
            ob_low=ob_low, ob_high=ob_high, tp1=tp1, tp2=tp2, sl_price=sl_price,
            entry_bar_ts=candles[-1].ts, current_price=candles[-1].close
        )
        sig.quality_score = calculate_quality_score(candles, sig, bos_idx, sweep_idx)

        if sig.quality_score < cfg.get("min_quality_score", 50):
            continue

        return sig
    return None


# ---------------------------------------------------------------------------
# EXCHANGE, TELEGRAM, STATE, etc. (unchanged from previous version)
# ---------------------------------------------------------------------------

def build_exchange() -> ccxt.Exchange:
    ex = ccxt.mexc({"enableRateLimit": True})
    ex.load_markets()
    return ex


def get_usdt_pairs(ex: ccxt.Exchange, cfg: dict) -> List[str]:
    symbols = []
    for sym, market in ex.markets.items():
        if not market.get("active", True): continue
        if market.get("quote") != cfg["quote_currency"]: continue
        if market.get("type") not in ("spot", None): continue
        if any(bad in sym for bad in cfg["symbol_excludes"]): continue
        symbols.append(sym)
    return sorted(symbols)


def fetch_candles(ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[List[Candle]]:
    try:
        raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw or len(raw) < 50:
            return None
        return [Candle(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in raw]
    except Exception as e:
        log.debug(f"fetch failed for {symbol} {timeframe}: {e}")
        return None


def send_telegram(cfg: dict, text: str) -> None:
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log.error("Telegram error: %s", e)


def format_signal_message(symbol: str, timeframe: str, sig: Signal) -> str:
    arrow = "🟢 BULLISH" if sig.direction == "bullish" else "🔴 BEARISH"
    return (
        f"{arrow} setup detected\n"
        f"*Pair:* {symbol}\n"
        f"*Timeframe:* {timeframe}\n"
        f"*Sweep:* {sig.sweep_price:.6g}\n"
        f"*BOS:* {sig.bos_price:.6g}\n"
        f"*OB:* {sig.ob_low:.6g} - {sig.ob_high:.6g}\n"
        f"*SL:* {sig.sl_price:.6g}\n"
        f"*TP1:* {sig.tp1:.6g}\n"
        f"*TP2:* {sig.tp2:.6g}\n"
        f"*Quality:* {sig.quality_score:.1f}/100\n"
        f"*Price:* {sig.current_price:.6g}\n"
    )


def load_state(path: str) -> Dict[str, int]:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(path: str, state: Dict[str, int]) -> None:
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("State save failed: %s", e)


def signal_key(symbol: str, timeframe: str, direction: str) -> str:
    return f"{symbol}|{timeframe}|{direction}"


def get_higher_tf_bias(ex: ccxt.Exchange, symbol: str, tf: str) -> bool:
    higher_map = {"15m": "1h", "1h": "4h", "4h": None}
    higher_tf = higher_map.get(tf)
    if not higher_tf:
        return True
    higher_candles = fetch_candles(ex, symbol, higher_tf, 100)
    if not higher_candles:
        return True
    swings = find_swings(higher_candles, 3)
    lows = [s.price for s in swings if s.kind == "low"][-3:]
    return bool(lows and higher_candles[-1].close > max(lows) * 0.98) if lows else True


# ---------------------------------------------------------------------------
# SCAN LOOP
# ---------------------------------------------------------------------------

def scan_once(ex: ccxt.Exchange, symbols: List[str], cfg: dict, state: Dict[str, int]) -> None:
    for symbol in symbols:
        for tf in cfg["timeframes"]:
            candles = fetch_candles(ex, symbol, tf, cfg["candle_limit"])
            if not candles:
                continue

            swings = find_swings(candles, cfg["fractal_strength"])

            for detector, direction in ((detect_bullish_setup, "bullish"), (detect_bearish_setup, "bearish")):
                sig = detector(candles, swings, cfg)
                if sig is None:
                    continue

                if not get_higher_tf_bias(ex, symbol, tf):
                    continue

                key = signal_key(symbol, tf, direction)
                if state.get(key) == sig.entry_bar_ts:
                    continue

                sig.symbol = symbol
                sig.timeframe = tf
                msg = format_signal_message(symbol, tf, sig)
                log.info("SIGNAL: %s %s %s (Q=%.1f)", symbol, tf, direction, sig.quality_score)
                send_telegram(cfg, msg)

                state[key] = sig.entry_bar_ts
                save_state(cfg["state_file"], state)

            time.sleep(cfg["per_symbol_delay_seconds"])


def main():
    cfg = CONFIG
    ex = None
    while ex is None:
        try:
            ex = build_exchange()
        except Exception as e:
            log.error("Exchange connect failed: %s - retrying", e)
            time.sleep(30)

    symbols = get_usdt_pairs(ex, cfg)
    log.info("Scanning %d pairs every 1 hour", len(symbols))

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
        log.info("Cycle completed in %.1fs. Next scan in 1 hour.", elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
