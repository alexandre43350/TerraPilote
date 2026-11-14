"""Moteur de backtest barre par barre, réaliste :

- décision à la clôture de la barre t, exécution à l'OUVERTURE de t+1 (pas de look-ahead) ;
- frais Binance (0,1 % par ordre par défaut) + slippage ;
- stop suiveur ATR vérifié en intra-barre sur le plus bas (sortie au stop, ou à l'ouverture en cas de gap) ;
- pas de levier, pas de vente à découvert, impossible de dépenser plus que le cash disponible ;
- portefeuille multi-actifs : capital réparti entre BTC / ETH en « poches » indépendantes.
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
    sleeves: dict = field(default_factory=dict)
    benchmark: pd.Series | None = None
    metrics: dict = field(default_factory=dict)
    benchmark_metrics: dict = field(default_factory=dict)
    fees_paid: float = 0.0


def run_sleeve(df: pd.DataFrame, target: pd.Series, capital: float, cfg: BacktestConfig, risk: RiskConfig, symbol: str = ""):
    """Simule une poche mono-actif. `target[t]` = exposition voulue décidée à la clôture de t."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    a = ind.atr(df, risk.atr_n).to_numpy(float)
    tgt = target.reindex(df.index).fillna(0.0).to_numpy(float)
    times = df.index
    n = len(df)
    buy_px_mult = 1 + cfg.slippage
    sell_px_mult = 1 - cfg.slippage

    cash, qty = capital, 0.0
    equity = np.empty(n)
    held = np.empty(n)
    fees = 0.0
    stop = np.nan
    peak_px = np.nan
    blocked_until = -1
    eq_peak = capital
    trades: list[dict] = []
    trip: dict | None = None

    def sell(q: float, px: float, i: int, reason: str):
        nonlocal cash, qty, fees, trip, stop, peak_px
        fill = px * sell_px_mult
        notional = q * fill
        fee = notional * cfg.fee
        cash += notional - fee
        fees += fee
        qty -= q
        if trip is not None:
            trip["proceeds"] += notional - fee
            if qty <= 1e-12:
                qty = 0.0
                trip.update(exit_time=times[i], exit_price=fill, reason=reason)
                _close_trip(trip, trades)
                trip = None
                stop, peak_px = np.nan, np.nan

    def buy(q: float, px: float, i: int, eq_now: float):
        nonlocal cash, qty, fees, trip, stop, peak_px
        fill = px * buy_px_mult
        q = min(q, cash / (fill * (1 + cfg.fee)))  # jamais plus que le cash disponible
        if q <= 0:
            return
        notional = q * fill
        fee = notional * cfg.fee
        cash -= notional + fee
        fees += fee
        if trip is None:
            trip = {"symbol": symbol, "entry_time": times[i], "entry_price": fill, "invested": 0.0,
                    "proceeds": 0.0, "equity_at_entry": eq_now, "entry_idx": i}
            peak_px = px
            stop = px - risk.stop_atr * a[i - 1] if risk.stop_atr and i > 0 and np.isfinite(a[i - 1]) else np.nan
        trip["invested"] += notional + fee
        qty += q

    for i in range(n):
        # 1) Exécution à l'ouverture de la décision prise à la clôture précédente.
        if i > 0:
            desired = 0.0 if i <= blocked_until else tgt[i - 1]
            eq_open = cash + qty * o[i]
            cur_w = qty * o[i] / eq_open if eq_open > 0 else 0.0
            w = round_exposure(desired, cur_w, risk.rebalance_band)
            if abs(w - cur_w) > 1e-9:
                target_qty = w * eq_open / o[i]
                if target_qty < qty:
                    sell(qty - target_qty if w > 0 else qty, o[i], i, "signal")
                else:
                    buy(target_qty - qty, o[i], i, eq_open)

        # 2) Stop suiveur touché pendant la barre ?
        if qty > 0 and np.isfinite(stop) and l[i] <= stop:
            sell(qty, min(o[i], stop), i, "stop")
            blocked_until = i + cfg.stop_cooldown

        # 3) Valorisation à la clôture et mise à jour du stop.
        eq = cash + qty * c[i]
        equity[i] = eq
        held[i] = qty * c[i] / eq if eq > 0 else 0.0
        if qty > 0 and risk.stop_atr and np.isfinite(a[i]):
            peak_px = np.nanmax([peak_px, c[i]])
            new_stop = peak_px - risk.stop_atr * a[i]
            stop = new_stop if not np.isfinite(stop) else max(stop, new_stop)

        # 4) Coupe-circuit sur drawdown de la poche.
        eq_peak = max(eq_peak, eq)
        if risk.max_drawdown_halt and i > blocked_until and eq < eq_peak * (1 - risk.max_drawdown_halt):
            blocked_until = i + cfg.halt_bars
            eq_peak = eq  # on repart de zéro après la pause

    if trip is not None:  # position encore ouverte en fin de période : valorisée au dernier cours
        trip["proceeds"] += qty * c[-1] * sell_px_mult * (1 - cfg.fee)
        trip.update(exit_time=times[-1], exit_price=c[-1], reason="open")
        _close_trip(trip, trades)

    return (pd.Series(equity, index=times), pd.Series(held, index=times), trades, fees)


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
    `targets` permet d'injecter des expositions déjà calculées (utilisé par le walk-forward)."""
    cfg = cfg or BacktestConfig()
    risk = risk or RiskConfig()
    weights = cfg.weights or {s: 1 / len(data) for s in data}
    equities, exposures, all_trades, sleeves, fees = {}, {}, [], {}, 0.0
    for sym, df in data.items():
        if targets is not None:
            tgt = targets[sym]
        else:
            tgt = size_positions(df, strategy.signal(df), risk, cfg.bars_per_year)
        eq, held, trades, f = run_sleeve(df, tgt, cfg.capital * weights[sym], cfg, risk, sym)
        equities[sym], exposures[sym], sleeves[sym] = eq, held, eq
        all_trades.extend(trades)
        fees += f
    eq_df = pd.DataFrame(equities).ffill()
    # Poches qui démarrent plus tard (ex. historique plus court) : capital en cash en attendant.
    for sym in eq_df:
        eq_df[sym] = eq_df[sym].fillna(cfg.capital * weights[sym])
    equity = eq_df.sum(axis=1)
    trades = pd.DataFrame(all_trades)
    if not trades.empty:
        trades = trades.sort_values("entry_time").reset_index(drop=True)
    result = BacktestResult(equity=equity, trades=trades, exposure=pd.DataFrame(exposures), sleeves=sleeves, fees_paid=fees)
    result.metrics = compute_metrics(equity, trades, cfg.bars_per_year, result.exposure)
    result.metrics["fees_paid"] = fees
    if benchmark:
        bh = run(data, BuyHold(), cfg, RiskConfig(target_vol=None, stop_atr=None), benchmark=False)
        result.benchmark = bh.equity.reindex(equity.index).ffill()
        result.benchmark_metrics = bh.metrics
    return result
