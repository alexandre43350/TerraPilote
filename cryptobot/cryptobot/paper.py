"""Paper trading : le bot tourne en temps réel sur les vrais prix Binance, mais avec
un portefeuille FICTIF. Aucun ordre réel n'est jamais envoyé (aucune clé API utilisée).

Même logique que le backtest : décision à la clôture de chaque bougie, trésorerie commune
BTC/ETH, ventes avant achats, frais et slippage simulés, stop suiveur ATR surveillé à chaque
relevé de prix. L'état est sauvegardé dans state/<nom>.json : on peut arrêter et relancer le bot.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import data as market
from . import indicators as ind
from .backtest import BacktestConfig, align, portfolio_targets
from .risk import RiskConfig, round_exposure
from .strategies import Strategy

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperTrader:
    def __init__(self, strategy: Strategy, symbols: list[str], interval: str = "4h", capital: float = 10_000.0,
                 cfg: BacktestConfig | None = None, risk: RiskConfig | None = None, name: str | None = None,
                 warmup_bars: int = 2500, price_fn=None, candles_fn=None, state_dir: Path = STATE_DIR):
        self.strategy, self.symbols, self.interval = strategy, symbols, interval
        self.cfg = cfg or BacktestConfig(capital=capital, bars_per_year=market.BARS_PER_YEAR[interval])
        self.risk = risk or RiskConfig()
        self.name = name or f"{strategy.name}_{interval}"
        self.warmup = warmup_bars
        self.price_fn = price_fn or self._live_price
        self.live = candles_fn is None
        self.candles_fn = candles_fn or self._live_candles
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / f"{self.name}.json"
        self.trades_path = state_dir / f"{self.name}_trades.csv"
        self.equity_path = state_dir / f"{self.name}_equity.csv"
        self.state = self._load(capital)

    # ---------- état ----------
    def _load(self, capital: float) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {
            "created": _now(), "strategy": self.strategy.name, "params": self.strategy.params,
            "interval": self.interval, "initial_capital": capital, "cash": capital, "last_bar": None,
            "positions": {s: {"qty": 0.0, "stop": None, "peak": None, "blocked_bars": 0, "wait_reset": False}
                          for s in self.symbols},
        }

    def _save(self):
        self.state_path.write_text(json.dumps(self.state, indent=2, default=str))

    # ---------- marché ----------
    @staticmethod
    def _live_price(symbol: str) -> float:
        return float(market._get("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def _live_candles(self, symbol: str) -> pd.DataFrame:
        ms = market.INTERVAL_MS[self.interval]
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(milliseconds=ms * self.warmup)
        return market.fetch_klines(symbol, self.interval, start)

    # ---------- exécution simulée ----------
    def _equity(self, prices: dict) -> float:
        return self.state["cash"] + sum(p["qty"] * prices[s] for s, p in self.state["positions"].items())

    def _log_trade(self, symbol, side, qty, price, fee, reason, equity):
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "symbol", "side", "qty", "price", "notional", "fee", "reason", "portfolio_equity"])
            w.writerow([_now(), symbol, side, f"{qty:.8f}", f"{price:.2f}", f"{qty * price:.2f}", f"{fee:.4f}", reason, f"{equity:.2f}"])
        print(f"[{_now()}] {side:4s} {symbol} {qty:.6f} @ {price:,.2f} ({reason})")

    def _sell(self, sym, q, price, reason, eq):
        pos = self.state["positions"][sym]
        fill = price * (1 - self.cfg.slippage)
        fee = q * fill * self.cfg.fee
        self.state["cash"] += q * fill - fee
        pos["qty"] -= q
        if pos["qty"] <= 1e-12:
            pos.update(qty=0.0, stop=None, peak=None)
        self._log_trade(sym, "SELL", q, fill, fee, reason, eq)

    def _buy(self, sym, q, price, eq):
        pos = self.state["positions"][sym]
        fill = price * (1 + self.cfg.slippage)
        q = min(q, self.state["cash"] / (fill * (1 + self.cfg.fee)))
        if q * fill < 1.0:  # moins de 1 $ : ignoré
            return
        fee = q * fill * self.cfg.fee
        self.state["cash"] -= q * fill + fee
        if pos["qty"] <= 0:
            pos["peak"] = price
        pos["qty"] += q
        self._log_trade(sym, "BUY", q, fill, fee, "signal", eq)

    # ---------- boucle ----------
    def step(self) -> dict:
        """Un cycle : stops intra-bougie puis, si une nouvelle bougie est close, nouvelle décision."""
        st = self.state
        prices = {s: self.price_fn(s) for s in self.symbols}
        for s, pos in st["positions"].items():
            if pos["qty"] > 0 and pos["stop"] is not None and prices[s] <= pos["stop"]:
                self._sell(s, pos["qty"], prices[s], "stop", self._equity(prices))
                pos["blocked_bars"] = self.cfg.stop_cooldown
                pos["wait_reset"] = self.risk.reentry == "fresh"

        if self.live and st["last_bar"]:
            # inutile de retélécharger l'historique tant qu'aucune nouvelle bougie n'est close
            ms = market.INTERVAL_MS[self.interval]
            expected = pd.Timestamp((time.time() * 1000 // ms) * ms - ms, unit="ms", tz="UTC")
            new_bar_possible = str(expected) != st["last_bar"]
        else:
            new_bar_possible = True
        data = align({s: self.candles_fn(s) for s in self.symbols}) if new_bar_possible else None
        last_bar = str(next(iter(data.values())).index[-1]) if data is not None else st["last_bar"]
        if last_bar != st["last_bar"]:
            st["last_bar"] = last_bar
            targets = portfolio_targets(self.strategy, data, self.risk, self.cfg)
            eq = self._equity(prices)
            band = self.risk.rebalance_band / len(self.symbols)
            want = {}
            for s, pos in st["positions"].items():
                d = float(targets[s].iloc[-1])
                if pos["wait_reset"]:
                    if d <= 1e-9 and pos["blocked_bars"] <= 0:
                        pos["wait_reset"] = False
                    d = 0.0
                if pos["blocked_bars"] > 0:
                    pos["blocked_bars"] -= 1
                    d = 0.0
                cur = pos["qty"] * prices[s] / eq if eq > 0 else 0.0
                want[s] = (round_exposure(d, cur, band), cur)
            for s, (w, cur) in want.items():  # ventes d'abord
                if w < cur - 1e-9:
                    pos = st["positions"][s]
                    q = pos["qty"] - w * eq / prices[s] if w > 0 else pos["qty"]
                    self._sell(s, q, prices[s], "signal", eq)
            for s, (w, cur) in want.items():  # puis achats
                if w > cur + 1e-9:
                    self._buy(s, w * eq / prices[s] - st["positions"][s]["qty"], prices[s], eq)
            # stop suiveur sur la clôture de la bougie
            for s, pos in st["positions"].items():
                a = ind.atr(data[s], self.risk.atr_n).iloc[-1]
                if pos["qty"] > 0 and self.risk.stop_atr and np.isfinite(a):
                    close = float(data[s]["close"].iloc[-1])
                    pos["peak"] = max(pos["peak"] or close, close)
                    new_stop = pos["peak"] - self.risk.stop_atr * a
                    pos["stop"] = new_stop if pos["stop"] is None else max(pos["stop"], new_stop)

        eq = self._equity(prices)
        snap = {s: {"price": prices[s], "exposure": p["qty"] * prices[s] / eq if eq > 0 else 0.0, "stop": p["stop"]}
                for s, p in st["positions"].items()}
        new = not self.equity_path.exists()
        with self.equity_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "equity", "cash", *[f"{s}_exposure" for s in self.symbols]])
            w.writerow([_now(), f"{eq:.2f}", f"{st['cash']:.2f}", *[f"{snap[s]['exposure']:.3f}" for s in self.symbols]])
        self._save()
        return {"time": _now(), "equity": eq, "pnl_pct": (eq / st["initial_capital"] - 1) * 100, "positions": snap}

    @staticmethod
    def status_line(snap: dict) -> str:
        parts = [f"{s}: {v['price']:,.2f} poids {v['exposure']:.0%}" + (f" stop {v['stop']:,.0f}" if v["stop"] else "")
                 for s, v in snap["positions"].items()]
        return f"[{snap['time']}] capital fictif {snap['equity']:,.2f} $ ({snap['pnl_pct']:+.2f} %) | " + " | ".join(parts)

    def run(self, poll_seconds: int = 60, max_cycles: int | None = None):
        print(f"Paper trading « {self.name} » — {self.strategy.name} {self.strategy.params} sur {', '.join(self.symbols)} ({self.interval})")
        print(f"Risque : {asdict(self.risk)}")
        print(f"État : {self.state_path}  (Ctrl+C pour arrêter, relancer reprend où on en était)")
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                print(self.status_line(self.step()), flush=True)
            except Exception as e:  # réseau, API... on retente au prochain cycle
                print(f"[{_now()}] erreur : {e}", flush=True)
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(poll_seconds)
