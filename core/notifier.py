"""Telegram notifier — push events to your phone from paper or live mode.

Setup (3 minutes, one-time):

  1. On Telegram, message @BotFather and run:
        /newbot
     Pick a name (e.g. "rofl-trading-bot") and a username ending in "_bot".
     BotFather replies with your *bot token* — looks like:
        7891234567:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
     Save it.

  2. Start a chat with your new bot (search for its username) and send /start.

  3. Get your *chat ID* by messaging @userinfobot — it replies with a
     numeric ID like 123456789. (Or just visit
     https://api.telegram.org/bot<TOKEN>/getUpdates after sending /start
     to your bot and grep for `chat.id`.)

  4. Set two env vars before running the bot:
        TELEGRAM_BOT_TOKEN=7891234567:AAHxxxxx
        TELEGRAM_CHAT_ID=123456789

That's it. Messages are silent for routine events and noisy for trade
fills + errors. If either env var is missing the notifier is a no-op,
so the bot runs fine without Telegram set up.
"""
from __future__ import annotations

import logging
import os
import time
import threading

import requests


class Notifier:
    def __init__(self, mode: str = "paper", preset: str = "",
                 symbol: str = "", logger: logging.Logger | None = None):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.mode = mode
        self.preset = preset
        self.symbol = symbol
        self.log = logger or logging.getLogger("bot")
        self.enabled = bool(self.token and self.chat_id)
        # Rate-limit guard (Telegram allows ~1 msg/s per chat)
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._min_interval = 0.4
        if self.enabled:
            self.log.info(f"telegram notifier enabled (chat={self.chat_id})")
        else:
            self.log.info("telegram notifier disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)")

    # ---- low-level send ----------------------------------------------------
    def _send(self, text: str, silent: bool = False) -> None:
        if not self.enabled:
            return
        with self._lock:
            gap = time.time() - self._last_send
            if gap < self._min_interval:
                time.sleep(self._min_interval - gap)
            # Retry on 429: with several portfolio bots sharing one chat,
            # simultaneous sends (e.g. a regime flip hits all pairs at once)
            # can trip Telegram's per-chat limit. Honor retry_after.
            for attempt in range(3):
                try:
                    r = requests.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": text,
                            "parse_mode": "Markdown",
                            "disable_notification": silent,
                            "disable_web_page_preview": True,
                        },
                        timeout=5,
                    )
                    self._last_send = time.time()
                    if r.status_code == 200:
                        return
                    if r.status_code == 429:
                        try:
                            wait = float(r.json()["parameters"]["retry_after"])
                        except Exception:
                            wait = 2.0 * (attempt + 1)
                        time.sleep(min(wait, 15))
                        continue
                    self.log.warning(f"telegram send {r.status_code}: {r.text[:120]}")
                    return
                except Exception as e:
                    self.log.warning(f"telegram error: {e}")
                    return

    def _tag(self) -> str:
        m = "🟢 LIVE" if self.mode == "live" else "📝 PAPER"
        return f"{m} | {self.preset} | {self.symbol}"

    # ---- high-level event helpers -----------------------------------------
    def startup(self, equity: float, extra: dict | None = None) -> None:
        lines = [
            f"🚀 *Bot started*",
            f"`{self._tag()}`",
            f"equity: *{equity:.2f}* USDT",
        ]
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: `{v}`")
        self._send("\n".join(lines))

    def shutdown(self, trades: int, pnl: float, equity: float) -> None:
        self._send(
            f"🛑 *Bot stopped*\n"
            f"`{self._tag()}`\n"
            f"trades: *{trades}*  realised: *{pnl:+.2f}*  equity: *{equity:.2f}*"
        )

    def trade_open(self, side: int, qty: float, price: float,
                   sl: float, tp: float, notional: float,
                   risk: float, equity: float, regime: str | None = None) -> None:
        arrow = "🟩 LONG" if side == 1 else "🟥 SHORT"
        regime_str = f"\nregime: `{regime}`" if regime else ""
        self._send(
            f"{arrow} *OPEN*\n"
            f"`{self._tag()}`\n"
            f"qty: `{qty:.4f}` @ `{price:.4f}`\n"
            f"SL: `{sl:.4f}`  TP: `{tp:.4f}`\n"
            f"notional: `{notional:.2f}` ({notional/equity:.1f}x)\n"
            f"risk: `{risk*100:.2f}%`  equity: `{equity:.2f}`"
            f"{regime_str}"
        )

    def trade_close(self, side: int, qty: float, price: float, pnl: float,
                    reason: str, bars_held: int, equity: float,
                    pct: float) -> None:
        emoji = "✅" if pnl > 0 else "❌"
        side_str = "LONG" if side == 1 else "SHORT"
        self._send(
            f"{emoji} *CLOSE* {side_str} ({reason})\n"
            f"`{self._tag()}`\n"
            f"qty: `{qty:.4f}` @ `{price:.4f}`\n"
            f"P&L: *{pnl:+.3f}* ({pct:+.2f}%)\n"
            f"held: `{bars_held}` bars\n"
            f"equity: `{equity:.2f}`"
        )

    def daily_summary(self, equity: float, day_pnl: float, day_trades: int,
                      total_trades: int, total_pnl: float,
                      win_rate: float, current_dd: float,
                      regime: str | None = None) -> None:
        regime_str = f"\nregime: `{regime}`" if regime else ""
        # Silent unless there was meaningful activity
        silent = (day_trades == 0)
        self._send(
            f"📊 *Daily Summary*\n"
            f"`{self._tag()}`\n"
            f"equity: *{equity:.2f}* USDT\n"
            f"today: *{day_pnl:+.2f}* ({day_trades} trades)\n"
            f"all-time: *{total_pnl:+.2f}* ({total_trades} trades, "
            f"{win_rate*100:.0f}% win)\n"
            f"current DD: `{current_dd*100:+.1f}%`"
            f"{regime_str}",
            silent=silent,
        )

    def error(self, msg: str) -> None:
        self._send(f"⚠️ *Error*\n`{self._tag()}`\n```\n{msg[:500]}\n```")

    def regime_change(self, old: str, new: str, equity: float) -> None:
        emoji = {"BULL": "🐂", "BEAR": "🐻", "CHOP": "↔️"}.get(new, "❓")
        self._send(
            f"{emoji} *Regime change*: `{old}` → `{new}`\n"
            f"`{self._tag()}`\n"
            f"equity: `{equity:.2f}`",
            silent=True,
        )
