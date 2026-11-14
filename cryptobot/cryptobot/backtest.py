"""Moteur de backtest barre par barre, réaliste :

- décision à la clôture de la barre t, exécution à l'OUVERTURE de t+1 (pas de look-ahead) ;
- frais Binance (0,1 % par ordre par défaut) + slippage ;
- stop suiveur ATR vérifié en intra-barre sur le plus bas (sortie au stop, ou à l'ouverture en cas de gap) ;
- pas de levier, pas de vente à découvert, impossible de dépenser plus que le cash disponible ;
- portefeuille multi-actifs à TRÉSORERIE COMMUNE : le capital peut passer de BTC à ETH
  (rotation) ; les ventes sont exécutées avant les achats.

Les cibles sont exprimées en *poids du portefeuille* (0,5 = 50 % du capital total sur l'actif).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ind
from .metrics import compute_metrics
from .risk import RiskConfig, round_exposure, size_positions
from .strategies import BuyHold, Strategy


@dataclass
class BacktestConfig:
    capital: float = 10_000.0          # capital fictif en USDT
    fee: float = 0.001                 # 0,1 % (taker Binance spot sans réduction BNB)
    slippage: float = 0.0005           # 0,05 % de glissement par ordre
    stop_cooldown: int = 6             # barres d'attente après un stop avant de pouvoir revenir
    halt_bars: int = 30                # durée de la pause après un « coupe-circuit » drawdown
    bars_per_year: float = 365 * 6     # 4h par défaut
    weights: dict | None = None        # répartition du capital par symbole (défaut : égale)


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    exposure: pd.DataFrame
    benchmark: pd.Series | None = None
    metrics: dict = field(default_factory=dict)
    benchmark_metrics: dict = field(default_factory=dict)
    fees_paid: float = 0.0


def align(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Ne garde que les dates communes à tous les actifs."""
    common = None
    for df in data.values():
        common = df.index if common is None else common.intersection(df.index)
    return {s: df.loc[common] for s, df in data.items()}


def portfolio_targets(strategy: Strategy, data: dict[str, pd.DataFrame], risk: RiskConfig, cfg: BacktestConfig) -> dict[str, pd.Series]:
    """Poids cibles du portefeuille. Une stratégie « de portefeuille » (rotation) les calcule elle-même ;
    sinon chaque actif reçoit sa part fixe × son exposition (signal × ciblage de volatilité)."""
    if hasattr(strategy, "portfolio_weights"):
        return strategy.portfolio_weights(data, risk, cfg.bars_per_year)
    weights = cfg.weights or {s: 1 / len(data) for s in data}
    return {s: weights[s] * size_positions(df, strategy.signal(df), risk, cfg.bars_per_year) for s, df in data.items()}


def run_portfolio(data: dict[str, pd.DataFrame], targets: dict[str, pd.Series], capital: float,
                  cfg: BacktestConfig, risk: RiskConfig):
    """Simule le portefeuille. `targets[s][t]` = poids voulu pour s, décidé à la clôture de t.
    Renvoie (equity, poids détenus, trades, frais)."""
    data = align(data)
    syms = list(data)
    k = len(syms)
    times = next(iter(data.values())).index
    n = len(times)
    O = np.column_stack([data[s]["open"].to_numpy(float) for s in syms])
    L = np.column_stack([data[s]["low"].to_numpy(float) for s in syms])
    C = np.column_stack([data[s]["close"].to_numpy(float) for s in syms])
    A = np.column_stack([ind.atr(data[s], risk.atr_n).to_numpy(float) for s in syms])
    T = np.column_stack([targets[s].reindex(times).fillna(0.0).to_numpy(float) for s in syms])
    band = risk.rebalance_band / k   # bande exprimée en exposition par actif
    buy_mult, sell_mult = 1 + cfg.slippage, 1 - cfg.slippage

    cash = capital
    qty = np.zeros(k)
    stop = np.full(k, np.nan)
    peak = np.full(k, np.nan)
    blocked_until = np.full(k, -1)
    waiting_reset = np.zeros(k, bool)
    halted_until = -1
    eq_peak = capital
    fees = 0.0
    trips: list[dict | None] = [None] * k
    trades: list[dict] = []
    equity = np.empty(n)
    held = np.empty((n, k))

    def sell(j, q, px, i, reason):
        nonlocal cash, fees
        fill = px * sell_mult
        notional = q * fill
        fee = notional * cfg.fee
        cash += notional - fee
        fees += fee
        qty[j] -= q
        trips[j]["proceeds"] += notional - fee
        if qty[j] <= 1e-12:
            qty[j] = 0.0
            trips[j].update(exit_time=times[i], exit_price=fill, reason=reason)
            _close_trip(trips[j], trades)
            trips[j] = None
            stop[j] = peak[j] = np.nan

    def buy(j, q, px, i, eq_now):
        nonlocal cash, fees
        fill = px * buy_mult
        q = min(q, cash / (fill * (1 + cfg.fee)))  # jamais plus que le cash disponible
        if q <= 1e-12:
            return
        notional = q * fill
        fee = notional * cfg.fee
        cash -= notional + fee
        fees += fee
        if trips[j] is None:
            trips[j] = {"symbol": syms[j], "entry_time": times[i], "entry_price": fill, "invested": 0.0,
                        "proceeds": 0.0, "equity_at_entry": eq_now}
            peak[j] = px
            if risk.stop_atr and np.isfinite(A[i - 1, j]):
                stop[j] = px - risk.stop_atr * A[i - 1, j]
        trips[j]["invested"] += notional + fee
        qty[j] += q

    for i in range(n):
        # 1) Exécution à l'ouverture des décisions prises à la clôture précédente.
        if i > 0:
            eq_open = cash + float(qty @ O[i])
            cur = qty * O[i] / eq_open if eq_open > 0 else np.zeros(k)
            want = np.zeros(k)
            for j in range(k):
                d = T[i - 1, j]
                if waiting_reset[j]:          # après un stop : attendre que le signal retombe
                    if d <= 1e-9 and i > blocked_until[j]:
                        waiting_reset[j] = False
                    d = 0.0
                if i <= blocked_until[j] or i <= halted_until:
                    d = 0.0
                want[j] = round_exposure(d, cur[j], band)
            for j in np.where(want < cur - 1e-9)[0]:          # ventes d'abord (libèrent du cash)
                target_q = want[j] * eq_open / O[i, j]
                sell(j, qty[j] - target_q if want[j] > 0 else qty[j], O[i, j], i, "signal")
            for j in np.where(want > cur + 1e-9)[0]:          # puis achats
                buy(j, want[j] * eq_open / O[i, j] - qty[j], O[i, j], i, eq_open)

        # 2) Stops suiveurs touchés pendant la barre ?
        for j in range(k):
            if qty[j] > 0 and np.isfinite(stop[j]) and L[i, j] <= stop[j]:
                sell(j, qty[j], min(O[i, j], stop[j]), i, "stop")
                blocked_until[j] = i + cfg.stop_cooldown
                waiting_reset[j] = risk.reentry == "fresh"

        # 3) Valorisation à la clôture et remontée des stops.
        pos_val = qty * C[i]
        eq = cash + pos_val.sum()
        equity[i] = eq
        held[i] = pos_val / eq if eq > 0 else 0.0
        if risk.stop_atr:
            for j in np.where(qty > 0)[0]:
                if np.isfinite(A[i, j]):
                    peak[j] = max(peak[j], C[i, j]) if np.isfinite(peak[j]) else C[i, j]
                    s = peak[j] - risk.stop_atr * A[i, j]
                    stop[j] = s if not np.isfinite(stop[j]) else max(stop[j], s)

        # 4) Coupe-circuit sur le drawdown du portefeuille.
        eq_peak = max(eq_peak, eq)
        if risk.max_drawdown_halt and i > halted_until and eq < eq_peak * (1 - risk.max_drawdown_halt):
            halted_until = i + cfg.halt_bars
            eq_peak = eq

    for j in range(k):  # positions encore ouvertes : valorisées au dernier cours
        if trips[j] is not None:
            trips[j]["proceeds"] += qty[j] * C[-1, j] * sell_mult * (1 - cfg.fee)
            trips[j].update(exit_time=times[-1], exit_price=C[-1, j], reason="open")
            _close_trip(trips[j], trades)

    return pd.Series(equity, index=times), pd.DataFrame(held, index=times, columns=syms), trades, fees


def run_sleeve(df: pd.DataFrame, target: pd.Series, capital: float, cfg: BacktestConfig, risk: RiskConfig, symbol: str = "X"):
    """Raccourci mono-actif. Renvoie (equity, exposition, trades, frais)."""
    eq, held, trades, fees = run_portfolio({symbol: df}, {symbol: target}, capital, cfg, risk)
    return eq, held[symbol], trades, fees


def _close_trip(trip: dict, trades: list):
    pnl = trip["proceeds"] - trip["invested"]
    trades.append({
        "symbol": trip["symbol"],
        "entry_time": trip["entry_time"],
        "exit_time": trip["exit_time"],
        "entry_price": trip["entry_price"],
        "exit_price": trip["exit_price"],
        "pnl": pnl,
        "return_pct": pnl / trip["invested"] * 100 if trip["invested"] else 0.0,
        "portfolio_impact_pct": pnl / trip["equity_at_entry"] * 100 if trip["equity_at_entry"] else 0.0,
        "duration": trip["exit_time"] - trip["entry_time"],
        "reason": trip["reason"],
    })


def run(data: dict[str, pd.DataFrame], strategy: Strategy, cfg: BacktestConfig | None = None,
        risk: RiskConfig | None = None, targets: dict[str, pd.Series] | None = None,
        benchmark: bool = True) -> BacktestResult:
    """Backtest d'une stratégie sur un portefeuille {symbole: OHLCV}.
    `targets` permet d'injecter des poids déjà calculés (utilisé par le walk-forward)."""
    cfg = cfg or BacktestConfig()
    risk = risk or RiskConfig()
    data = align(data)
    if targets is None:
        targets = portfolio_targets(strategy, data, risk, cfg)
    equity, held, trades, fees = run_portfolio(data, targets, cfg.capital, cfg, risk)
    trades = pd.DataFrame(trades)
    if not trades.empty:
        trades = trades.sort_values("entry_time").reset_index(drop=True)
    result = BacktestResult(equity=equity, trades=trades, exposure=held, fees_paid=fees)
    result.metrics = compute_metrics(equity, trades, cfg.bars_per_year, held)
    result.metrics["fees_paid"] = fees
    if benchmark:
        bh = run(data, BuyHold(), cfg, RiskConfig(target_vol=None, stop_atr=None, rebalance_band=10.0), benchmark=False)  # jamais rééquilibré
        result.benchmark = bh.equity
        result.benchmark_metrics = bh.metrics
    return result
