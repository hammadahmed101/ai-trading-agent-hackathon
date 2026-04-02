"""
Harold — AI Trading Agent v3 (Kraken CLI v2.3.0 + Groq)
=========================================================
Hackathon Edition — All v2 bugs fixed, live status monitor,
balance-aware position sizing, and race-condition-free execution.

Requirements:
    pip3 install python-dotenv groq requests
"""

import os
import time
import json
import logging
import subprocess
import requests
import csv
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq

# ─── CONFIG ────────────────────────────────────────────────────────────────────
load_dotenv()

GROQ_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_KEY:
    raise ValueError("Missing GROQ_API_KEY in .env file.")

groq_client = Groq(api_key=GROQ_KEY)

# Symbol Management: CLI and REST API use different strings
SYMBOL_CLI = "BTCUSD"    # Used for Kraken CLI execution
SYMBOL_API = "XBTUSD"   # Used for Kraken REST API data fetching

# Safety net percentages
TAKE_PROFIT = 0.80     # Close trade at +1.5% PnL
STOP_LOSS   = -0.4     # Close trade at -1.2% PnL

# Timing
AI_CYCLE_SEC     = 120   # How often Groq makes a decision
MONITOR_SEC      = 20    # How often the status monitor ticks

# Files
STATE_FILE = "harold_state.json"
CSV_FILE   = "trades_log.csv"

# ─── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("harold")

# ─── STATE MANAGEMENT ──────────────────────────────────────────────────────────
def load_state() -> dict | None:
    """Loads the active position from JSON to survive crashes (amnesia fix)."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Failed to load state file: {e}")
    return None

def save_state(position: dict | None):
    """Saves active position to JSON, or removes the file when flat."""
    if position is None:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    else:
        with open(STATE_FILE, "w") as f:
            json.dump(position, f, indent=2)

# ─── CSV LEDGER ────────────────────────────────────────────────────────────────
def log_trade_to_csv(action: str, price: float, size: float, reasoning: str):
    """Appends every trade to CSV for post-hackathon analysis."""
    file_exists = os.path.isfile(CSV_FILE)
    try:
        with open(CSV_FILE, mode="a", newline="") as f:
            w = csv.writer(f)
            if not file_exists:
                w.writerow(["Timestamp", "Action", "Price", "Size", "Total_Value", "Reasoning"])
            w.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                action.upper(),
                f"${price:,.2f}",
                size,
                f"${price * size:,.2f}",
                reasoning,
            ])
    except Exception as e:
        log.error(f"CSV write failed: {e}")

# ─── KRAKEN CLI EXECUTOR ───────────────────────────────────────────────────────
def kraken_run(args: list) -> dict:
    """Runs a Kraken CLI command and safely parses JSON output."""
    cmd = ["kraken", "-o", "json"] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            log.error(f"CLI Error [{' '.join(args)}]: {r.stderr.strip()}")
            return {}
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        log.error(f"CLI JSON parse error: {e}")
        return {}
    except Exception as e:
        log.error(f"CLI Exception: {e}")
        return {}

def paper_buy(symbol: str, size: float) -> dict:
    return kraken_run(["paper", "buy", symbol, str(size)])

def paper_sell(symbol: str, size: float) -> dict:
    return kraken_run(["paper", "sell", symbol, str(size)])

def paper_status() -> dict:
    return kraken_run(["paper", "status"])

# ─── BALANCE HELPERS ───────────────────────────────────────────────────────────
def get_current_value() -> float:
    """
    Fetches `current_value` from paper status.
    This is total portfolio value (USD + BTC marked to market).
    Returns 0.0 on failure so callers can detect it.
    """
    status = paper_status()
    if not status:
        return 0.0
    return float(status.get("current_value", 0.0))

def get_available_usd(position: dict | None, current_price: float) -> float:
    """
    Calculates spendable USD by subtracting the estimated BTC holding
    value from total current_value. When flat, current_value IS the USD.

    Formula:
        available_usd = current_value - (btc_size * current_price)

    If position is None (flat), available_usd == current_value.
    """
    current_value = get_current_value()
    if current_value == 0.0:
        return 0.0

    if position is None:
        return current_value

    btc_held_value = position["size"] * current_price
    available = current_value - btc_held_value

    # Clamp to 0 to avoid negative from floating point drift
    return max(0.0, available)

# ─── MARKET DATA ───────────────────────────────────────────────────────────────
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
        log.error(f"Ticker fetch failed: {e}")
        return {"price": 0.0, "high24": 0.0, "low24": 0.0}

def fetch_ohlc_with_retry(interval: int = 1, max_attempts: int = 3) -> list:
    """
    Fetches 1-minute OHLC candles with retry logic.
    One bad network blip won't send the AI a blank dataset anymore.
    """
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
            log.warning(f"OHLC returned empty list (attempt {attempt}/{max_attempts})")
        except Exception as e:
            log.warning(f"OHLC fetch attempt {attempt}/{max_attempts} failed: {e}")
        if attempt < max_attempts:
            time.sleep(2 ** attempt)  # Exponential backoff: 2s, 4s
    log.error("All OHLC fetch attempts failed. AI will receive no candle data.")
    return []

# ─── SIGNAL ENGINE ─────────────────────────────────────────────────────────────
def build_signals(ticker: dict, ohlc: list) -> dict:
    """
    Calculates technical indicators for 1-minute charts.
    Also returns last 5 closes as a price series for richer AI context.
    """
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
        "recent_closes":  recent_closes,   # ← NEW: gives AI actual trend context
    }

# ─── GROQ AI ───────────────────────────────────────────────────────────────────
def ask_groq(
    signals: dict,
    position: dict | None,
    available_usd: float,
    trade_history: list,
) -> dict:
    """
    Asks Groq to analyze the market and make a fully autonomous decision.
    Now includes recent trade history so the AI doesn't flip-flop blindly.
    """
    price = signals.get("price", 0)

    # Build position context
    if position:
        pnl = ((price - position["entry"]) / position["entry"]) * 100
        position_ctx = (
            f"OPEN POSITION:\n"
            f"  Entry Price : ${position['entry']:,.2f}\n"
            f"  BTC Size    : {position['size']}\n"
            f"  Current PnL : {pnl:+.2f}%"
        )
    else:
        position_ctx = "NO OPEN POSITION. Ready to enter."

    # Last 3 trades for memory context
    if trade_history:
        history_lines = "\n".join(
            f"  [{t['time']}] {t['action']} @ ${t['price']} — {t['reasoning']}"
            for t in trade_history[-3:]
        )
        history_ctx = f"RECENT TRADES:\n{history_lines}"
    else:
        history_ctx = "RECENT TRADES: None yet."

    prompt = f"""You are Harold, a fully autonomous AI crypto trading agent in a hackathon.
Your goal is to maximise total portfolio value.

━━━ MARKET DATA (1-Minute Candles) ━━━
{json.dumps(signals, indent=2)}

━━━ ACCOUNT ━━━
Available USD (spendable): ${available_usd:,.2f}
{position_ctx}

━━━ HISTORY ━━━
{history_ctx}

━━━ RULES ━━━
1. Full autonomy — you decide buy, sell, or hold every 60 seconds.
2. Use the 10m/30m SMAs and recent_closes trend to judge direction.
3. If BUY: choose amount_percent between 5 and 20 (% of available USD).
4. If OPEN POSITION and momentum turns negative, SELL to protect capital.
5. Do NOT blindly flip-flop. Check your recent trade history first.
6. Reasoning must be 10 words max.

━━━ RESPOND ONLY IN EXACT JSON — NO OTHER TEXT ━━━
{{"action": "buy"|"sell"|"hold", "amount_percent": <5-20>, "reasoning": "<10 words max>"}}"""

    for attempt in range(1, 4):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,   # Low = consistent JSON, no hallucinated keys
                max_tokens=100,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            err = str(e).lower()
            if ("429" in err or "rate limit" in err) and attempt < 3:
                wait = 5 * attempt
                log.warning(f"Groq rate limit. Retrying in {wait}s...")
                time.sleep(wait)
                continue
            log.error(f"Groq error (attempt {attempt}): {e}")
            return {"action": "hold", "amount_percent": 0, "reasoning": "API error"}

    return {"action": "hold", "amount_percent": 0, "reasoning": "Max retries hit"}

# ─── STATUS MONITOR ────────────────────────────────────────────────────────────
def log_status(ticker_price: float, position: dict | None, starting_balance: float):
    """
    Prints a one-line status every 5 seconds showing:
      - Current BTC price
      - Open position PnL (from harold_state.json entry price)
      - Total capital PnL (current_value vs starting_balance, live from Kraken)
    """
    current_value = get_current_value()
    total_pnl_usd = current_value - starting_balance
    total_pnl_pct = (total_pnl_usd / starting_balance) * 100 if starting_balance else 0

    if position:
        trade_pnl_pct = ((ticker_price - position["entry"]) / position["entry"]) * 100
        trade_pnl_usd = trade_pnl_pct / 100 * (position["size"] * position["entry"])
        position_str  = (
            f"| TRADE PnL: {trade_pnl_pct:+.2f}% (${trade_pnl_usd:+,.2f}) "
            f"[entry=${position['entry']:,.2f}, size={position['size']} BTC]"
        )
    else:
        position_str = "| NO OPEN TRADE"

    log.info(
        f"📡 PRICE: ${ticker_price:,.2f} "
        f"| CAPITAL: ${current_value:,.2f} ({total_pnl_pct:+.2f}%, ${total_pnl_usd:+,.2f}) "
        f"{position_str}"
    )

# ─── MAIN LOOP ─────────────────────────────────────────────────────────────────
def run():
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  HAROLD V3 — HACKATHON MODE ACTIVATED  ")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ── 1. Init / verify paper account ────────────────────────────────────────
    status = paper_status()
    if not status:
        log.info("No paper account found. Initialising with $10,000...")
        kraken_run(["paper", "init", "--balance", "10000"])
        status = paper_status()
        if not status:
            log.critical("Paper account init failed twice. Cannot continue. Exiting.")
            return

    starting_balance = float(status.get("starting_balance", 10000.0))
    log.info(f"Paper account confirmed. Starting balance: ${starting_balance:,.2f}")

    # ── 2. Recover crash state ─────────────────────────────────────────────────
    position = load_state()
    if position:
        log.info(f"💾 Recovered open trade from state file — entry=${position['entry']:,.2f}, size={position['size']} BTC")

    # ── 3. Runtime state ───────────────────────────────────────────────────────
    last_ai_time  = 0
    trade_history = []   # In-memory last 3 trades for AI context

    try:
        while True:
            now    = time.time()
            ticker = fetch_ticker()

            # If price fetch failed completely, wait and retry
            if ticker["price"] == 0.0:
                log.warning("Price fetch returned 0. Skipping this tick.")
                time.sleep(MONITOR_SEC)
                continue

            price = ticker["price"]

            # ── STATUS MONITOR (every 5 seconds) ──────────────────────────────
            log_status(price, position, starting_balance)

            # ── SAFETY MONITOR: SL / TP (every 5 seconds) ─────────────────────
            #
            # BUG FIX #6: We set a flag instead of modifying `position` mid-loop.
            # This prevents the AI block below from seeing a stale `position`
            # value in the same iteration after SL/TP fires.
            #
            sl_tp_fired = False

            if position:
                pnl = ((price - position["entry"]) / position["entry"]) * 100

                if pnl >= TAKE_PROFIT:
                    log.info(f"✅ TAKE PROFIT HIT at {pnl:+.2f}% — Selling {position['size']} BTC")
                    res = paper_sell(SYMBOL_CLI, position["size"])
                    if res:
                        log_trade_to_csv("SELL(TP)", price, position["size"], f"TP +{TAKE_PROFIT}%")
                        trade_history.append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "action": "SELL(TP)",
                            "price": f"{price:,.2f}",
                            "reasoning": f"TP +{TAKE_PROFIT}%",
                        })
                        position = None
                        save_state(None)
                        sl_tp_fired = True
                    else:
                        log.error("TP sell order failed on Kraken CLI.")

                elif pnl <= STOP_LOSS:
                    log.warning(f"🛑 STOP LOSS TRIGGERED at {pnl:+.2f}% — Selling {position['size']} BTC")
                    res = paper_sell(SYMBOL_CLI, position["size"])
                    if res:
                        log_trade_to_csv("SELL(SL)", price, position["size"], f"SL {STOP_LOSS}%")
                        trade_history.append({
                            "time": datetime.now().strftime("%H:%M:%S"),
                            "action": "SELL(SL)",
                            "price": f"{price:,.2f}",
                            "reasoning": f"SL {STOP_LOSS}%",
                        })
                        position = None
                        save_state(None)
                        sl_tp_fired = True
                    else:
                        log.error("SL sell order failed on Kraken CLI.")

            # ── AI DECISION CYCLE (every 60 seconds) ──────────────────────────
            #
            # BUG FIX #6 (continued): Skip the AI cycle this tick if SL/TP just
            # fired — `last_ai_time` was not updated, so AI runs cleanly next cycle.
            #
            if not sl_tp_fired and (now - last_ai_time >= AI_CYCLE_SEC):

                ohlc    = fetch_ohlc_with_retry()
                signals = build_signals(ticker, ohlc)

                log.info(
                    f"📊 AI CYCLE | ${signals['price']:,.2f} "
                    f"| SMA10: {signals.get('sma_10m')} "
                    f"| SMA30: {signals.get('sma_30m')} "
                    f"| Mom: {signals.get('momentum')} "
                    f"| VolSpike: {signals.get('volume_spike')}"
                )

                # BUG FIX #1 & #3: Recalculate available USD live before asking AI
                available_usd = get_available_usd(position, price)

                decision = ask_groq(signals, position, available_usd, trade_history)
                log.info(
                    f"🤖 Groq → {decision['action'].upper()} "
                    f"{decision.get('amount_percent', 0)}% | {decision['reasoning']}"
                )

                # ── EXECUTE: BUY ──────────────────────────────────────────────
                if decision["action"] == "buy" and position is None:
                    percent      = max(5.0, min(20.0, float(decision.get("amount_percent", 10))))
                    usd_to_spend = available_usd * (percent / 100)
                    trade_size   = round(usd_to_spend / price, 5)

                    log.info(f"🚀 BUY {trade_size} BTC (${usd_to_spend:,.2f} at {percent}% of ${available_usd:,.2f})")
                    res = paper_buy(SYMBOL_CLI, trade_size)

                    if res:
                        position = {"entry": price, "size": trade_size}
                        save_state(position)
                        log_trade_to_csv("BUY", price, trade_size, decision["reasoning"])
                        trade_history.append({
                            "time":      datetime.now().strftime("%H:%M:%S"),
                            "action":    "BUY",
                            "price":     f"{price:,.2f}",
                            "reasoning": decision["reasoning"],
                        })
                    else:
                        log.error("BUY order failed on Kraken CLI.")

                # ── EXECUTE: SELL (AI decision) ───────────────────────────────
                elif decision["action"] == "sell" and position is not None:
                    log.info(f"📉 AI SELL {position['size']} BTC at ${price:,.2f}")
                    res = paper_sell(SYMBOL_CLI, position["size"])

                    if res:
                        log_trade_to_csv("SELL(AI)", price, position["size"], decision["reasoning"])
                        trade_history.append({
                            "time":      datetime.now().strftime("%H:%M:%S"),
                            "action":    "SELL(AI)",
                            "price":     f"{price:,.2f}",
                            "reasoning": decision["reasoning"],
                        })
                        position = None
                        save_state(None)
                    else:
                        log.error("AI SELL order failed on Kraken CLI.")

                last_ai_time = now  # Reset AI timer after a clean cycle

            time.sleep(MONITOR_SEC)

    except KeyboardInterrupt:
        print("\n") # Add a clean line break in the terminal
        log.info("Manual shutdown received.")
        if position:
            while True:
                ans = input(f"⚠️ You have an open position ({position['size']} BTC). Close it now? (y/n): ").strip().lower()
                if ans == 'y':
                    log.info("Selling open position before exit...")
                    res = paper_sell(SYMBOL_CLI, position["size"])
                    if res:
                        log.info("Position closed successfully. State cleared.")
                        save_state(None)
                    else:
                        log.error("Failed to close position on Kraken CLI.")
                    break
                elif ans == 'n':
                    log.info("Leaving position open. State saved in harold_state.json for next run.")
                    break
                else:
                    print("Please enter 'y' or 'n'.")
        
        log.info("Harold signing off. Good luck in the hackathon!")

if __name__ == "__main__":
    run()