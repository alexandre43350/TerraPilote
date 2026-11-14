"""Gestion du risque : dimensionnement des positions et stops.

- Ciblage de volatilité : on réduit l'exposition quand le marché devient très volatil,
  pour que le risque pris reste à peu près constant (c'est le levier n°1 pour limiter
  les drawdowns en crypto).
- Stop suiveur ATR : sortie si le prix recule de `stop_atr` × ATR depuis le plus haut
  atteint pendant la position. Appliqué barre par barre par le moteur.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class RiskConfig:
    target_vol: float | None = 0.60   # volatilité annuelle visée (BTC ~ 60-80 %/an). None = désactivé
    vol_lookback: int = 60            # barres pour estimer la volatilité réalisée
    max_exposure: float = 1.0         # 1.0 = pas de levier (spot)
    stop_atr: float | None = 3.0      # multiple d'ATR pour le stop suiveur. None = pas de stop
    atr_n: int = 14
    rebalance_band: float = 0.25      # on ne rééquilibre que si l'écart d'exposition > 25 points
    max_drawdown_halt: float | None = None  # ex. 0.35 : on coupe tout si le drawdown dépasse 35 %
    reentry: str = "cooldown"         # après un stop : "cooldown" (attendre stop_cooldown barres)
                                      # ou "fresh" (attendre en plus que le signal retombe à 0)


def size_positions(df: pd.DataFrame, signal: pd.Series, risk: RiskConfig, bars_per_year: float) -> pd.Series:
    """Exposition finale = signal × facteur de volatilité, bornée à [0, max_exposure]."""
    exposure = signal.clip(lower=0.0).fillna(0.0)
    if risk.target_vol:
        vol = ind.realized_vol(df["close"], risk.vol_lookback) * np.sqrt(bars_per_year)
        scale = (risk.target_vol / vol).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        exposure = exposure * scale.clip(upper=risk.max_exposure)
    return exposure.clip(0.0, risk.max_exposure)


def round_exposure(target: float, current: float, band: float) -> float:
    """Filtre anti-sur-trading : on garde l'exposition actuelle si l'écart est faible.
    Les passages à 0 (sortie complète) et depuis 0 (entrée) sont toujours exécutés."""
    if target <= 1e-9:
        return 0.0
    if current <= 1e-9:
        return target
    return target if abs(target - current) > band else current
