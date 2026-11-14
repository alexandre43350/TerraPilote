import numpy as np
import pandas as pd
import pytest

from cryptobot import data as market
from cryptobot import indicators as ind
from cryptobot import strategies
from cryptobot.backtest import BacktestConfig, run, run_portfolio, run_sleeve
from cryptobot.metrics import compute_metrics, drawdown, probabilistic_sharpe
from cryptobot.optimize import walk_forward
from cryptobot.paper import PaperTrader
from cryptobot.profiles import PROFILES
from cryptobot.risk import RiskConfig, round_exposure

NO_RISK = RiskConfig(target_vol=None, stop_atr=None, rebalance_band=0.0)


@pytest.fixture(scope="module")
def df():
    return market.synthetic(n=3000, seed=1)


def _flat_df(prices, start="2024-01-01"):
    prices = np.asarray(prices, float)
    idx = pd.date_range(start, periods=len(prices), freq="4h", tz="UTC")
    return pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices, "volume": 1.0}, index=idx)


# ---------- indicateurs ----------
def test_rsi_bounds(df):
    r = ind.rsi(df["close"]).dropna()
    assert r.between(0, 100).all()


def test_sma_value():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert ind.sma(s, 3).iloc[-1] == pytest.approx(4.0)


def test_donchian_excludes_current_bar(df):
    d = ind.donchian(df, 20)
    assert d["upper"].iloc[50] == pytest.approx(df["high"].iloc[30:50].max())


SINGLE = [n for n in strategies.REGISTRY if n != "rotation"]


@pytest.mark.parametrize("name", SINGLE)
def test_no_lookahead(df, name):
    """Le signal à t ne doit pas changer si on supprime les données postérieures à t."""
    strat = strategies.get(name)
    full = strat.signal(df)
    for k in (800, 1500, 2200):
        part = strat.signal(df.iloc[:k])
        pd.testing.assert_series_equal(part, full.iloc[:k], check_names=False)


@pytest.mark.parametrize("name", SINGLE)
def test_signal_range(df, name):
    s = strategies.get(name).signal(df)
    assert s.between(0, 1).all()


# ---------- moteur ----------
def test_buy_hold_no_costs_matches_price():
    d = _flat_df([100, 100, 110, 121, 133.1])
    cfg = BacktestConfig(fee=0, slippage=0)
    eq, *_ = run_sleeve(d, pd.Series(1.0, index=d.index), 1000, cfg, NO_RISK)
    # décision à la clôture de la barre 0, achat à l'ouverture de la barre 1 (100)
    assert eq.iloc[-1] == pytest.approx(1000 * 133.1 / 100)


def test_execution_is_next_open():
    d = _flat_df([100, 200, 200])
    d["open"] = [100, 150, 200]
    cfg = BacktestConfig(fee=0, slippage=0)
    eq, held, trades, _ = run_sleeve(d, pd.Series([1.0, 1.0, 1.0], index=d.index), 1000, cfg, NO_RISK)
    assert trades[0]["entry_price"] == pytest.approx(150)  # pas au prix de clôture de la barre 0


def test_fees_reduce_equity():
    d = _flat_df(np.full(10, 100.0))
    sig = pd.Series([1, 0] * 5, index=d.index, dtype=float)
    eq, _, trades, fees = run_sleeve(d, sig, 1000, BacktestConfig(fee=0.001, slippage=0.0005), NO_RISK)
    assert eq.iloc[-1] < 1000
    assert fees > 0
    assert len(trades) >= 4


def test_flat_signal_keeps_cash():
    d = market.synthetic(500, seed=3)
    eq, *_ = run_sleeve(d, pd.Series(0.0, index=d.index), 1000, BacktestConfig(), NO_RISK)
    assert (eq == 1000).all()


def test_stop_loss_exits_on_crash():
    prices = [100] * 30 + [101, 102, 103, 60, 50, 40]
    d = _flat_df(prices)
    d["high"] = d["close"] * 1.01
    d["low"] = d["close"] * 0.99
    risk = RiskConfig(target_vol=None, stop_atr=3.0, rebalance_band=0.0)
    eq, held, trades, _ = run_sleeve(d, pd.Series(1.0, index=d.index), 1000, BacktestConfig(fee=0, slippage=0), risk)
    assert any(t["reason"] == "stop" for t in trades)
    assert held.iloc[-1] == 0
    assert eq.iloc[-1] > 1000 * 40 / 100  # bien mieux que de subir toute la baisse


def test_never_negative_cash(df):
    res = run({"A": df}, strategies.get("ensemble"), BacktestConfig(), RiskConfig())
    assert (res.equity > 0).all()
    assert (res.exposure <= 1.0 + 1e-9).all().all()


def test_round_exposure():
    assert round_exposure(0.0, 0.5, 0.25) == 0.0
    assert round_exposure(0.3, 0.0, 0.25) == 0.3
    assert round_exposure(0.6, 0.5, 0.25) == 0.5
    assert round_exposure(0.9, 0.5, 0.25) == 0.9


# ---------- statistiques ----------
def test_drawdown():
    eq = pd.Series([100, 120, 60, 130.0])
    assert drawdown(eq).min() == pytest.approx(-0.5)


def test_metrics_basic():
    idx = pd.date_range("2024-01-01", periods=366, freq="1D", tz="UTC")
    eq = pd.Series(np.linspace(100, 200, 366), index=idx)
    m = compute_metrics(eq, None, 365)
    assert m["total_return"] == pytest.approx(1.0)
    assert m["cagr"] == pytest.approx(1.0, rel=1e-3)
    assert m["max_drawdown"] == 0


def test_psr_increases_with_edge():
    rng = np.random.default_rng(0)
    noise = rng.normal(0, 0.01, 2000)
    assert probabilistic_sharpe(noise + 0.002) > probabilistic_sharpe(noise) > probabilistic_sharpe(noise - 0.002)


# ---------- walk-forward ----------
def test_walk_forward_is_out_of_sample():
    d = {"A": market.synthetic(4000, seed=5), "B": market.synthetic(4000, seed=6)}
    wf = walk_forward(d, strategies.get("ema_cross"), BacktestConfig(), RiskConfig(), train_days=300, test_days=90)
    assert len(wf.folds) >= 3
    # chaque période de test commence après la fin de sa période d'entraînement
    assert (wf.folds["test_start"] > wf.folds["train_start"]).all()
    assert wf.oos.equity.index[0] == wf.folds["test_start"].iloc[0]
    assert 0 <= wf.deflated_sharpe <= 1


# ---------- portefeuille / rotation / profils ----------
def test_shared_cash_never_overspends():
    d = {"BTCUSDT": market.synthetic(2000, seed=11), "ETHUSDT": market.synthetic(2000, seed=12)}
    tg = {s: pd.Series(0.9, index=df.index) for s, df in d.items()}  # 180 % demandés au total
    eq, held, _, _ = run_portfolio(d, tg, 1000, BacktestConfig(), NO_RISK)
    assert (held.sum(axis=1) <= 1.0 + 1e-6).all()
    assert (eq > 0).all()


def test_rotation_weights_bounded_and_filtered():
    d = {"BTCUSDT": market.synthetic(3000, seed=21), "ETHUSDT": market.synthetic(3000, seed=22)}
    for params in ({}, {"tilt": 0.5, "concentrate": True}):
        w = strategies.get("rotation", **params).portfolio_weights(d, RiskConfig(), 365 * 6)
        total = w["BTCUSDT"] + w["ETHUSDT"]
        assert (total <= 1.0 + 1e-9).all() and (total >= 0).all()
        btc = d["BTCUSDT"]["close"]
        risk_off = btc <= ind.ema(btc, 600)
        assert (total[risk_off] == 0).all()  # filtre de marché : rien acheté quand BTC est baissier


def test_rotation_no_lookahead():
    d = {"BTCUSDT": market.synthetic(2500, seed=31), "ETHUSDT": market.synthetic(2500, seed=32)}
    st = strategies.get("rotation")
    full = st.portfolio_weights(d, RiskConfig(), 365 * 6)
    part = st.portfolio_weights({s: df.iloc[:1800] for s, df in d.items()}, RiskConfig(), 365 * 6)
    for s in d:
        pd.testing.assert_series_equal(part[s], full[s].iloc[:1800], check_names=False)


def test_profiles_run():
    d = {"BTCUSDT": market.synthetic(2500, seed=41), "ETHUSDT": market.synthetic(2500, seed=42)}
    for name, prof in PROFILES.items():
        res = run(d, strategies.get(prof["strategy"], **prof["params"]), BacktestConfig(), RiskConfig(**prof["risk"]))
        assert np.isfinite(res.metrics["sharpe"]), name
        assert "losing_12m" in res.metrics


def test_reentry_fresh_waits_for_signal_reset():
    prices = [100] * 30 + [101, 102, 103, 60] + [61 + i for i in range(30)]
    d = _flat_df(prices)
    d["high"], d["low"] = d["close"] * 1.01, d["close"] * 0.99
    sig = pd.Series(1.0, index=d.index)  # le signal ne retombe jamais à 0
    risk = RiskConfig(target_vol=None, stop_atr=3.0, rebalance_band=0.0, reentry="fresh")
    _, held, trades, _ = run_sleeve(d, sig, 1000, BacktestConfig(fee=0, slippage=0, stop_cooldown=2), risk)
    assert trades[0]["reason"] == "stop"
    assert held.iloc[-1] == 0  # pas de rachat sans nouveau signal


# ---------- paper trading ----------
def _trend_hist(n=2600):
    hist = market.synthetic(n, seed=7)
    hist["close"] = np.linspace(100, 300, len(hist))
    hist["open"] = hist["close"].shift().fillna(100)
    hist["high"], hist["low"] = hist["close"] * 1.005, hist["close"] * 0.995
    return hist


def test_paper_trader_cycle(tmp_path):
    hist = _trend_hist()
    state = {"n": 2000}
    candles = lambda s: hist.iloc[: state["n"]]
    price = lambda s: float(hist["close"].iloc[state["n"] - 1])
    pt = PaperTrader(strategies.get("ema_cross"), ["BTCUSDT"], "4h", 1000, risk=RiskConfig(target_vol=None),
                     price_fn=price, candles_fn=candles, state_dir=tmp_path)
    snap = pt.step()
    assert snap["positions"]["BTCUSDT"]["exposure"] > 0.9
    # même bougie : aucune nouvelle décision
    pt.step()
    assert len((tmp_path / f"{pt.name}_trades.csv").read_text().strip().splitlines()) == 2
    # reprise depuis l'état sauvegardé
    pt2 = PaperTrader(strategies.get("ema_cross"), ["BTCUSDT"], "4h", 1000, price_fn=price, candles_fn=candles, state_dir=tmp_path)
    assert pt2.state["positions"]["BTCUSDT"]["qty"] == pytest.approx(pt.state["positions"]["BTCUSDT"]["qty"])
    # krach : le stop doit couper la position
    stop = pt2.state["positions"]["BTCUSDT"]["stop"]
    pt2.price_fn = lambda s: stop * 0.9
    pt2.step()
    assert pt2.state["positions"]["BTCUSDT"]["qty"] == 0


def test_paper_trader_portfolio_strategy(tmp_path):
    hist = _trend_hist()
    candles = lambda s: hist.iloc[:2000]
    price = lambda s: float(hist["close"].iloc[1999])
    prof = PROFILES["prudent"]
    pt = PaperTrader(strategies.get(prof["strategy"]), ["BTCUSDT", "ETHUSDT"], "4h", 1000,
                     risk=RiskConfig(**prof["risk"]), price_fn=price, candles_fn=candles, state_dir=tmp_path)
    snap = pt.step()
    total = sum(v["exposure"] for v in snap["positions"].values())
    assert 0 < total <= 1.0 + 1e-9
    assert pt.state["cash"] >= 0
