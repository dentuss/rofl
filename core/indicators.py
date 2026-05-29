"""Pure-pandas technical indicators. No TA-Lib, no pandas-ta dependency."""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    # Wilder smoothing
    roll_up = up.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    roll_down = down.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n, min_periods=n).mean()
    std = close.rolling(n, min_periods=n).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def supertrend(high: pd.Series, low: pd.Series, close: pd.Series,
               n: int = 10, k: float = 3.0):
    """Supertrend: returns (line, direction). direction = +1 long, -1 short."""
    a = atr(high, low, close, n)
    hl2 = (high + low) / 2.0
    upper = hl2 + k * a
    lower = hl2 - k * a

    # Recursive bands
    final_upper = upper.copy()
    final_lower = lower.copy()
    for i in range(1, len(close)):
        if close.iat[i - 1] <= final_upper.iat[i - 1]:
            final_upper.iat[i] = min(upper.iat[i], final_upper.iat[i - 1])
        if close.iat[i - 1] >= final_lower.iat[i - 1]:
            final_lower.iat[i] = max(lower.iat[i], final_lower.iat[i - 1])

    direction = pd.Series(1, index=close.index, dtype="int8")
    line = pd.Series(np.nan, index=close.index)
    for i in range(len(close)):
        if i == 0 or pd.isna(final_upper.iat[i]) or pd.isna(final_lower.iat[i]):
            continue
        if direction.iat[i - 1] == 1:
            if close.iat[i] < final_lower.iat[i]:
                direction.iat[i] = -1
                line.iat[i] = final_upper.iat[i]
            else:
                direction.iat[i] = 1
                line.iat[i] = final_lower.iat[i]
        else:
            if close.iat[i] > final_upper.iat[i]:
                direction.iat[i] = 1
                line.iat[i] = final_lower.iat[i]
            else:
                direction.iat[i] = -1
                line.iat[i] = final_upper.iat[i]
    return line, direction


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    hist = line - sig
    return line, sig, hist


def donchian(high: pd.Series, low: pd.Series, n: int = 20):
    upper = high.rolling(n, min_periods=n).max()
    lower = low.rolling(n, min_periods=n).min()
    mid = (upper + lower) / 2.0
    return upper, mid, lower


def adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    up = high.diff()
    dn = -low.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = true_range(high, low, close)
    atr_n = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean().fillna(0.0)


def choppiness_index(high: pd.Series, low: pd.Series, close: pd.Series,
                     n: int = 14) -> pd.Series:
    """Choppiness Index (0-100). High (>61.8) = consolidating/choppy,
    low (<38.2) = trending. Measures how much price meanders vs travels.

      CI = 100 * log10( sum(TR, n) / (maxHigh_n - minLow_n) ) / log10(n)
    """
    tr = true_range(high, low, close)
    tr_sum = tr.rolling(n, min_periods=n).sum()
    rng = high.rolling(n, min_periods=n).max() - low.rolling(n, min_periods=n).min()
    rng = rng.replace(0, np.nan)
    ci = 100 * np.log10(tr_sum / rng) / np.log10(n)
    return ci


def efficiency_ratio(close: pd.Series, n: int = 14) -> pd.Series:
    """Kaufman Efficiency Ratio (0-1). Near 1 = clean directional trend,
    near 0 = choppy/noisy. = |net change over n| / sum(|bar changes|).
    """
    net = (close - close.shift(n)).abs()
    noise = close.diff().abs().rolling(n, min_periods=n).sum().replace(0, np.nan)
    return (net / noise).clip(0, 1)
