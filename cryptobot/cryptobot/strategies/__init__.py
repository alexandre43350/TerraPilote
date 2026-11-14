"""Stratégies de trading.

Chaque stratégie renvoie une *exposition cible* entre 0 et 1 pour chaque barre
(0 = 100 % cash USDT, 1 = 100 % crypto). La valeur à la barre t est calculée avec
la clôture de t et sera exécutée à l'ouverture de t+1 par le moteur : pas de look-ahead.
Le bot est « long only » (marché spot) : il ne vend pas à découvert.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import indicators as ind


@dataclass
class Strategy:
    name: str = "base"
    params: dict = field(default_factory=dict)
    # Grille de paramètres explorée par l'optimiseur walk-forward.
    grid: dict = field(default_factory=dict)

    def signal(self, df: pd.DataFrame) -> pd.Series:  # pragma: no cover - interface
        raise NotImplementedError

    def with_params(self, **params) -> "Strategy":
        return type(self)(params={**self.params, **params})

    def grid_combinations(self) -> list[dict]:
        if not self.grid:
            return [dict(self.params)]
        keys = list(self.grid)
        combos = []
        for values in itertools.product(*(self.grid[k] for k in keys)):
            p = {**self.params, **dict(zip(keys, values))}
            if self.valid(p):
                combos.append(p)
        return combos

    @staticmethod
    def valid(p: dict) -> bool:
        return True


def _stateful(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Position 0/1 : on entre quand `entry` est vrai, on reste jusqu'à `exit_`."""
    pos = np.zeros(len(entry))
    state = 0.0
    for i in range(len(entry)):
        if state == 0 and entry[i]:
            state = 1.0
        elif state == 1 and exit_[i]:
            state = 0.0
        pos[i] = state
    return pos


class BuyHold(Strategy):
    """Référence : acheter et conserver. Toute stratégie doit être comparée à elle."""

    def __init__(self, params=None):
        super().__init__("buy_hold", params or {}, {})

    def signal(self, df):
        return pd.Series(1.0, index=df.index)


class EmaCross(Strategy):
    """Suivi de tendance : investi quand l'EMA rapide est au-dessus de l'EMA lente."""

    def __init__(self, params=None):
        super().__init__(
            "ema_cross",
            {"fast": 20, "slow": 100, **(params or {})},
            {"fast": [10, 20, 30, 50], "slow": [50, 100, 150, 200]},
        )

    @staticmethod
    def valid(p):
        return p["fast"] * 2 <= p["slow"]

    def signal(self, df):
        c = df["close"]
        return (ind.ema(c, self.params["fast"]) > ind.ema(c, self.params["slow"])).astype(float)


class DonchianBreakout(Strategy):
    """Cassure de canal (type « Turtle ») : achat au plus haut de N barres, sortie au plus bas de M barres."""

    def __init__(self, params=None):
        super().__init__(
            "donchian",
            {"entry": 55, "exit": 20, **(params or {})},
            {"entry": [20, 40, 55, 80, 120], "exit": [10, 20, 30, 50]},
        )

    @staticmethod
    def valid(p):
        return p["exit"] < p["entry"]

    def signal(self, df):
        up = ind.donchian(df, self.params["entry"])["upper"]
        lo = ind.donchian(df, self.params["exit"])["lower"]
        c = df["close"]
        return pd.Series(_stateful((c > up).to_numpy(), (c < lo).to_numpy()), index=df.index)


class MacdTrend(Strategy):
    """Momentum MACD filtré par une tendance de fond (prix au-dessus d'une EMA longue)."""

    def __init__(self, params=None):
        super().__init__(
            "macd",
            {"fast": 12, "slow": 26, "sig": 9, "trend": 200, **(params or {})},
            {"fast": [8, 12, 20], "slow": [26, 40, 60], "sig": [9], "trend": [100, 200, 300]},
        )

    @staticmethod
    def valid(p):
        return p["fast"] < p["slow"]

    def signal(self, df):
        c = df["close"]
        m = ind.macd(c, self.params["fast"], self.params["slow"], self.params["sig"])
        return ((m["hist"] > 0) & (c > ind.ema(c, self.params["trend"]))).astype(float)


class MeanReversion(Strategy):
    """Retour à la moyenne : achat des excès de baisse (Bollinger + RSI) uniquement en tendance
    de fond haussière ; sortie au retour à la moyenne ou après `max_hold` barres."""

    def __init__(self, params=None):
        super().__init__(
            "mean_reversion",
            {"n": 20, "k": 2.0, "rsi_n": 14, "rsi_low": 30, "trend": 200, "max_hold": 30, **(params or {})},
            {"n": [20, 40], "k": [1.5, 2.0, 2.5], "rsi_low": [25, 30, 40], "trend": [100, 200], "max_hold": [30]},
        )

    def signal(self, df):
        p = self.params
        c = df["close"]
        bb = ind.bollinger(c, p["n"], p["k"])
        r = ind.rsi(c, p["rsi_n"])
        uptrend = c > ind.sma(c, p["trend"])
        entry = ((c < bb["lower"]) & (r < p["rsi_low"]) & uptrend).to_numpy()
        exit_ = (c > bb["mid"]).to_numpy()
        pos = np.zeros(len(c))
        state, held = 0.0, 0
        for i in range(len(c)):
            if state == 0 and entry[i]:
                state, held = 1.0, 0
            elif state == 1:
                held += 1
                if exit_[i] or held >= p["max_hold"]:
                    state = 0.0
            pos[i] = state
        return pd.Series(pos, index=df.index)


class TimeSeriesMomentum(Strategy):
    """Momentum multi-horizons : moyenne de signaux « le prix a monté sur L barres » pour plusieurs L.
    Donne une exposition graduée (0, 1/3, 2/3, 1) — plus robuste qu'un seul horizon."""

    def __init__(self, params=None):
        super().__init__(
            "tsmom",
            {"base": 30, **(params or {})},
            {"base": [10, 20, 30, 45, 60, 90]},
        )

    def signal(self, df):
        c = df["close"]
        b = self.params["base"]
        sigs = [(c > c.shift(L)).astype(float).where(c.shift(L).notna()) for L in (b, 3 * b, 6 * b)]
        return pd.concat(sigs, axis=1).mean(axis=1).fillna(0.0)


class RegimeAdaptive(Strategy):
    """Détection de régime : en marché directionnel (efficiency ratio élevé) on suit la tendance
    (EMA), en marché latéral on n'achète que les excès de baisse (retour à la moyenne)."""

    def __init__(self, params=None):
        super().__init__(
            "regime",
            {"er_n": 30, "er_min": 0.30, "fast": 20, "slow": 100, "bb_n": 20, "bb_k": 2.0, **(params or {})},
            {"er_n": [20, 30, 60], "er_min": [0.2, 0.3, 0.4], "fast": [20, 30], "slow": [100, 150]},
        )

    def signal(self, df):
        p = self.params
        c = df["close"]
        er = ind.efficiency_ratio(c, p["er_n"])
        trend_up = ind.ema(c, p["fast"]) > ind.ema(c, p["slow"])
        bb = ind.bollinger(c, p["bb_n"], p["bb_k"])
        trending = er > p["er_min"]
        trend_sig = (trending & trend_up).to_numpy()
        mr_entry = ((~trending) & (c < bb["lower"]) & (c > ind.sma(c, 200))).to_numpy()
        mr_exit = (c > bb["mid"]).to_numpy()
        mr = _stateful(mr_entry, mr_exit)
        # Tendance franche ou excès de baisse à racheter : 100 %. Hausse molle sans direction : 50 %.
        pos = np.where(trend_sig | (mr == 1), 1.0, np.where(trend_up.to_numpy(), 0.5, 0.0))
        return pd.Series(pos, index=df.index)


class Ensemble(Strategy):
    """Moyenne des expositions de plusieurs stratégies décorrélées (diversification des modèles)."""

    def __init__(self, params=None, members: list[Strategy] | None = None):
        super().__init__("ensemble", params or {}, {})
        self.members = members or [EmaCross(), DonchianBreakout(), TimeSeriesMomentum(), MacdTrend()]

    def with_params(self, **params):
        return Ensemble({**self.params, **params}, members=self.members)

    def signal(self, df):
        return pd.concat([m.signal(df) for m in self.members], axis=1).mean(axis=1)


class Rotation(Strategy):
    """Stratégie de PORTEFEUILLE (BTC + ETH, trésorerie commune) :

    1. Score de tendance par actif = moyenne de 3 modèles (EMA, Donchian, momentum) → 0 à 1.
    2. Filtre de marché : BTC « tire » tout le marché crypto. Si BTC est sous sa moyenne longue
       (`market_n` barres), on n'achète RIEN, même si ETH a l'air de monter.
    3. Option rotation : l'actif le plus fort (momentum sur `rs_n` barres) reçoit plus de capital
       (`tilt`) ; si un seul actif est en tendance, il peut recevoir le capital de l'autre (`concentrate`).
       Testé en walk-forward sur 2018-2024 : la rotation n'a PAS amélioré les résultats, elle est donc
       désactivée par défaut (tilt=0, concentrate=False). Le filtre de marché, lui, réduit les pertes.
    4. Ciblage de volatilité par actif (via RiskConfig), poids total ≤ 100 %.
    """

    def __init__(self, params=None):
        super().__init__(
            "rotation",
            {"market_filter": True, "market_n": 600, "rs_n": 180, "tilt": 0.0, "concentrate": False, "macd": True,
             "fast": 20, "slow": 100, "entry": 55, "exit": 20, "base": 30, **(params or {})},
            {},
        )

    def signal(self, df):
        raise TypeError("rotation est une stratégie de portefeuille : utiliser portfolio_weights()")

    def trend_scores(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        members = [EmaCross({"fast": p["fast"], "slow": p["slow"]}),
                   DonchianBreakout({"entry": p["entry"], "exit": p["exit"]}),
                   TimeSeriesMomentum({"base": p["base"]})]
        if p["macd"]:
            members.append(MacdTrend())
        return pd.concat([m.signal(df) for m in members], axis=1).mean(axis=1)

    def portfolio_weights(self, data: dict[str, pd.DataFrame], risk, bars_per_year: float) -> dict[str, pd.Series]:
        from ..risk import size_positions

        p = self.params
        syms = list(data)
        k = len(syms)
        market = data["BTCUSDT"] if "BTCUSDT" in data else data[syms[0]]
        mc = market["close"]
        risk_on = (mc > ind.ema(mc, p["market_n"])).astype(float).reindex(data[syms[0]].index).fillna(0.0)
        if not p["market_filter"]:
            risk_on[:] = 1.0

        expo = pd.DataFrame({s: size_positions(df, self.trend_scores(df), risk, bars_per_year) for s, df in data.items()})
        on = expo > 1e-9

        # force relative : momentum de chaque actif sur rs_n barres, le meilleur est « leader »
        mom = pd.DataFrame({s: df["close"] / df["close"].shift(p["rs_n"]) for s, df in data.items()})
        leader = mom.eq(mom.max(axis=1), axis=0) & mom.notna()
        alloc = pd.DataFrame(1.0 / k, index=expo.index, columns=syms)
        if k > 1 and p["tilt"]:
            alloc = alloc.where(~leader, (1 + p["tilt"]) / k).where(leader, (1 - p["tilt"]) / k)
        if p["concentrate"]:
            active = alloc.where(on, 0.0).sum(axis=1).replace(0, np.nan)
            alloc = alloc.div(active, axis=0).fillna(0.0).clip(upper=1.0)
        w = (alloc * expo).mul(risk_on, axis=0)
        total = w.sum(axis=1)
        w = w.div(total.clip(lower=1.0), axis=0)  # jamais plus de 100 % investi
        return {s: w[s] for s in syms}


REGISTRY: dict[str, type[Strategy]] = {
    "buy_hold": BuyHold,
    "ema_cross": EmaCross,
    "donchian": DonchianBreakout,
    "macd": MacdTrend,
    "mean_reversion": MeanReversion,
    "tsmom": TimeSeriesMomentum,
    "regime": RegimeAdaptive,
    "ensemble": Ensemble,
    "rotation": Rotation,
}


def get(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"Stratégie inconnue '{name}'. Disponibles : {', '.join(REGISTRY)}")
    return REGISTRY[name](params or None)
