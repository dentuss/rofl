"""Sentiment data: Crypto Fear & Greed Index.

The F&G index (0-100) aggregates volatility, market momentum, social media,
surveys, BTC dominance, and Google Trends. It's published daily by
alternative.me. Free, no auth, history back to 2018-02-01.

  0-25   = Extreme Fear
  25-45  = Fear
  45-55  = Neutral
  55-75  = Greed
  75-100 = Extreme Greed

Classic interpretation: extremes are reversal zones. Extreme greed is when
markets top, extreme fear is when they bottom. We use it as a filter on the
trend-following strategy: avoid trades that fight the consensus too hard.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)


def fetch_fear_greed(use_cache: bool = True) -> pd.DataFrame:
    """Return daily F&G index. Index is UTC date, single column 'fng' (int)."""
    cache_file = CACHE_DIR / "fear_greed.parquet"
    if use_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < 6 * 3600:  # refresh every 6h
            return pd.read_parquet(cache_file)

    last_err: Exception | None = None
    for attempt in range(4):
        try:
            r = requests.get("https://api.alternative.me/fng/",
                             params={"limit": 0, "format": "json"}, timeout=20)
            r.raise_for_status()
            payload = r.json()
            break
        except Exception as e:
            last_err = e
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"F&G fetch failed: {last_err}")

    rows = payload.get("data") or []
    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["timestamp"].astype("int64"), unit="s", utc=True).dt.normalize()
    df["fng"] = df["value"].astype(int)
    df = df.set_index("dt")[["fng"]].sort_index()

    if use_cache:
        df.to_parquet(cache_file)
    return df


def align_to_bars(fng: pd.DataFrame, bar_index: pd.DatetimeIndex) -> pd.Series:
    """Forward-fill the daily F&G value onto an arbitrary bar index.

    F&G is published once per UTC day. For an intraday bar timestamped t, we
    use the F&G value of the most recent already-published day (t.date()
    minus one if we want to be strict about look-ahead). We use t.date(),
    which means the bot effectively sees today's F&G after midnight UTC -
    that's how the index updates in real life.
    """
    f = fng.copy()
    f.index = pd.DatetimeIndex(f.index, tz="UTC")
    out = pd.Series(index=bar_index, dtype="float64", name="fng")
    daily = f["fng"].asof(bar_index)
    out[:] = daily.values
    return out


if __name__ == "__main__":
    df = fetch_fear_greed()
    print(f"Daily F&G: {len(df)} rows, {df.index[0].date()} -> {df.index[-1].date()}")
    print("\nLast 7 days:")
    print(df.tail(7))
    print("\nDistribution (1y):")
    last_year = df.tail(365)
    bins = pd.cut(last_year["fng"], bins=[0, 25, 45, 55, 75, 100],
                  labels=["x.fear", "fear", "neutral", "greed", "x.greed"])
    print(bins.value_counts().sort_index())
