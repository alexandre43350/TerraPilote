"""Ligne de commande.

  python -m cryptobot fetch                       # télécharge l'historique BTC/ETH
  python -m cryptobot backtest -s ema_cross       # backtest d'une stratégie
  python -m cryptobot compare                     # toutes les stratégies côte à côte
  python -m cryptobot walkforward -s donchian     # optimisation walk-forward (out-of-sample)
  python -m cryptobot paper -s ensemble           # paper trading temps réel, argent fictif
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from . import data as market
from . import strategies
from .backtest import BacktestConfig, run
from .optimize import walk_forward
from .report import REPORT_DIR, markdown_table, metrics_table, plot_equity
from .profiles import PROFILES
from .risk import RiskConfig


def _parse_params(items: list[str]) -> dict:
    out = {}
    for it in items or []:
        k, v = it.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _risk(a) -> RiskConfig:
    """Profil choisi, puis options explicites de la ligne de commande par-dessus."""
    kw = dict(PROFILES[a.profile]["risk"]) if getattr(a, "profile", None) else {}
    if a.target_vol is not None:
        kw["target_vol"] = a.target_vol or None
    if a.stop_atr is not None:
        kw["stop_atr"] = a.stop_atr or None
    if a.band is not None:
        kw["rebalance_band"] = a.band
    if a.dd_halt:
        kw["max_drawdown_halt"] = a.dd_halt
    if a.reentry:
        kw["reentry"] = a.reentry
    return RiskConfig(**kw)


def _strategy(a):
    """Stratégie : celle du profil, sauf si -s est donné explicitement."""
    name = a.strategy or (PROFILES[a.profile]["strategy"] if a.profile else "ensemble")
    params = {**(PROFILES[a.profile]["params"] if a.profile and not a.strategy else {}), **_parse_params(a.param)}
    return strategies.get(name, **params)


def _cfg(a) -> BacktestConfig:
    return BacktestConfig(capital=a.capital, fee=a.fee, slippage=a.slippage, bars_per_year=market.BARS_PER_YEAR[a.interval])


def _load(a) -> dict[str, pd.DataFrame]:
    if a.synthetic:
        return {s: market.synthetic(n=6000, seed=i, interval=a.interval) for i, s in enumerate(a.symbols)}
    out = {}
    for s in a.symbols:
        df = market.load(s, a.interval, a.start, a.end, refresh=not a.offline)
        print(f"{s} : {len(df)} bougies {a.interval} du {df.index[0]:%Y-%m-%d} au {df.index[-1]:%Y-%m-%d}")
        out[s] = df
    return out


def cmd_fetch(a):
    _load(a)


def cmd_backtest(a):
    data = _load(a)
    strat = _strategy(a)
    res = run(data, strat, _cfg(a), _risk(a))
    print(f"\nStratégie : {strat.name} {strat.params}\n")
    print(metrics_table({strat.name: res.metrics, "Buy & Hold": res.benchmark_metrics}))
    path = plot_equity({strat.name: res.equity, "Buy & Hold": res.benchmark}, REPORT_DIR / f"backtest_{strat.name}.png",
                       f"{strat.name} vs Buy & Hold — {', '.join(data)} {a.interval}")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not res.trades.empty:
        res.trades.to_csv(REPORT_DIR / f"trades_{strat.name}.csv", index=False)
    print(f"\nGraphique : {path}\nTrades : {REPORT_DIR / f'trades_{strat.name}.csv'}")


def cmd_compare(a):
    data = _load(a)
    cfg, risk = _cfg(a), _risk(a)
    cols, curves = {}, {}
    for name in strategies.REGISTRY:
        if name == "buy_hold":
            continue
        res = run(data, strategies.get(name), cfg, risk, benchmark=not cols)
        if not cols:
            cols["Buy & Hold"], curves["Buy & Hold"] = res.benchmark_metrics, res.benchmark
        cols[name], curves[name] = res.metrics, res.equity
    print("\nParamètres par défaut, sur tout l'historique (ATTENTION : in-sample, à confirmer en walk-forward)\n")
    print(metrics_table(cols))
    plot_equity(curves, REPORT_DIR / "compare.png", f"Comparaison des stratégies — {', '.join(data)} {a.interval}")
    print(f"\nGraphique : {REPORT_DIR / 'compare.png'}")


def cmd_walkforward(a):
    data = _load(a)
    if a.strategy == "all":
        jobs = [(n, strategies.get(n), _risk(a)) for n in strategies.REGISTRY if n != "buy_hold"]
    elif a.profile == "all":
        jobs = []
        for prof in PROFILES:
            a.profile = prof
            jobs.append((prof, _strategy(a), _risk(a)))
        a.profile = "all"
    else:
        st = _strategy(a)
        jobs = [(a.profile or st.name, st, _risk(a))]
    cfg = _cfg(a)
    cols, curves, extra, md = {}, {}, {}, []
    for name, strat, risk in jobs:
        print(f"\n=== Walk-forward : {name} ({strat.name}) ===")
        wf = walk_forward(data, strat, cfg, risk, a.train_days, a.test_days, a.objective, verbose=True)
        if not cols:
            cols["Buy & Hold"], curves["Buy & Hold"] = wf.oos.benchmark_metrics, wf.oos.benchmark
        cols[name], curves[name] = wf.oos.metrics, wf.oos.equity
        b = wf.bootstrap
        extra[name] = (f"DSR {wf.deflated_sharpe:.1%} ({wf.n_trials} combinaisons) | Sharpe IC90 "
                       f"[{b['sharpe_ci90'][0]:.2f} ; {b['sharpe_ci90'][1]:.2f}] | CAGR IC90 "
                       f"[{b['cagr_ci90'][0]:+.0%} ; {b['cagr_ci90'][1]:+.0%}] | P(perte) {b['prob_loss']:.0%}") if b else ""
        folds = wf.folds.assign(params=wf.folds["params"].map(lambda p: {k: v for k, v in p.items() if k in strat.grid}))
        print(folds[["test_start", "test_end", "params", "train_score", "test_sharpe"]].to_string(index=False))
        rows = "\n".join(f"| {r.test_start:%Y-%m-%d} | {r.test_end:%Y-%m-%d} | `{r.params}` | {r.train_score:.2f} | {r.test_sharpe:.2f} |"
                         for r in folds.itertuples())
        md.append(f"### {name}\n\n{extra[name]}\n\n| Test du | au | Paramètres choisis | Score entraînement | Sharpe test |\n|---|---|---|---:|---:|\n{rows}")
    print("\nRésultats OUT-OF-SAMPLE (périodes jamais vues pendant l'optimisation)\n")
    print(metrics_table(cols))
    print()
    for n, t in extra.items():
        print(f"{n:16s} {t}")
    suffix = a.strategy or f"profil_{a.profile}" if (a.strategy or a.profile) else "ensemble"
    plot_equity(curves, REPORT_DIR / f"walkforward_{suffix}.png", f"Walk-forward out-of-sample — {', '.join(data)} {a.interval}")
    report = REPORT_DIR / f"walkforward_{suffix}.md"
    report.write_text(f"# Walk-forward {', '.join(data)} {a.interval}\n\n{markdown_table(cols)}\n\n" + "\n\n".join(md))
    print(f"\nGraphique : {REPORT_DIR / f'walkforward_{suffix}.png'}\nRapport : {report}")


def cmd_profiles(a):
    from . import profiles
    for name, p in PROFILES.items():
        print(f"{name:10s} {p['description']}\n{'':10s} stratégie={p['strategy']} risque={p['risk']}")
    print(profiles.__doc__)


def cmd_paper(a):
    from .paper import PaperTrader

    params = _parse_params(a.param)
    if a.optimize:
        print("Choix des paramètres sur les 2 dernières années (walk-forward)…")
        data = _load(a)
        wf = walk_forward(data, _strategy(a), _cfg(a), _risk(a), a.train_days, a.test_days, a.objective)
        params = {**wf.folds.iloc[-1]["params"], **params}
        print(f"Paramètres retenus : {params}")
    a.param = [f"{k}={json.dumps(v)}" for k, v in params.items()]
    strat = _strategy(a)
    trader = PaperTrader(strat, a.symbols, a.interval, a.capital, _cfg(a), _risk(a), name=a.name)
    trader.run(poll_seconds=a.poll, max_cycles=a.cycles)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cryptobot", description="Bot de trading crypto — backtest et paper trading (monnaie fictive)")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    common.add_argument("--interval", default="4h", choices=list(market.INTERVAL_MS))
    common.add_argument("--start", default="2018-01-01")
    common.add_argument("--end", default=None)
    common.add_argument("--capital", type=float, default=10_000.0, help="capital fictif en USDT")
    common.add_argument("--fee", type=float, default=0.001, help="frais par ordre (0.001 = 0,1 %%)")
    common.add_argument("--slippage", type=float, default=0.0005)
    common.add_argument("--profile", default=None, choices=[*PROFILES, "all"],
                        help="profil de risque prêt à l'emploi : prudent, equilibre, dynamique (all : walkforward seulement)")
    common.add_argument("--target-vol", type=float, default=None, help="volatilité annuelle visée, défaut 0.6 (0 = désactivé)")
    common.add_argument("--stop-atr", type=float, default=None, help="stop suiveur en multiples d'ATR, défaut 3 (0 = désactivé)")
    common.add_argument("--band", type=float, default=None, help="seuil de rééquilibrage, défaut 0.25")
    common.add_argument("--reentry", default=None, choices=["cooldown", "fresh"], help="règle de retour après un stop")
    common.add_argument("--dd-halt", type=float, default=0.0, help="coupe-circuit drawdown, ex. 0.35 (0 = désactivé)")
    common.add_argument("--synthetic", action="store_true", help="données synthétiques (hors ligne, pour tester)")
    common.add_argument("--offline", action="store_true", help="n'utiliser que le cache local")

    strat_args = argparse.ArgumentParser(add_help=False)
    strat_args.add_argument("-s", "--strategy", default=None, help=f"{', '.join(strategies.REGISTRY)} ou all (défaut : ensemble)")
    strat_args.add_argument("-p", "--param", nargs="*", default=[], help="ex. -p fast=20 slow=100")

    wf_args = argparse.ArgumentParser(add_help=False)
    wf_args.add_argument("--train-days", type=int, default=730)
    wf_args.add_argument("--test-days", type=int, default=180)
    wf_args.add_argument("--objective", default="sharpe", choices=["sharpe", "calmar", "cagr"])

    sub.add_parser("fetch", parents=[common]).set_defaults(fn=cmd_fetch)
    sub.add_parser("backtest", parents=[common, strat_args]).set_defaults(fn=cmd_backtest)
    sub.add_parser("compare", parents=[common]).set_defaults(fn=cmd_compare)
    sub.add_parser("profiles").set_defaults(fn=cmd_profiles)
    sub.add_parser("walkforward", parents=[common, strat_args, wf_args]).set_defaults(fn=cmd_walkforward)
    pp = sub.add_parser("paper", parents=[common, strat_args, wf_args])
    pp.add_argument("--poll", type=int, default=60, help="secondes entre deux relevés de prix")
    pp.add_argument("--cycles", type=int, default=None, help="nombre de cycles (défaut : infini)")
    pp.add_argument("--name", default=None, help="nom du portefeuille fictif (fichier d'état)")
    pp.add_argument("--optimize", action="store_true", help="choisir les paramètres par walk-forward avant de démarrer")
    pp.set_defaults(fn=cmd_paper)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
