"""Every notifier call site must match the Notifier signature.

2026-08-06, live: `_adopt_pending_fill` called `notifier.trade_open(...,
symbol=...)` but `Notifier.trade_open` has no `symbol` parameter. It raised
TypeError, was swallowed by the surrounding try/except (correct — logging must
never crash the bot), and logged one WARNING line.

The consequence was invisible and total: the adopt path is what runs on every
MAKER fill, and ENTRY_LIMIT_ORDERS=1 makes every entry a maker fill. So the
deployed config would never have sent a single Telegram open alert, while the
bot traded normally. Monitoring silently absent is worse than monitoring
obviously broken.

These tests bind every notifier call in bot.py against the real signature, so
a mismatched kwarg fails here instead of in production.

Run:  ./.venv/bin/python test_notifier_signatures.py
"""
import ast
import inspect
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="rofl_notif_")
os.environ.update({"MODE": "paper", "STATE_FILE": os.path.join(_TMP, "s.json"),
                   "LOG_FILE": os.path.join(_TMP, "b.log")})

from core.notifier import Notifier  # noqa: E402

WATCHED = ("trade_open", "trade_close", "error", "daily_summary",
           "regime_change", "bot_start", "bot_stop")


def _call_sites(path="bot.py"):
    """Every `self.notifier.<method>(...)` call with its keyword names."""
    tree = ast.parse(open(path).read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Attribute)
                and f.value.attr == "notifier"):
            continue
        kwargs = [k.arg for k in node.keywords if k.arg]
        out.append((f.attr, kwargs, node.lineno))
    return out


def test_every_call_site_binds():
    sites = _call_sites()
    assert sites, "no notifier call sites found — did bot.py move?"
    failures = []
    for method, kwargs, lineno in sites:
        fn = getattr(Notifier, method, None)
        if fn is None:
            failures.append(f"bot.py:{lineno} Notifier has no method {method!r}")
            continue
        sig = inspect.signature(fn)
        accepted = set(sig.parameters) - {"self"}
        has_kwargs = any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values())
        stray = set(kwargs) - accepted
        if stray and not has_kwargs:
            failures.append(
                f"bot.py:{lineno} notifier.{method}() got {sorted(stray)} "
                f"which the signature does not accept {sorted(accepted)}")
    assert not failures, "notifier call-site mismatches:\n  " + "\n  ".join(failures)
    print(f"PASS all {len(sites)} notifier call sites bind against Notifier")


def test_trade_open_specifically():
    """The one that actually broke — pin it."""
    sig = inspect.signature(Notifier.trade_open)
    accepted = set(sig.parameters) - {"self"}
    assert "symbol" not in accepted, (
        "if trade_open gains a symbol param, update the comment in "
        "_adopt_pending_fill — the symbol already reaches Telegram via _tag()")
    for site_method, kwargs, lineno in _call_sites():
        if site_method != "trade_open":
            continue
        stray = set(kwargs) - accepted
        assert not stray, f"bot.py:{lineno} trade_open stray kwargs {sorted(stray)}"
    print("PASS trade_open        both call sites bind (maker-adopt + market)")


def test_notifier_is_callable_with_real_kwargs():
    """Actually invoke it with a disabled notifier — no network, no token."""
    # Notifier reads its token/chat_id from env; with neither set it is
    # disabled and _send is a no-op, so this touches no network.
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_CHAT_ID", None)
    n = Notifier(mode="live", preset="adaptive_bidir_4h", symbol="ADA/USDT")
    assert not n.enabled, "test must run with the notifier disabled"
    n.trade_open(side=1, qty=260.0, price=0.1923, sl=0.184, tp=0.2201,
                 notional=50.0, risk=0.0193, equity=112.20, regime="BULL")
    n.trade_close(side=1, qty=260.0, price=0.2201, pnl=7.2, reason="tp",
                  bars_held=6, equity=119.4, pct=14.4)
    print("PASS live invocation   trade_open/trade_close accept the real kwargs")


if __name__ == "__main__":
    test_every_call_site_binds()
    test_trade_open_specifically()
    test_notifier_is_callable_with_real_kwargs()
    print("\nAll notifier signature tests passed.")
