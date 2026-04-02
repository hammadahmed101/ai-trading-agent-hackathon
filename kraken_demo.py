"""
Harold's Eyes — Data Verification Script
========================================
Run this to see the exact market data and calculated indicators
that are sent to the AI before it makes a decision.
"""

import requests
import json
import time

SYMBOL_API = "XBTUSD"

# ─── MARKET DATA FETCHERS ──────────────────────────────────────────────────────
def fetch_ticker() -> dict:
    """Gets latest price + 24h high/low from Kraken REST API."""
    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": SYMBOL_API},
            timeout=10,
        )
        r.raise_for_status()
        t = list(r.json()["result"].values())[0]
        return {
            "price":  float(t["c"][0]),
            "high24": float(t["h"][1]),
            "low24":  float(t["l"][1]),
        }
    except Exception as e:
        print(f"❌ Ticker fetch failed: {e}")
        return {"price": 0.0, "high24": 0.0, "low24": 0.0}

def fetch_ohlc_with_retry(interval: int = 1, max_attempts: int = 3) -> list:
    """Fetches 1-minute OHLC candles with retry logic."""
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": SYMBOL_API, "interval": interval},
                timeout=15,
            )
            r.raise_for_status()
            candles = r.json()["result"].get("XXBTZUSD", [])
            if candles:
                return candles
            print(f"⚠️ OHLC returned empty list (attempt {attempt}/{max_attempts})")
        except Exception as e:
            print(f"⚠️ OHLC fetch attempt {attempt}/{max_attempts} failed: {e}")
        
        if attempt < max_attempts:
            time.sleep(2 ** attempt)  
    
    print("❌ All OHLC fetch attempts failed.")
    return []

# ─── SIGNAL ENGINE ─────────────────────────────────────────────────────────────
def build_signals(ticker: dict, ohlc: list) -> dict:
    """Calculates technical indicators for 1-minute charts."""
    price = ticker["price"]

    if not ohlc or price == 0.0:
        return {"price": price, "error": "No candle data available"}

    closes  = [float(c[4]) for c in ohlc]
    volumes = [float(c[6]) for c in ohlc]

    # Fast Moving Averages
    sma_10m = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    sma_30m = sum(closes[-30:]) / 30 if len(closes) >= 30 else None

    # Momentum: count up vs down moves over last 5 candles
    recent = closes[-5:]
    ups   = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
    downs = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])
    momentum = "up" if ups >= 3 else ("down" if downs >= 3 else "flat")

    # Volume spike: last candle vs 20-candle average
    last_vol = volumes[-1] if volumes else 0
    avg_vol  = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1
    vol_spike = bool(last_vol > avg_vol * 1.5)

    # Last 5 closes as price history for the AI prompt
    recent_closes = [round(c, 2) for c in closes[-5:]]

    return {
        "price":          price,
        "sma_10m":        round(sma_10m, 2) if sma_10m else None,
        "sma_30m":        round(sma_30m, 2) if sma_30m else None,
        "above_sma_10m":  bool(sma_10m and price > sma_10m),
        "above_sma_30m":  bool(sma_30m and price > sma_30m),
        "momentum":       momentum,
        "volume_spike":   vol_spike,
        "recent_closes":  recent_closes,
    }

# ─── EXECUTION ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Fetching live market data from Kraken...")
    
    ticker = fetch_ticker()
    print(f"✅ Ticker fetched. Current Price: ${ticker['price']:,.2f}")
    
    ohlc = fetch_ohlc_with_retry()
    print(f"✅ OHLC fetched. Total candles: {len(ohlc)}")
    
    if ticker["price"] > 0 and ohlc:
        signals = build_signals(ticker, ohlc)
        
        print("\n" + "━"*50)
        print(" EXACT JSON PAYLOAD FED TO GROQ AI")
        print("━"*50)
        # We use json.dumps with an indent of 4 to make it highly readable in the terminal
        print(json.dumps(signals, indent=4))
        print("━"*50 + "\n")
    else:
        print("❌ Failed to gather enough data to build signals.")