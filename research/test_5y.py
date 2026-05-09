"""5-year backtest with year-by-year and crisis-period breakdown.

Crises captured (May 2021 - May 2026):
  - 2021-05  China mining ban         (BTC -50% in days)
  - 2021-11  Cycle top -> 2022 bear   (BTC -75% peak to trough)
  - 2022-05  Terra/Luna collapse      (UST de-peg)
  - 2022-11  FTX collapse             (counterparty contagion)
  - 2023-03  SVB / banking crisis     (BTC bottom)
  - 2024-08  Yen carry unwind         (sharp -20%)
  - 2025-04  Tariff shock             (sharp drawdown)
"""
from __future__ import annotations

import sys as _sys, os as _os
_THIS_DIR_PARENT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _THIS_DIR_PARENT not in _sys.path: _sys.path.insert(0, _THIS_DIR_PARENT)

import pandas as pd

from core.backtest import BTConfig, run_backtest
from core.data import fetch_ohlcv
from core.sentiment import fetch_fear_greed
from core.strategies import REGISTRY
from core.strategies_enhanced import with_htf_trend_filter
from core.strategies_sentiment import donchian_skip_fear

PARAMS = dict(entry_n=20, exit_n=10, adx_n=14, adx_min=20.0,
              atr_n=14, sl_mult=2.5, tp_mult=5.0)

CRISES = [
    ("China mining ban",    "2021-05-15", "2021-07-31"),
    ("2022 bear market",    "2021-11-08", "2022-12-31"),
    ("Terra/Luna",          "2022-05-01", "2022-06-15"),
    ("FTX collapse",        "2022-11-01", "2022-12-31"),
    ("SVB / banking crisis","2023-03-01", "2023-04-15"),
    ("Yen carry unwind",    "2024-08-01", "2024-08-15"),
    ("April-2025 tariffs",  "2025-04-01", "2025-04-30"),
]


def slice_period(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s = pd.to_datetime(start, utc=True)
    e = pd.to_datetime(end, utc=True)
    return df.loc[(df.index >= s) & (df.index <= e)]


def yearly_breakdown(df, sig, label):
    df_eq = df.join(sig, how="inner")
    res = run_backtest(df, sig, BTConfig())
    eq = res.equity_curve
    # Slice by year
    rows = []
    for year in sorted({d.year for d in eq.index}):
        sub = eq[eq.index.year == year]
        if len(sub) < 100:
            continue
        ret = sub.iloc[-1] / sub.iloc[0] - 1
        peak = sub.cummax()
        dd = (sub / peak - 1).min()
        rows.append({"year": year, "ret": round(ret, 4),
                     "mdd": round(float(dd), 4),
                     "end_eq": round(float(sub.iloc[-1]), 2)})
    return res, pd.DataFrame(rows)


def main():
    pd.set_option("display.width", 220)
    fng = fetch_fear_greed()

    # 5y data (KuCoin gives ~ since May 2021 for BTC/ETH)
    df_eth = fetch_ohlcv("ETH-USDT", "1h", days=5 * 365)
    df_btc = fetch_ohlcv("BTC-USDT", "1h", days=5 * 365)
    df_sol = fetch_ohlcv("SOL-USDT", "1h", days=5 * 365)

    print(f"Data ranges:")
    for sym, df in [("ETH", df_eth), ("BTC", df_btc), ("SOL", df_sol)]:
        print(f"  {sym}: {df.index[0].date()} -> {df.index[-1].date()}  ({len(df)} bars)")

    print("\n" + "=" * 80)
    print("STRATEGY SCAN ON 5-YEAR DATA")
    print("=" * 80)
    rows = []
    for pair_name, df in [("ETH-USDT", df_eth), ("BTC-USDT", df_btc),
                          ("SOL-USDT", df_sol)]:
        for sname, sc in REGISTRY.items():
            sig = sc.fn(df)
            res = run_backtest(df, sig, BTConfig(), long_only=sc.long_only)
            rows.append({"strategy": sname, "pair": pair_name, **res.stats()})
    out = pd.DataFrame(rows).sort_values("sharpe", ascending=False).reset_index(drop=True)
    print(out.to_string(index=False))

    print("\n" + "=" * 80)
    print("YEAR-BY-YEAR: Donchian + sentiment on each pair")
    print("=" * 80)
    for pair_name, df in [("ETH-USDT", df_eth), ("BTC-USDT", df_btc),
                          ("SOL-USDT", df_sol)]:
        sig = donchian_skip_fear(df, fng, fear_min=25, **PARAMS)
        res, yearly = yearly_breakdown(df, sig, pair_name)
        print(f"\n{pair_name} | overall: {res.stats()}")
        print(yearly.to_string(index=False))

    print("\n" + "=" * 80)
    print("CRISIS PERIODS: Donchian + sentiment on ETH-USDT")
    print("=" * 80)
    sig = donchian_skip_fear(df_eth, fng, fear_min=25, **PARAMS)
    full_res = run_backtest(df_eth, sig, BTConfig())
    eq = full_res.equity_curve
    print(f"Full 5y ETH stats: {full_res.stats()}")
    print()
    for label, start, end in CRISES:
        sub_eq = eq.loc[(eq.index >= pd.to_datetime(start, utc=True)) &
                        (eq.index <= pd.to_datetime(end, utc=True))]
        if len(sub_eq) < 24:
            print(f"  {label:25s} ({start} - {end}): no data in window")
            continue
        ret = sub_eq.iloc[-1] / sub_eq.iloc[0] - 1
        peak = sub_eq.cummax()
        dd = (sub_eq / peak - 1).min()
        # underlying drawdown
        sub_price = df_eth.loc[(df_eth.index >= pd.to_datetime(start, utc=True)) &
                               (df_eth.index <= pd.to_datetime(end, utc=True))]
        u_ret = sub_price["close"].iloc[-1] / sub_price["close"].iloc[0] - 1
        u_peak = sub_price["close"].cummax()
        u_dd = (sub_price["close"] / u_peak - 1).min()
        print(f"  {label:25s} ({start} -> {end}): "
              f"strategy {ret*100:+6.2f}% (DD {dd*100:+6.2f}%) | "
              f"buy&hold {u_ret*100:+7.2f}% (DD {u_dd*100:+7.2f}%)")


if __name__ == "__main__":
    main()
