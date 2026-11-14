"""Paper trading : le bot tourne en temps réel sur les vrais prix Binance, mais avec
un portefeuille FICTIF. Aucun ordre réel n'est jamais envoyé (aucune clé API utilisée).

Même logique que le backtest : décision à la clôture de chaque bougie, frais et slippage
simulés, stop suiveur ATR surveillé à chaque relevé de prix.
L'état est sauvegardé dans state/<nom>.json : on peut arrêter et relancer le bot.
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
from .backtest import BacktestConfig
from .risk import RiskConfig, round_exposure, size_positions
from .strategies import Strategy

STATE_DIR = Path(__file__).resolve().parent.parent / "state"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PaperTrader:
    def __init__(self, strategy: Strategy, symbols: list[str], interval: str = "4h", capital: float = 10_000.0,
                 cfg: BacktestConfig | None = None, risk: RiskConfig | None = None, name: str | None = None,
                 warmup_bars: int = 800, price_fn=None, candles_fn=None, state_dir: Path = STATE_DIR):
        self.strategy, self.symbols, self.interval = strategy, symbols, interval
        self.cfg = cfg or BacktestConfig(capital=capital, bars_per_year=market.BARS_PER_YEAR[interval])
        self.risk = risk or RiskConfig()
        self.name = name or f"{strategy.name}_{interval}"
        self.warmup = warmup_bars
        self.price_fn = price_fn or self._live_price
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
        w = self.cfg.weights or {s: 1 / len(self.symbols) for s in self.symbols}
        return {
            "created": _now(), "strategy": self.strategy.name, "params": self.strategy.params,
            "interval": self.interval, "initial_capital": capital,
            "sleeves": {s: {"cash": capital * w[s], "qty": 0.0, "stop": None, "peak": None, "last_bar": None,
                            "blocked_bars": 0, "entry": None} for s in self.symbols},
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
    def _log_trade(self, symbol: str, side: str, qty: float, price: float, fee: float, reason: str, equity: float):
        new = not self.trades_path.exists()
        with self.trades_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "symbol", "side", "qty", "price", "notional", "fee", "reason", "sleeve_equity"])
            w.writerow([_now(), symbol, side, f"{qty:.8f}", f"{price:.2f}", f"{qty * price:.2f}", f"{fee:.4f}", reason, f"{equity:.2f}"])
        print(f"[{_now()}] {side:4s} {symbol} {qty:.6f} @ {price:,.2f} ({reason})")

    def _trade_to(self, symbol: str, w: float, price: float, reason: str):
        sl = self.state["sleeves"][symbol]
        eq = sl["cash"] + sl["qty"] * price
        target_qty = w * eq / price
        delta = target_qty - sl["qty"]
        if abs(delta) * price < 1.0:  # en dessous de 1 $ : on ignore
            return
        if delta > 0:
            fill = price * (1 + self.cfg.slippage)
            delta = min(delta, sl["cash"] / (fill * (1 + self.cfg.fee)))
            fee = delta * fill * self.cfg.fee
            sl["cash"] -= delta * fill + fee
            if sl["qty"] <= 0:
                sl["entry"] = {"time": _now(), "price": fill}
                sl["peak"] = price
            sl["qty"] += delta
            self._log_trade(symbol, "BUY", delta, fill, fee, reason, eq)
        else:
            q = -delta if w > 0 else sl["qty"]
            fill = price * (1 - self.cfg.slippage)
            fee = q * fill * self.cfg.fee
            sl["cash"] += q * fill - fee
            sl["qty"] -= q
            if w <= 0 or sl["qty"] <= 1e-12:
                sl["qty"], sl["stop"], sl["peak"], sl["entry"] = 0.0, None, None, None
            self._log_trade(symbol, "SELL", q, fill, fee, reason, eq)

    # ---------- boucle ----------
    def step(self) -> dict:
        """Un cycle : stop intra-bougie puis, si une nouvelle bougie est close, nouvelle décision."""
        snapshot = {}
        for sym in self.symbols:
            sl = self.state["sleeves"][sym]
            price = self.price_fn(sym)
            if sl["qty"] > 0 and sl["stop"] is not None and price <= sl["stop"]:
                self._trade_to(sym, 0.0, price, "stop")
                sl["blocked_bars"] = self.cfg.stop_cooldown

            df = self.candles_fn(sym)
            last_bar = str(df.index[-1])
            if last_bar != sl["last_bar"]:
                sl["last_bar"] = last_bar
                target = float(size_positions(df, self.strategy.signal(df), self.risk, self.cfg.bars_per_year).iloc[-1])
                if sl["blocked_bars"] > 0:
                    sl["blocked_bars"] -= 1
                    target = 0.0
                eq = sl["cash"] + sl["qty"] * price
                cur_w = sl["qty"] * price / eq if eq > 0 else 0.0
                w = round_exposure(target, cur_w, self.risk.rebalance_band)
                if abs(w - cur_w) > 1e-9:
                    self._trade_to(sym, w, price, "signal")
                # stop suiveur sur la clôture de la bougie
                a = ind.atr(df, self.risk.atr_n).iloc[-1]
                if sl["qty"] > 0 and self.risk.stop_atr and np.isfinite(a):
                    close = float(df["close"].iloc[-1])
                    sl["peak"] = max(sl["peak"] or close, close)
                    new_stop = sl["peak"] - self.risk.stop_atr * a
                    sl["stop"] = new_stop if sl["stop"] is None else max(sl["stop"], new_stop)
            snapshot[sym] = {"price": price, "equity": sl["cash"] + sl["qty"] * price,
                             "exposure": sl["qty"] * price / max(sl["cash"] + sl["qty"] * price, 1e-9), "stop": sl["stop"]}
        total = sum(v["equity"] for v in snapshot.values())
        new = not self.equity_path.exists()
        with self.equity_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "equity", *[f"{s}_exposure" for s in self.symbols]])
            w.writerow([_now(), f"{total:.2f}", *[f"{snapshot[s]['exposure']:.3f}" for s in self.symbols]])
        self._save()
        return {"time": _now(), "equity": total, "pnl_pct": (total / self.state["initial_capital"] - 1) * 100, "sleeves": snapshot}

    def status_line(self, snap: dict) -> str:
        parts = [f"{s}: {v['price']:,.2f} expo {v['exposure']:.0%}" + (f" stop {v['stop']:,.0f}" if v["stop"] else "")
                 for s, v in snap["sleeves"].items()]
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
