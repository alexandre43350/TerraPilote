"""Optimisation walk-forward : la seule façon honnête d'évaluer une stratégie optimisée.

Principe : on découpe l'historique en fenêtres glissantes.
  [---- entraînement (ex. 2 ans) ----][- test (6 mois) -]
                 [---- entraînement ----][- test -]
                               [---- entraînement ----][- test -]
Sur chaque fenêtre d'entraînement on choisit les meilleurs paramètres, puis on les
applique sur la période de test qui suit, JAMAIS vue pendant le choix. La performance
publiée est celle des périodes de test mises bout à bout (« out-of-sample »).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest import BacktestConfig, BacktestResult, run, run_sleeve
from .metrics import bootstrap_ci, deflated_sharpe, sharpe
from .risk import RiskConfig, size_positions
from .strategies import Strategy


@dataclass
class WalkForwardResult:
    strategy: str
    folds: pd.DataFrame
    oos: BacktestResult
    n_trials: int
    deflated_sharpe: float
    bootstrap: dict = field(default_factory=dict)


def _align(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    common = None
    for df in data.values():
        common = df.index if common is None else common.intersection(df.index)
    return {s: df.loc[common] for s, df in data.items()}


def _score(eq: pd.Series, bars_per_year: float, objective: str) -> float:
    r = eq.pct_change().dropna().to_numpy()
    if len(r) < 10 or r.std() == 0:
        return -np.inf
    if objective == "sharpe":
        return sharpe(r, bars_per_year)
    if objective == "calmar":
        years = len(r) / bars_per_year
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
        mdd = (eq / eq.cummax() - 1).min()
        return cagr / -mdd if mdd < 0 else -np.inf
    if objective == "cagr":
        return float(np.log(eq.iloc[-1] / eq.iloc[0]))
    raise ValueError(objective)


def walk_forward(data: dict[str, pd.DataFrame], strategy: Strategy, cfg: BacktestConfig | None = None,
                 risk: RiskConfig | None = None, train_days: int = 730, test_days: int = 180,
                 objective: str = "sharpe", verbose: bool = False) -> WalkForwardResult:
    cfg = cfg or BacktestConfig()
    risk = risk or RiskConfig()
    data = _align(data)
    index = next(iter(data.values())).index
    bars_per_day = cfg.bars_per_year / 365
    train_bars, test_bars = int(train_days * bars_per_day), int(test_days * bars_per_day)
    if len(index) < train_bars + test_bars:
        raise ValueError(f"Historique trop court : {len(index)} barres pour {train_bars}+{test_bars} demandées.")
    weights = cfg.weights or {s: 1 / len(data) for s in data}

    combos = strategy.grid_combinations()
    # 1) Une simulation complète par combinaison (les indicateurs sont causaux, donc on peut
    #    calculer sur tout l'historique puis ne LIRE que la fenêtre d'entraînement).
    targets_by_combo, equity_by_combo = [], []
    for k, params in enumerate(combos):
        strat = strategy.with_params(**params)
        tg, eqs = {}, []
        for sym, df in data.items():
            tg[sym] = size_positions(df, strat.signal(df), risk, cfg.bars_per_year)
            eq, *_ = run_sleeve(df, tg[sym], cfg.capital * weights[sym], cfg, risk, sym)
            eqs.append(eq)
        targets_by_combo.append(tg)
        equity_by_combo.append(sum(eqs))
        if verbose:
            print(f"\r  simulation {k + 1}/{len(combos)}", end="", flush=True)
    if verbose:
        print()

    # 2) Pour chaque fenêtre : choix des paramètres sur l'entraînement, application sur le test.
    folds, oos_targets = [], {s: [] for s in data}
    start = train_bars
    while start < len(index):
        tr = slice(start - train_bars, start)
        te = slice(start, min(start + test_bars, len(index)))
        scores = [_score(eq.iloc[tr], cfg.bars_per_year, objective) for eq in equity_by_combo]
        best = int(np.nanargmax(scores))
        oos_score = _score(equity_by_combo[best].iloc[slice(start - 1, te.stop)], cfg.bars_per_year, "sharpe")
        folds.append({
            "train_start": index[tr.start], "test_start": index[te.start], "test_end": index[te.stop - 1],
            "params": combos[best], "train_score": scores[best], "test_sharpe": oos_score,
        })
        for sym in data:
            oos_targets[sym].append(targets_by_combo[best][sym].iloc[te])
        start += test_bars

    # 3) Backtest réel sur la période out-of-sample recousue.
    oos_start = index[train_bars]
    oos_data = {s: df[df.index >= oos_start] for s, df in data.items()}
    stitched = {s: pd.concat(v) for s, v in oos_targets.items()}
    oos = run(oos_data, strategy, cfg, risk, targets=stitched)

    r = oos.equity.pct_change().dropna().to_numpy()
    trial_sr = [float(np.mean(x) / np.std(x, ddof=1)) for x in (eq.pct_change().dropna().to_numpy() for eq in equity_by_combo) if np.std(x) > 0]
    dsr = deflated_sharpe(r, trial_sr) if len(combos) > 1 else oos.metrics.get("psr", math.nan)
    return WalkForwardResult(
        strategy=strategy.name,
        folds=pd.DataFrame(folds),
        oos=oos,
        n_trials=len(combos),
        deflated_sharpe=dsr,
        bootstrap=bootstrap_ci(r, cfg.bars_per_year),
    )
