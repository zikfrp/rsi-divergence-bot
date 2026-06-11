import ccxt
import pandas as pd
import pandas_ta as ta
from scipy.signal import argrelextrema
import numpy as np
import time
import asyncio
from telegram import Bot
import datetime
from fastapi import FastAPI
import uvicorn
import threading

# ================== CONFIG ==================
TELEGRAM_TOKEN = "8864441483:AAGa3UpekRTIIBF6djF9wjRkNEhc8SmRK14"
TELEGRAM_CHAT_ID = 1405093484

SYMBOLS = ['XAU/USDT', 'EUR/USDT']   # KuCoin format
TIMEFRAMES = ['5m', '15m', '30m', '1h', '4h']

RSI_PERIOD = 14
LOOKBACK = 60
EXTREMA_ORDER = 5

bot = Bot(token=TELEGRAM_TOKEN)

app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "alive", "time": datetime.datetime.now().isoformat(), "exchange": "KuCoin"}

async def send_alert(message):
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        print(f"✅ Alert sent")
    except Exception as e:
        print(f"Telegram error: {e}")

def fetch_ohlcv(exchange, symbol, timeframe, limit=300):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"❌ Error fetching {symbol} {timeframe}: {str(e)[:150]}")
        return None

def detect_rsi_divergence(df, symbol, tf_name):
    if df is None or len(df) < LOOKBACK:
        return None, None
    try:
        close = df['close']
        rsi = ta.rsi(close, length=RSI_PERIOD)
        price = close.iloc[-LOOKBACK:].values
        rsi_vals = rsi.iloc[-LOOKBACK:].values

        max_idx = argrelextrema(price, np.greater, order=EXTREMA_ORDER)[0]
        min_idx = argrelextrema(price, np.less, order=EXTREMA_ORDER)[0]

        if len(min_idx) >= 2:
            p1, p2 = min_idx[-2:]
            if price[p2] < price[p1] and rsi_vals[p2] > rsi_vals[p1]:
                return "🟢 **Bullish RSI Divergence**", "Price LL | RSI HL"
        if len(max_idx) >= 2:
            p1, p2 = max_idx[-2:]
            if price[p2] > price[p1] and rsi_vals[p2] < rsi_vals[p1]:
                return "🔴 **Bearish RSI Divergence**", "Price HH | RSI LH"
    except Exception as e:
        print(f"Detection error {symbol} {tf_name}: {e}")
    return None, None

async def main():
    exchange = ccxt.kucoin({
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })
    
    print("🤖 RSI Divergence Bot Started (KuCoin)")

    last_alert_time = {}
    
    while True:
        for symbol in SYMBOLS:
            for tf in TIMEFRAMES:
                key = f"{symbol}_{tf}"
                df = fetch_ohlcv(exchange, symbol, tf)
                if df is not None:
                    signal, details = detect_rsi_divergence(df, symbol, tf)
                    if signal:
                        now = time.time()
                        if key not in last_alert_time or now - last_alert_time[key] > 3600:
                            message = f"""
<b>🚨 RSI Divergence Alert</b>

📊 <b>{symbol}</b> | {tf}
{signal}

{details}

🕒 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}
                            """
                            await send_alert(message.strip())
                            last_alert_time[key] = now
                            await asyncio.sleep(3)
        await asyncio.sleep(60)

def run_web_server():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_web_server, daemon=True)
    server_thread.start()
    print("🌐 Health check running on port 8000")
    asyncio.run(main())
