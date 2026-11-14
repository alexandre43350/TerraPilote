"""Indicateurs techniques. Tous sont causaux : la valeur à t n'utilise que les données <= t."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd, "z": (close - mid) / sd})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Force de la tendance (0-100). > 25 : marché en tendance, < 20 : marché sans direction."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Canal de Donchian sur les n barres PRÉCÉDENTES (exclut la barre courante)."""
    return pd.DataFrame(
        {"upper": df["high"].rolling(n, min_periods=n).max().shift(), "lower": df["low"].rolling(n, min_periods=n).min().shift()}
    )


def realized_vol(close: pd.Series, n: int = 30) -> pd.Series:
    """Écart-type des rendements log par barre (non annualisé)."""
    return np.log(close).diff().rolling(n, min_periods=n).std()


def efficiency_ratio(close: pd.Series, n: int = 20) -> pd.Series:
    """Kaufman : |déplacement net| / somme des déplacements. ~1 = tendance pure, ~0 = bruit."""
    change = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return change / path.replace(0, np.nan)
