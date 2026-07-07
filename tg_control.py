"""Telegram control service — interactive Stats / Positions / Today / Health /
Reconcile buttons for the multi-pair portfolio.

A SINGLE lightweight process (one container) that:
  - long-polls Telegram getUpdates and answers inline-keyboard button presses,
  - reads every bot's state file (mounted read-only at STATES_DIR/<bot>/bot_state.json),
  - queries Bybit READ-ONLY for live equity / positions (for Positions + Reconcile).

It NEVER places orders — only read endpoints are called. The individual bots keep
sending their own fill/alert pushes (core/notifier.py); this service adds the
on-demand pull side that the outbound-only notifier can't do.

Env:
  TELEGRAM_BOT_TOKEN   bot token (same bot the notifier uses is fine)
  TELEGRAM_CHAT_ID     the ONLY chat allowed to drive the buttons (authorization)
  STATES_DIR           dir holding <bot>/bot_state.json + <bot>/heartbeat (default /states)
  EXCHANGE             default 'bybit'
  API_KEY / API_SECRET read-only key for live equity/positions (optional; falls
                       back to booked-only if absent/unreachable)
  TG_POLL_TIMEOUT      long-poll seconds (default 30)

Run:  python tg_control.py
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("tg_control")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
STATES_DIR = Path(os.getenv("STATES_DIR", "/states"))
EXCHANGE = os.getenv("EXCHANGE", "bybit").lower()
POLL_TIMEOUT = int(os.getenv("TG_POLL_TIMEOUT", "30"))
API = f"https://api.telegram.org/bot{TOKEN}"

# bot dir -> display order. Dirs that don't exist are skipped silently, so this
# works for every portfolio generation (extras are appended alphabetically).
# The 4h BLEND50_CONF stack uses <sym>-t (triple leg) / <sym>-p (pullback leg).
BOT_ORDER = ["btc-t", "btc-p", "eth-t", "eth-p", "sol-t", "sol-p",
             "xrp-t", "xrp-p", "doge-t", "doge-p", "ada-t", "ada-p",
             "link-t", "link-p", "avax-t", "avax-p",
             # legacy single-leg portfolios
             "inj", "sol", "ada", "eth", "link",
             "avax", "near", "aave", "grt", "rune", "doge"]

MENU = {"inline_keyboard": [
    [{"text": "📊 Stats", "callback_data": "stats"},
     {"text": "📈 Positions", "callback_data": "positions"}],
    [{"text": "🗓 Today", "callback_data": "today"},
     {"text": "❤️ Health", "callback_data": "health"}],
    [{"text": "🧮 Reconcile", "callback_data": "reconcile"},
     {"text": "🔄 Refresh menu", "callback_data": "menu"}],
]}


# --------------------------- Telegram API ----------------------------------
def tg(method: str, payload: dict, timeout: int = 15) -> dict:
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=timeout)
        return r.json()
    except Exception as e:
        log.warning(f"telegram {method} failed: {e}")
        return {}


def send(text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
               "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("sendMessage", payload)


def edit(chat_id, message_id, text: str, reply_markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
               "parse_mode": "Markdown", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg("editMessageText", payload)


# --------------------------- State reading ---------------------------------
def read_bots() -> list[dict]:
    """Read each bot's booked state from STATES_DIR/<bot>/bot_state.json."""
    bots = []
    found = {p.parent.name: p for p in STATES_DIR.glob("*/bot_state.json")}
    ordered = [b for b in BOT_ORDER if b in found] + \
              [b for b in sorted(found) if b not in BOT_ORDER]
    now = int(time.time())
    for name in ordered:
        try:
            d = json.loads(found[name].read_text())
        except Exception as e:
            log.warning(f"read {name} state failed: {e}")
            continue
        hb_path = found[name].parent / "heartbeat"
        hb_age = None
        try:
            hb_age = now - int(hb_path.read_text().strip())
        except Exception:
            try:
                hb_age = now - int(hb_path.stat().st_mtime)
            except Exception:
                hb_age = None
        eq = float(d.get("equity", 0.0))
        peak = float(d.get("equity_peak", eq) or eq)
        bots.append({
            "name": name.upper(),
            "symbol": f"{name.split('-')[0].upper()}/USDT",
            "equity": eq,
            "peak": peak,
            "dd": (eq / peak - 1) if peak > 0 else 0.0,
            "realised_pnl": float(d.get("realised_pnl", 0.0)),
            "realised_trades": int(d.get("realised_trades", 0)),
            "realised_wins": int(d.get("realised_wins", 0)),
            "day_start_equity": float(d.get("day_start_equity", eq) or eq),
            "position": d.get("position"),
            "last_bar_ts": int(d.get("last_bar_ts", 0)),
            "hb_age": hb_age,
        })
    return bots


# --------------------------- Bybit read-only -------------------------------
# The BLEND50_CONF live stack runs TWO accounts: main = triple book,
# sub-account = pullback book (two bots must never share a symbol on one
# account — netting + reconcile collide). API_KEY/API_SECRET -> main,
# API_KEY2/API_SECRET2 -> sub. Either may be absent (that account degrades
# to booked-only views).
_clients = None


def bybit_clients():
    """Lazy read-only ccxt clients, one per configured account. Returns [] if
    no keys; a transient init failure is retried on the next call."""
    global _clients
    if _clients is not None:
        return _clients
    built, failed = [], False
    for tag, key, sec in (
            ("main", os.getenv("API_KEY", ""), os.getenv("API_SECRET", "")),
            ("sub", os.getenv("API_KEY2", ""), os.getenv("API_SECRET2", ""))):
        if not (key and sec):
            continue
        try:
            import ccxt
            ex = ccxt.bybit({"apiKey": key, "secret": sec,
                             "enableRateLimit": True,
                             "options": {"defaultType": "swap",
                                         "recvWindow": 60000,
                                         "adjustForTimeDifference": True}})
            ex.load_time_difference()   # absorb clock skew (bybit-clock-skew)
            built.append((tag, ex))
        except Exception as e:
            log.warning(f"bybit {tag} client init failed (retry next call): {e}")
            failed = True
    if not failed:                       # cache only a complete build
        _clients = built
    return built


def real_wallet_equity():
    clients = bybit_clients()
    if not clients:
        return None
    total, got = 0.0, False
    for tag, ex in clients:
        try:
            wb = ex.private_get_v5_account_wallet_balance({"accountType": "UNIFIED"})
            total += float(wb["result"]["list"][0]["totalEquity"])
            got = True
        except Exception as e:
            log.warning(f"wallet balance ({tag}) failed: {e}")
    return total if got else None


def real_positions():
    clients = bybit_clients()
    if not clients:
        return None
    out, got = [], False
    for tag, ex in clients:
        try:
            resp = ex.private_get_v5_position_list({"category": "linear",
                                                    "settleCoin": "USDT"})
            got = True
            for p in resp["result"]["list"]:
                if float(p.get("size") or 0) == 0:
                    continue
                sym = p["symbol"] + (" ·sub" if tag == "sub" else "")
                out.append({"symbol": sym, "side": p["side"],
                            "size": float(p["size"]),
                            "entry": float(p.get("avgPrice") or 0),
                            "upnl": float(p.get("unrealisedPnl") or 0),
                            "sl": p.get("stopLoss") or "-",
                            "tp": p.get("takeProfit") or "-"})
        except Exception as e:
            log.warning(f"position list ({tag}) failed: {e}")
    return out if got else None


# --------------------------- Renderers -------------------------------------
def render_stats() -> str:
    bots = read_bots()
    if not bots:
        return f"⚠️ No bot state found under `{STATES_DIR}`."
    tot_eq = sum(b["equity"] for b in bots)
    tot_pnl = sum(b["realised_pnl"] for b in bots)
    tot_tr = sum(b["realised_trades"] for b in bots)
    tot_win = sum(b["realised_wins"] for b in bots)
    wr = (tot_win / tot_tr * 100) if tot_tr else 0.0
    lines = ["📊 *Portfolio Stats*",
             f"booked equity: *{tot_eq:.2f}* USDT   realised: *{tot_pnl:+.2f}*",
             f"trades: *{tot_tr}*   win rate: *{wr:.0f}%*"]
    real = real_wallet_equity()
    if real is not None:
        lines.append(f"real Bybit equity: *{real:.2f}*  (gap {tot_eq - real:+.2f})")
    lines.append("")
    lines.append("`bot      equity    realised    DD`")
    for b in bots:
        lines.append(f"`{b['name']:<7} {b['equity']:>7.2f}   {b['realised_pnl']:>+7.2f}"
                     f"   {b['dd']*100:>5.1f}%`")
    return "\n".join(lines)


def render_positions() -> str:
    bots = read_bots()
    live = real_positions()
    if live is not None:
        if not live:
            return "📈 *Positions*: FLAT — no open positions on the exchange."
        lines = ["📈 *Open Positions* (live from Bybit)"]
        for p in live:
            arrow = "🟩" if p["side"].lower() == "buy" else "🟥"
            lines.append(f"{arrow} *{p['symbol']}* {p['side']}  size `{p['size']}`\n"
                         f"   entry `{p['entry']}`  SL `{p['sl']}`  TP `{p['tp']}`\n"
                         f"   uPnL *{p['upnl']:+.3f}*")
        return "\n".join(lines)
    # fallback: booked positions from state
    open_b = [b for b in bots if b.get("position")]
    if not open_b:
        return "📈 *Positions* (booked): FLAT. (Bybit unreachable for live view.)"
    lines = ["📈 *Open Positions* (booked — Bybit unreachable)"]
    for b in open_b:
        p = b["position"]
        arrow = "🟩" if p["side"] == 1 else "🟥"
        lines.append(f"{arrow} *{b['symbol']}* qty `{p['qty']}`  entry `{p['entry_px']}`\n"
                     f"   SL `{p['sl']}`  TP `{p['tp']}`")
    return "\n".join(lines)


def render_today() -> str:
    bots = read_bots()
    if not bots:
        return "⚠️ No bot state found."
    day_pnl = sum(b["equity"] - b["day_start_equity"] for b in bots)
    tot_pnl = sum(b["realised_pnl"] for b in bots)
    tot_tr = sum(b["realised_trades"] for b in bots)
    lines = ["🗓 *Today / PnL*",
             f"today: *{day_pnl:+.2f}* USDT (since UTC midnight)",
             f"all-time realised: *{tot_pnl:+.2f}*  over *{tot_tr}* trades", "",
             "`bot      today      all-time`"]
    for b in bots:
        d = b["equity"] - b["day_start_equity"]
        lines.append(f"`{b['name']:<7} {d:>+7.2f}    {b['realised_pnl']:>+7.2f}`")
    return "\n".join(lines)


def render_health() -> str:
    bots = read_bots()
    if not bots:
        return "⚠️ No bot state found."
    now = int(time.time())
    lines = ["❤️ *Health* (heartbeat age; stale = hung/stopped)"]
    for b in bots:
        hb = b["hb_age"]
        if hb is None:
            tag, hb_s = "❓ no heartbeat", "?"
        elif hb < 300:
            tag, hb_s = "✅", f"{hb}s"
        elif hb < 1800:
            tag, hb_s = "⚠️ slow", f"{hb//60}m"
        else:
            tag, hb_s = "🛑 STALE", f"{hb//60}m"
        bar_age = (now - b["last_bar_ts"]) // 60 if b["last_bar_ts"] else None
        bar_s = f"{bar_age}m" if bar_age is not None else "?"
        lines.append(f"{tag} `{b['name']:<7}` hb {hb_s:<5} last bar {bar_s} ago")
    lines.append("\n_Note: an in-memory HALT isn't visible here — check logs if a bot "
                 "is alive but not trading._")
    return "\n".join(lines)


def render_reconcile() -> str:
    bots = read_bots()
    booked = sum(b["equity"] for b in bots)
    real = real_wallet_equity()
    pos = real_positions()
    lines = ["🧮 *Reconcile — books vs exchange*",
             f"booked total (sum of bot state): *{booked:.2f}* USDT"]
    if real is None:
        lines.append("real Bybit equity: _unavailable_ (no key / unreachable)")
    else:
        gap = booked - real
        flag = "  ✅" if abs(gap) < 1.0 else "  ⚠️"
        lines.append(f"real Bybit equity: *{real:.2f}*")
        lines.append(f"gap (booked − real): *{gap:+.2f}* USDT{flag}")
    if pos is not None:
        lines.append(f"open positions on exchange: *{len(pos)}*")
    lines.append("\n_Books drift above real from theoretical-fill rounding + funding; "
                 "the external-fill fix narrows it going forward._")
    return "\n".join(lines)


HANDLERS = {
    "stats": render_stats, "positions": render_positions, "today": render_today,
    "health": render_health, "reconcile": render_reconcile,
}


def menu_text() -> str:
    return ("🤖 *rofl control*\nPick a view:")


# --------------------------- Main loop -------------------------------------
def authorized(chat_id) -> bool:
    return CHAT_ID and str(chat_id) == str(CHAT_ID)


def handle_update(u: dict) -> None:
    # Button press
    cq = u.get("callback_query")
    if cq:
        chat = cq.get("message", {}).get("chat", {}).get("id")
        msg_id = cq.get("message", {}).get("message_id")
        data = cq.get("data", "")
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})  # stop spinner
        if not authorized(chat):
            return
        if data == "menu":
            edit(chat, msg_id, menu_text(), MENU)
            return
        fn = HANDLERS.get(data)
        if fn:
            try:
                text = fn()
            except Exception as e:
                text = f"⚠️ error rendering {data}: {e}"
            edit(chat, msg_id, text, MENU)
        return
    # Text command
    msg = u.get("message") or {}
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip().lower().lstrip("/")
    if not authorized(chat):
        return
    if text in ("start", "menu", "help"):
        send(menu_text(), MENU)
    elif text in HANDLERS:
        try:
            send(HANDLERS[text]())
        except Exception as e:
            send(f"⚠️ error: {e}")


def main() -> None:
    if not (TOKEN and CHAT_ID):
        log.error("TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID required; exiting.")
        return
    log.info(f"tg_control up. states={STATES_DIR} "
             f"bybit accounts={len(bybit_clients())}")
    send("🤖 *rofl control* online. Send /menu for buttons.", MENU)
    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(f"{API}/getUpdates", params=params,
                                timeout=POLL_TIMEOUT + 10).json()
            for u in resp.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    log.warning(f"handle_update error: {e}")
        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            log.warning(f"poll error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
