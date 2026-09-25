#!/usr/bin/env python3
"""
Crypto Fear & Greed Index Trading Bot for Bitkub
=================================================
Strategy:
  - Extreme Fear (0-24)   : Market BUY every day
  - Fear (25-46)          : Market BUY every other day
  - Neutral (47-54)       : No trade
  - Greed (55-74)         : Market SELL every other day
  - Extreme Greed (75-100): Market SELL every day

Reports daily/weekly/monthly/yearly summaries to Discord.
"""

import os
import sys
import json
import logging
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

# Bitkub Python library
from bitkub import Bitkub

# Fear & Greed Index wrapper
from fear_and_greed import FearAndGreedIndex

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

# --- Bitkub credentials (from environment variables / GitHub Secrets) ---
BITKUB_API_KEY = os.getenv("BITKUB_API_KEY", "")
BITKUB_API_SECRET = os.getenv("BITKUB_API_SECRET", "")

# --- Discord webhook ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# --- Trading parameters ---
SYMBOL = "THB_BTC"                # Bitkub trading pair
TRADE_AMOUNT_THB = 10.0           # Fixed THB amount per order
STATE_FILE = Path("trade_state.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management (to track day-gap logic)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    """Load persisted state (last trade date, etc.)."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not read state file, starting fresh.")
    return {"last_trade_date": None, "last_trade_side": None, "trades": []}


def save_state(state: dict) -> None:
    """Persist state to disk."""
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Fear & Greed Index
# ---------------------------------------------------------------------------
def get_fear_greed() -> tuple:
    """
    Fetch current Fear & Greed Index.
    Returns (value: int, classification: str).
    """
    fng = FearAndGreedIndex()
    value = fng.get_current_value()
    classification = fng.get_current_classification()
    logger.info("Fear & Greed Index: %s (%s)", value, classification)
    return int(value), classification


# ---------------------------------------------------------------------------
# Trading decision logic
# ---------------------------------------------------------------------------
def decide_action(index_value: int, state: dict) -> str:
    """
    Decide whether to BUY, SELL, or HOLD based on index value and day-gap rules.

    Returns one of: "BUY", "SELL", "HOLD"
    """
    today = datetime.now(timezone.utc).date()
    last_trade_date_str = state.get("last_trade_date")

    last_trade_date = None
    if last_trade_date_str:
        try:
            last_trade_date = datetime.strptime(last_trade_date_str, "%Y-%m-%d").date()
        except ValueError:
            pass

    days_since_last = (today - last_trade_date).days if last_trade_date else 999

    # --- Extreme Fear: 0-24 => BUY every day ---
    if 0 <= index_value <= 24:
        logger.info("Extreme Fear → BUY signal (daily)")
        return "BUY"

    # --- Fear: 25-46 => BUY every other day ---
    if 25 <= index_value <= 46:
        if days_since_last >= 2 or state.get("last_trade_side") != "BUY":
            logger.info("Fear → BUY signal (every other day)")
            return "BUY"
        logger.info("Fear → HOLD (already traded recently)")
        return "HOLD"

    # --- Neutral: 47-54 => HOLD ---
    if 47 <= index_value <= 54:
        logger.info("Neutral → HOLD")
        return "HOLD"

    # --- Greed: 55-74 => SELL every other day ---
    if 55 <= index_value <= 74:
        if days_since_last >= 2 or state.get("last_trade_side") != "SELL":
            logger.info("Greed → SELL signal (every other day)")
            return "SELL"
        logger.info("Greed → HOLD (already traded recently)")
        return "HOLD"

    # --- Extreme Greed: 75-100 => SELL every day ---
    if 75 <= index_value <= 100:
        logger.info("Extreme Greed → SELL signal (daily)")
        return "SELL"

    logger.warning("Index value out of expected range: %s", index_value)
    return "HOLD"


# ---------------------------------------------------------------------------
# Bitkub trading
# ---------------------------------------------------------------------------
def create_bitkub_client() -> Bitkub:
    """Initialise authenticated Bitkub client."""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        raise ValueError(
            "BITKUB_API_KEY and BITKUB_API_SECRET must be set as environment variables."
        )
    return Bitkub(api_key=BITKUB_API_KEY, api_secret=BITKUB_API_SECRET)


def get_balances(client: Bitkub) -> dict:
    """Return wallet balances."""
    resp = client.balances()
    if resp.get("error") != 0:
        raise RuntimeError(f"Failed to fetch balances: {resp}")
    return resp.get("result", {})


def get_ticker_price(client: Bitkub, symbol: str) -> float:
    """Get latest market price for symbol."""
    resp = client.ticker(sym=symbol)
    data = resp.get(symbol, {})
    price = data.get("last")
    if price is None:
        raise RuntimeError(f"No price found for {symbol}: {resp}")
    return float(price)


def execute_buy(client: Bitkub, amount_thb: float) -> dict:
    """
    Place a market BUY order.
    Bitkub uses place_bid for buy orders with rate and amount.
    For a market-like buy we fetch the current ask price and place at that rate.
    """
    price = get_ticker_price(client, SYMBOL)
    # Bitkub minimum order size checks
    amount_btc = amount_thb / price
    logger.info("Placing BUY: %s THB ≈ %.8f BTC @ %.2f", amount_thb, amount_btc, price)

    resp = client.place_bid(
        sym=SYMBOL,
        amt=amount_btc,
        rat=price,
        typ="market",   # market order
    )
    if resp.get("error") != 0:
        raise RuntimeError(f"BUY order failed: {resp}")
    logger.info("BUY order response: %s", resp)
    return resp


def execute_sell(client: Bitkub, amount_thb: float) -> dict:
    """
    Place a market SELL order.
    Sell a fixed THB worth of BTC at current market price.
    """
    price = get_ticker_price(client, SYMBOL)
    amount_btc = amount_thb / price

    balances = get_balances(client)
    available_btc = float(balances.get("BTC", 0))
    if available_btc < amount_btc:
        logger.warning(
            "Insufficient BTC balance: have %.8f, need %.8f. Selling max available.",
            available_btc,
            amount_btc,
        )
        amount_btc = available_btc
        if amount_btc <= 0:
            raise RuntimeError("No BTC available to sell.")

    logger.info("Placing SELL: %.8f BTC @ %.2f (≈ %.2f THB)", amount_btc, price, amount_btc * price)

    resp = client.place_ask(
        sym=SYMBOL,
        amt=amount_btc,
        rat=price,
        typ="market",
    )
    if resp.get("error") != 0:
        raise RuntimeError(f"SELL order failed: {resp}")
    logger.info("SELL order response: %s", resp)
    return resp


# ---------------------------------------------------------------------------
# Discord reporting
# ---------------------------------------------------------------------------
def send_discord(content: str) -> None:
    """Send a message to Discord via webhook."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("DISCORD_WEBHOOK_URL not set — skipping Discord notification.")
        return

    payload = {"content": content[:2000]}  # Discord limit
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        r.raise_for_status()
        logger.info("Discord notification sent.")
    except Exception as exc:
        logger.error("Failed to send Discord message: %s", exc)


def build_report(index_value: int, classification: str, action: str, order_result: dict | None, balances: dict | None) -> str:
    """Build a formatted report string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "**📊 Crypto Fear & Greed Trading Bot — Report**",
        f"**Time:** {now}",
        f"**Index:** {index_value} — {classification}",
        f"**Action:** {action}",
        "",
    ]

    if order_result:
        lines.append("**Order Result:**")
        lines.append(f"```json\n{json.dumps(order_result, indent=2, ensure_ascii=False)}\n```")
        lines.append("")

    if balances:
        btc_bal = balances.get("BTC", "N/A")
        thb_bal = balances.get("THB", "N/A")
        lines.append("**Balances After Trade:**")
        lines.append(f"- BTC: `{btc_bal}`")
        lines.append(f"- THB: `{thb_bal}`")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry-point
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("=" * 60)
    logger.info("Crypto Fear & Greed Trading Bot — START")
    logger.info("=" * 60)

    state = load_state()

    try:
        # 1. Fetch Fear & Greed Index
        index_value, classification = get_fear_greed()

        # 2. Decide action
        action = decide_action(index_value, state)

        # 3. Initialise Bitkub client
        client = create_bitkub_client()

        # 4. Execute trade if needed
        order_result = None
        balances_before = None
        balances_after = None

        if action in ("BUY", "SELL"):
            # Pre-trade balance check
            balances_before = get_balances(client)
            logger.info("Balances before trade: %s", balances_before)

            if action == "BUY":
                # Check THB balance
                thb_bal = float(balances_before.get("THB", 0))
                if thb_bal < TRADE_AMOUNT_THB:
                    raise RuntimeError(
                        f"Insufficient THB balance: {thb_bal:.2f} < {TRADE_AMOUNT_THB}"
                    )
                order_result = execute_buy(client, TRADE_AMOUNT_THB)

            elif action == "SELL":
                order_result = execute_sell(client, TRADE_AMOUNT_THB)

            # Update state
            today = datetime.now(timezone.utc).date().isoformat()
            state["last_trade_date"] = today
            state["last_trade_side"] = action
            state.setdefault("trades", []).append({
                "date": today,
                "index": index_value,
                "classification": classification,
                "action": action,
                "amount_thb": TRADE_AMOUNT_THB,
            })
            save_state(state)

            balances_after = get_balances(client)
            logger.info("Balances after trade: %s", balances_after)

        # 5. Build and send report
        report = build_report(
            index_value=index_value,
            classification=classification,
            action=action,
            order_result=order_result,
            balances=balances_after or balances_before,
        )
        send_discord(report)

        logger.info("Bot completed successfully.")

    except Exception as exc:
        error_msg = f"**❌ Bot Error**\n```\n{exc}\n```\n```\n{traceback.format_exc()[:1500]}\n```"
        logger.error("Fatal error: %s", exc, exc_info=True)
        send_discord(error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()