#!/usr/bin/env python3
"""
Crypto Fear & Greed Index Trading Bot for Bitkub
=================================================
Strategy:
  - Extreme Fear (0-24)   : Market BUY every day
  - Fear (25-46)          : Market BUY every other day
  - Neutral (47-54)       : No trade (HOLD)
  - Greed (55-74)         : Market SELL every other day
  - Extreme Greed (75-100): Market SELL every day

Reports daily summaries to Discord.
Secure secrets via environment variables (GitHub Secrets).
"""

import os
import sys
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Bitkub Python library (สำหรับ private endpoints ที่ต้อง sign)
from bitkub import Bitkub

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

# --- Bitkub credentials ---
BITKUB_API_KEY = os.getenv("BITKUB_API_KEY", "")
BITKUB_API_SECRET = os.getenv("BITKUB_API_SECRET", "")

# --- Discord webhook ---
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# --- Trading parameters ---
SYMBOL = "THB_BTC"                # Bitkub trading pair
TRADE_AMOUNT_THB = 10.0           # Fixed THB amount per order
STATE_FILE = Path("trade_state.json")

# --- Bitkub public REST API ---
BITKUB_PUBLIC_API = "https://api.bitkub.com"

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
# State management (for "every other day" logic)
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
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Fear & Greed Index (Alternative.me)
# ---------------------------------------------------------------------------
def get_fear_greed() -> tuple:
    """
    Fetch current Fear & Greed Index from Alternative.me.
    Returns (value: int, classification: str).
    """
    url = "https://api.alternative.me/fng/"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch Fear & Greed Index: {exc}")

    try:
        entry = data["data"][0]
        value = int(entry["value"])
        classification = entry["value_classification"]
    except (KeyError, IndexError, ValueError) as exc:
        raise RuntimeError(f"Unexpected F&G response: {data} ({exc})")

    logger.info("Fear & Greed Index: %s (%s)", value, classification)
    return value, classification


# ---------------------------------------------------------------------------
# Trading decision logic
# ---------------------------------------------------------------------------
def decide_action(index_value: int, state: dict) -> str:
    """
    Decide action: "BUY", "SELL", or "HOLD".
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
    last_side = state.get("last_trade_side")

    # --- Extreme Fear: 0-24 → BUY every day ---
    if 0 <= index_value <= 24:
        logger.info("Extreme Fear → BUY signal (daily)")
        return "BUY"

    # --- Fear: 25-46 → BUY every other day ---
    if 25 <= index_value <= 46:
        if last_side != "BUY" or days_since_last >= 2:
            logger.info("Fear → BUY signal (every other day)")
            return "BUY"
        logger.info("Fear → HOLD (already bought recently)")
        return "HOLD"

    # --- Neutral: 47-54 → HOLD ---
    if 47 <= index_value <= 54:
        logger.info("Neutral → HOLD")
        return "HOLD"

    # --- Greed: 55-74 → SELL every other day ---
    if 55 <= index_value <= 74:
        if last_side != "SELL" or days_since_last >= 2:
            logger.info("Greed → SELL signal (every other day)")
            return "SELL"
        logger.info("Greed → HOLD (already sold recently)")
        return "HOLD"

    # --- Extreme Greed: 75-100 → SELL every day ---
    if 75 <= index_value <= 100:
        logger.info("Extreme Greed → SELL signal (daily)")
        return "SELL"

    logger.warning("Index value out of expected range: %s", index_value)
    return "HOLD"


# ---------------------------------------------------------------------------
# Bitkub client & public API helpers
# ---------------------------------------------------------------------------
def create_bitkub_client() -> Bitkub:
    """Initialise authenticated Bitkub client."""
    if not BITKUB_API_KEY or not BITKUB_API_SECRET:
        raise ValueError(
            "BITKUB_API_KEY and BITKUB_API_SECRET must be set as environment variables."
        )
    return Bitkub(api_key=BITKUB_API_KEY, api_secret=BITKUB_API_SECRET)


def get_ticker_price(symbol: str) -> float:
    """
    Get latest market price for symbol via Bitkub public REST API (v3).
    ใช้ REST โดยตรงเพื่อหลีกเลี่ยงปัญหา endpoint ticker เก่าในไลบรารี
    """
    # ลอง v3 ก่อน ถ้าไม่ได้ fallback ไปตัวเก่า
    urls = [
        f"{BITKUB_PUBLIC_API}/api/v3/market/ticker?sym={symbol}",
        f"{BITKUB_PUBLIC_API}/api/market/ticker?sym={symbol}",
    ]

    last_error = None
    for url in urls:
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            last_error = exc
            logger.warning("Ticker request failed (%s): %s", url, exc)
            continue

        # กรณี API ตอบ error
        if isinstance(data, dict) and "error" in data and data.get("error") != 0:
            last_error = f"API error: {data}"
            continue

        # รูปแบบที่คาดหวัง: {"THB_BTC": {"last": "1234", ...}} หรือ list
        price = None
        if isinstance(data, dict):
            entry = data.get(symbol)
            if isinstance(entry, dict):
                price = entry.get("last")
        if price is None:
            last_error = f"No price found in response: {data}"
            continue

        try:
            return float(price)
        except (TypeError, ValueError) as exc:
            last_error = f"Invalid price value: {price} ({exc})"
            continue

    raise RuntimeError(f"Failed to fetch ticker for {symbol}: {last_error}")


def get_balances(client: Bitkub) -> dict:
    """Return wallet balances from authenticated Bitkub API."""
    resp = client.balances()
    if resp.get("error") != 0:
        raise RuntimeError(f"Failed to fetch balances: {resp}")
    return resp.get("result", {})


def _available(balances: dict, symbol: str) -> float:
    """Extract 'available' amount from balances dict safely."""
    try:
        return float(balances.get(symbol, {}).get("available", 0))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Trading (BUY / SELL)
# ---------------------------------------------------------------------------
def execute_buy(client: Bitkub, amount_thb: float) -> dict:
    """
    Place a market-like BUY order (uses current market price).
    """
    price = get_ticker_price(SYMBOL)
    if price <= 0:
        raise RuntimeError(f"Invalid price: {price}")

    amount_btc = amount_thb / price
    logger.info(
        "Placing BUY: %.2f THB ≈ %.8f BTC @ %.2f", amount_thb, amount_btc, price
    )

    try:
        resp = client.place_bid(
            sym=SYMBOL,
            amt=amount_btc,
            rat=price,
            typ="market",
        )
    except Exception as exc:
        raise RuntimeError(f"BUY exception: {exc}")

    if resp.get("error") != 0:
        raise RuntimeError(f"BUY order failed: {resp}")
    logger.info("BUY order response: %s", resp)
    return resp


def execute_sell(client: Bitkub, amount_thb: float) -> dict:
    """
    Place a market-like SELL order.
    ถ้าไม่มี BTC ในบัญชี → คืนค่า skip (ไม่ throw error)
    """
    balances = get_balances(client)
    available_btc = _available(balances, "BTC")

    if available_btc <= 0:
        logger.warning("No BTC available to sell — skipping SELL order.")
        return {"skipped": True, "reason": "no_btc_balance"}

    price = get_ticker_price(SYMBOL)
    if price <= 0:
        raise RuntimeError(f"Invalid price: {price}")

    amount_btc = amount_thb / price

    if available_btc < amount_btc:
        logger.warning(
            "Insufficient BTC: have %.8f, need %.8f. Selling all available.",
            available_btc,
            amount_btc,
        )
        amount_btc = available_btc

    logger.info(
        "Placing SELL: %.8f BTC @ %.2f (≈ %.2f THB)",
        amount_btc,
        price,
        amount_btc * price,
    )

    try:
        resp = client.place_ask(
            sym=SYMBOL,
            amt=amount_btc,
            rat=price,
            typ="market",
        )
    except Exception as exc:
        raise RuntimeError(f"SELL exception: {exc}")

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


def build_report(
    index_value: int,
    classification: str,
    action: str,
    order_result: dict | None,
    balances: dict | None,
) -> str:
    """Build a formatted report string."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⏸️"}.get(action, "❔")

    lines = [
        "**📊 Crypto Fear & Greed Trading Bot — Report**",
        f"**Time:** {now}",
        f"**Index:** {index_value} — {classification}",
        f"**Action:** {emoji} {action}",
        "",
    ]

    if order_result:
        if order_result.get("skipped"):
            lines.append(f"⚠️ Skipped: {order_result.get('reason')}")
        else:
            lines.append("**Order Result:**")
            lines.append(
                f"```json\n{json.dumps(order_result, indent=2, ensure_ascii=False)[:1500]}\n```"
            )
        lines.append("")

    if balances:
        btc_avail = _available(balances, "BTC")
        thb_avail = _available(balances, "THB")
        lines.append("**Balances After Trade:**")
        lines.append(f"- BTC: `{btc_avail:.8f}`")
        lines.append(f"- THB: `{thb_avail:,.2f}`")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
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

        # 3. Bitkub client
        client = create_bitkub_client()

        order_result = None
        balances_before = None
        balances_after = None

        # 4. Execute trade (if needed)
        if action in ("BUY", "SELL"):
            balances_before = get_balances(client)
            logger.info("Balances before trade: %s", balances_before)

            if action == "BUY":
                thb_bal = _available(balances_before, "THB")
                if thb_bal < TRADE_AMOUNT_THB:
                    raise RuntimeError(
                        f"Insufficient THB: {thb_bal:.2f} < {TRADE_AMOUNT_THB}"
                    )
                order_result = execute_buy(client, TRADE_AMOUNT_THB)

            elif action == "SELL":
                btc_bal = _available(balances_before, "BTC")
                if btc_bal <= 0:
                    logger.warning("No BTC to sell — treating as HOLD.")
                    action = "HOLD"
                    order_result = None
                else:
                    order_result = execute_sell(client, TRADE_AMOUNT_THB)

            # Update state — เฉพาะเมื่อเทรดจริง (ไม่ skip)
            traded = (
                action in ("BUY", "SELL")
                and order_result
                and not order_result.get("skipped")
            )
            if traded:
                today = datetime.now(timezone.utc).date().isoformat()
                state["last_trade_date"] = today
                state["last_trade_side"] = action
                state.setdefault("trades", []).append(
                    {
                        "date": today,
                        "index": index_value,
                        "classification": classification,
                        "action": action,
                        "amount_thb": TRADE_AMOUNT_THB,
                    }
                )
                save_state(state)

            balances_after = get_balances(client)
            logger.info("Balances after trade: %s", balances_after)

        # 5. Report
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
        error_msg = (
            f"**❌ Bot Error**\n```\n{exc}\n```\n"
            f"```\n{traceback.format_exc()[:1400]}\n```"
        )
        logger.error("Fatal error: %s", exc, exc_info=True)
        send_discord(error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()