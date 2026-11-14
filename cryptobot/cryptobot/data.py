"""Récupération et mise en cache des bougies (OHLCV).

Source : API publique de Binance (aucune clé nécessaire, lecture seule).
`data-api.binance.vision` est le miroir officiel « market data only » ; on bascule
sur `api.binance.com` s'il est indisponible.

Un générateur de marché synthétique est aussi fourni pour les tests hors ligne.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE_URLS = ("https://data-api.binance.vision", "https://api.binance.com")
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

INTERVAL_MS = {
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

# Nombre de barres par an, pour annualiser les statistiques (crypto = 24/7).
BARS_PER_YEAR = {k: 365 * 24 * 60 * 60_000 / v for k, v in INTERVAL_MS.items()}

COLUMNS = ["open", "high", "low", "close", "volume"]


def _get(path: str, params: dict, retries: int = 4) -> list:
    last_err: Exception | None = None
    for attempt in range(retries):
        for base in BASE_URLS:
            try:
                r = requests.get(base + path, params=params, timeout=20)
                if r.status_code == 200:
                    return r.json()
                last_err = RuntimeError(f"{base}: HTTP {r.status_code} {r.text[:200]}")
            except requests.RequestException as e:  # réseau
                last_err = e
        time.sleep(2**attempt)
    raise RuntimeError(f"Impossible de joindre Binance : {last_err}")


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def fetch_klines(symbol: str, interval: str, start: str | pd.Timestamp, end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """Télécharge toutes les bougies entre start et end (pagination automatique, 1000 par requête)."""
    if interval not in INTERVAL_MS:
        raise ValueError(f"Intervalle non supporté : {interval}")
    start_ms = int(_utc(start).timestamp() * 1000)
    end_ms = int(_utc(end).timestamp() * 1000) if end is not None else int(time.time() * 1000)
    rows: list = []
    cursor = start_ms
    while cursor < end_ms:
        batch = _get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not batch:
            break
        rows.extend(batch)
        cursor = batch[-1][0] + INTERVAL_MS[interval]
        if len(batch) < 1000:
            break
    return _to_frame(rows, interval)


def _to_frame(rows: list, interval: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = pd.DataFrame(rows).iloc[:, :7]
    df.columns = ["time", *COLUMNS, "close_time"]
    # On écarte la bougie en cours (pas encore clôturée) : la trader serait du look-ahead.
    now_ms = int(time.time() * 1000)
    df = df[df["close_time"] < now_ms]
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.set_index("time")[COLUMNS].astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def load(symbol: str, interval: str = "4h", start: str = "2018-01-01", end: str | None = None, refresh: bool = True) -> pd.DataFrame:
    """Charge depuis le cache CSV et complète avec les bougies manquantes."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{symbol}_{interval}.csv"
    cached = None
    if path.exists():
        cached = pd.read_csv(path, index_col="time", parse_dates=["time"])
        if cached.index.tz is None:
            cached.index = cached.index.tz_localize("UTC")
    start_ts = _utc(start)
    if cached is not None and not cached.empty and cached.index[0] <= start_ts + pd.Timedelta(days=7):
        if refresh:
            new = fetch_klines(symbol, interval, cached.index[-1] + pd.Timedelta(milliseconds=INTERVAL_MS[interval]), end)
            cached = pd.concat([cached, new])
            cached = cached[~cached.index.duplicated(keep="last")].sort_index()
        df = cached
    else:
        df = fetch_klines(symbol, interval, start_ts, end)
    df.to_csv(path)
    df = df[df.index >= start_ts]
    if end:
        df = df[df.index <= _utc(end)]
    return df


def synthetic(n: int = 5000, seed: int = 0, start_price: float = 20_000.0, interval: str = "4h") -> pd.DataFrame:
    """Marché synthétique réaliste : régimes haussier/baissier/latéral, volatilité en grappes (GARCH)
    et sauts. Sert aux tests unitaires et aux essais hors ligne — PAS à juger une stratégie."""
    rng = np.random.default_rng(seed)
    regimes = np.array([0.0006, -0.0005, 0.0])  # dérive par barre
    regime = 0
    omega, alpha, beta = 2e-6, 0.08, 0.90
    var = omega / (1 - alpha - beta)
    rets = np.empty(n)
    for i in range(n):
        if rng.random() < 0.004:
            regime = rng.integers(0, 3)
        eps = rng.standard_t(4) / np.sqrt(2)
        r = regimes[regime] + np.sqrt(var) * eps
        if rng.random() < 0.003:
            r += rng.normal(0, 0.06)
        rets[i] = r
        var = omega + alpha * r**2 + beta * var
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.normal(0, np.sqrt(np.maximum(var, 1e-6)), n)) * close
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - wick
    idx = pd.date_range("2020-01-01", periods=n, freq=pd.Timedelta(milliseconds=INTERVAL_MS[interval]), tz="UTC", name="time")
    vol = rng.lognormal(10, 0.5, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
