"""Bybit V5 order-payload schema guard.

2026-08-06: the first live start placed ZERO orders. Every entry logged
    maker entry not placed (bybit {"retCode":10001,"retMsg":"Request parameter
    error."}) — postonly reject or API error; sweeping + skipping this bar
so the book looked merely quiet while being completely inert. Two faults in
`Exchange._attached_sltp_params`, both invisible to the existing suite because
nothing validated the payload against Bybit's schema:

  1. `tpSize` / `slSize` are NOT parameters of POST /v5/order/create — they
     belong to POST /v5/position/trading-stop. Bybit's documented `linear`
     Partial example carries tpslMode / tpOrderType / tpLimitPrice and no
     sizes. Unknown fields => 10001.
  2. Bybit V5 takes every numeric as a STRING. ccxt stringifies the fields it
     knows (price, qty, stopLoss, takeProfit) but forwards custom params raw,
     so `tpLimitPrice` went as a JSON float.

Both paths were affected — the maker limit entry AND the market entry used as
the ENTRY_LIMIT_ORDERS=0 fallback, so the documented fallback would have
failed too.

These tests build the real payload through ccxt with a dry transport that
raises before any network write, so nothing is ever sent and no key is needed.

Run:  ./.venv/bin/python test_order_params.py
"""
import json

import ccxt

SYMBOL = "AVAX/USDT:USDT"
# /v5/order/create accepts these; anything else is a 10001 risk.
FORBIDDEN = {"tpSize", "slSize"}


class _DryBybit(ccxt.bybit):
    """Captures the POST body and aborts before the request leaves."""
    captured: dict = {}

    def fetch(self, url, method="GET", headers=None, body=None):
        if method == "POST":
            _DryBybit.captured = {"url": url, "body": body}
            raise RuntimeError("INTERCEPTED")
        return super().fetch(url, method, headers, body)


def _ex():
    ex = _DryBybit({"apiKey": "dry", "secret": "dry",
                    "options": {"defaultType": "linear"}})
    ex.has["fetchCurrencies"] = False
    ex.load_markets()
    ex.options.update(enableUnifiedAccount=True, enableUnifiedMargin=False,
                      unifiedAccount=True)
    return ex


def _payload(ex, params, market=False):
    _DryBybit.captured = {}
    try:
        if market:
            ex.create_market_buy_order(SYMBOL, 4.5, params=dict(params))
        else:
            ex.create_limit_buy_order(SYMBOL, 4.5, 21.0, params=dict(params))
    except RuntimeError:
        pass
    return json.loads(_DryBybit.captured.get("body") or "{}")


def _bot_params(tp_limit=True):
    """Mirror of Exchange._attached_sltp_params for a long with SL+TP."""
    p = {"stopLoss": "19.5", "takeProfit": "26.0"}
    if tp_limit:
        p.update(tpslMode="Partial", tpOrderType="Limit", tpLimitPrice="26.0")
    return p


def _assert_schema(d, label):
    assert d, f"{label}: no payload captured"
    stray = FORBIDDEN & set(d)
    assert not stray, (f"{label}: {stray} are not /v5/order/create params "
                       f"(they belong to /v5/position/trading-stop) -> retCode 10001")
    numeric = {k: v for k, v in d.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert not numeric, (f"{label}: Bybit V5 wants strings, got numerics {numeric} "
                         f"-> retCode 10001")


def test_limit_entry_payload():
    ex = _ex()
    d = _payload(ex, {**_bot_params(), "postOnly": True})
    _assert_schema(d, "limit entry")
    assert d["timeInForce"] == "PostOnly", f"postOnly did not become PostOnly: {d}"
    assert d["orderType"] == "Limit"
    for k in ("tpslMode", "tpOrderType", "tpLimitPrice"):
        assert k in d, f"limit entry missing {k}"
    assert d["tpslMode"] == "Partial", "limit TP requires tpslMode=Partial"
    print("PASS limit entry      strings only, no tpSize/slSize, PostOnly set")


def test_market_entry_payload():
    """The ENTRY_LIMIT_ORDERS=0 fallback path — same bug, same guard."""
    ex = _ex()
    d = _payload(ex, _bot_params(), market=True)
    _assert_schema(d, "market entry")
    assert d["orderType"] == "Market"
    print("PASS market entry     strings only, no tpSize/slSize")


def test_plain_sltp_without_tp_limit():
    """TP_LIMIT_ORDERS=0: a plain conditional-market TP/SL attach."""
    ex = _ex()
    d = _payload(ex, _bot_params(tp_limit=False))
    _assert_schema(d, "plain sl/tp")
    assert "tpslMode" not in d and "tpLimitPrice" not in d
    assert d["stopLoss"] == "19.5" and d["takeProfit"] == "26"
    print("PASS plain SL/TP      no Partial-mode fields when TP_LIMIT_ORDERS=0")


def test_guard_catches_the_original_bug():
    """The guard must fail on the payload that actually shipped."""
    ex = _ex()
    broken = {**_bot_params(), "tpLimitPrice": 26.0, "tpSize": 4.5,
              "slSize": 4.5, "postOnly": True}
    d = _payload(ex, broken)
    try:
        _assert_schema(d, "the 2026-08-06 payload")
        raise AssertionError("guard did NOT catch the original bug")
    except AssertionError as e:
        if "did NOT catch" in str(e):
            raise
    print("PASS regression       the original 10001 payload is rejected")


if __name__ == "__main__":
    test_limit_entry_payload()
    test_market_entry_payload()
    test_plain_sltp_without_tp_limit()
    test_guard_catches_the_original_bug()
    print("\nAll Bybit order-param tests passed.")
