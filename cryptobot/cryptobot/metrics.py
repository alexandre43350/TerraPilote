"""Statistiques de performance et tests de significativité."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    """Inverse de la loi normale (approximation d'Acklam, précision ~1e-9)."""
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02, 1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02, 6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00, -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > hi:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1


def max_drawdown_duration(equity: pd.Series) -> pd.Timedelta:
    peak_time = equity.index[0]
    peak = equity.iloc[0]
    longest = pd.Timedelta(0)
    for t, v in equity.items():
        if v >= peak:
            peak, peak_time = v, t
        else:
            longest = max(longest, t - peak_time)
    return longest


def sharpe(returns: np.ndarray, bars_per_year: float) -> float:
    sd = returns.std(ddof=1)
    return float(returns.mean() / sd * math.sqrt(bars_per_year)) if sd > 0 else 0.0


def probabilistic_sharpe(returns: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """PSR (Bailey & López de Prado) : probabilité que le vrai Sharpe (par barre) dépasse `sr_benchmark`,
    en tenant compte de la longueur de l'historique, de l'asymétrie et des queues épaisses."""
    n = len(returns)
    sd = returns.std(ddof=1)
    if n < 10 or sd == 0:
        return float("nan")
    sr = returns.mean() / sd
    z = (returns - returns.mean()) / sd
    skew = float((z**3).mean())
    kurt = float((z**4).mean())
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr**2, 1e-12))
    return _norm_cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom)


def deflated_sharpe(returns: np.ndarray, trial_sharpes: list[float]) -> float:
    """DSR : PSR contre le Sharpe maximal attendu PAR HASARD après avoir testé N combinaisons.
    Corrige le biais de sur-optimisation (« data snooping »). `trial_sharpes` : Sharpe par barre de chaque essai."""
    n_trials = len(trial_sharpes)
    if n_trials < 2:
        return probabilistic_sharpe(returns)
    var = float(np.var(trial_sharpes, ddof=1))
    gamma = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - gamma) * _norm_ppf(1 - 1 / n_trials) + gamma * _norm_ppf(1 - 1 / (n_trials * math.e)))
    return probabilistic_sharpe(returns, sr0)


def bootstrap_ci(returns: np.ndarray, bars_per_year: float, n_samples: int = 1000, block: int = 50, seed: int = 0) -> dict:
    """Bootstrap par blocs (préserve l'autocorrélation et les grappes de volatilité).
    Donne un intervalle de confiance à 90 % du Sharpe et du rendement annualisé."""
    rng = np.random.default_rng(seed)
    n = len(returns)
    if n < block * 2:
        return {}
    n_blocks = int(np.ceil(n / block))
    sharpes, cagrs = np.empty(n_samples), np.empty(n_samples)
    log_r = np.log1p(returns)
    for k in range(n_samples):
        starts = rng.integers(0, n - block, n_blocks)
        idx = (starts[:, None] + np.arange(block)).ravel()[:n]
        r = returns[idx]
        sharpes[k] = sharpe(r, bars_per_year)
        cagrs[k] = math.exp(log_r[idx].sum() * bars_per_year / n) - 1
    return {
        "sharpe_ci90": (float(np.percentile(sharpes, 5)), float(np.percentile(sharpes, 95))),
        "cagr_ci90": (float(np.percentile(cagrs, 5)), float(np.percentile(cagrs, 95))),
        "prob_loss": float((cagrs < 0).mean()),
    }


def compute_metrics(equity: pd.Series, trades: pd.DataFrame | None, bars_per_year: float, exposure: pd.DataFrame | None = None) -> dict:
    equity = equity.dropna()
    r = equity.pct_change().dropna().to_numpy()
    years = len(r) / bars_per_year if len(r) else 0
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 and equity.iloc[-1] > 0 else float("nan")
    vol = r.std(ddof=1) * math.sqrt(bars_per_year) if len(r) > 1 else float("nan")
    downside = r[r < 0]
    dd_sd = math.sqrt((downside**2).sum() / len(r)) * math.sqrt(bars_per_year) if len(r) else float("nan")
    mdd = float(drawdown(equity).min())
    m = {
        "start": equity.index[0],
        "end": equity.index[-1],
        "years": years,
        "final_equity": float(equity.iloc[-1]),
        "total_return": float(total),
        "cagr": float(cagr),
        "volatility": float(vol),
        "sharpe": sharpe(r, bars_per_year),
        "sortino": float(r.mean() * bars_per_year / dd_sd) if dd_sd and dd_sd > 0 else float("nan"),
        "max_drawdown": mdd,
        "calmar": float(cagr / -mdd) if mdd < 0 else float("nan"),
        "max_dd_duration_days": max_drawdown_duration(equity).total_seconds() / 86400,
        "psr": probabilistic_sharpe(r),
    }
    if exposure is not None and not exposure.empty:
        m["avg_exposure"] = float(exposure.mean(axis=1).mean())
    if trades is not None and not trades.empty:
        pnl = trades["pnl"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        m.update({
            "n_trades": int(len(trades)),
            "trades_per_year": len(trades) / years if years else float("nan"),
            "win_rate": float(len(wins) / len(trades)),
            "profit_factor": float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
            # En % du capital de la poche à l'entrée : tient compte de la taille de position.
            "avg_win_pct": float(trades.loc[pnl > 0, "portfolio_impact_pct"].mean()) if len(wins) else 0.0,
            "avg_loss_pct": float(trades.loc[pnl <= 0, "portfolio_impact_pct"].mean()) if len(losses) else 0.0,
            "expectancy_pct": float(trades["portfolio_impact_pct"].mean()),
            "avg_duration_days": float(trades["duration"].dt.total_seconds().mean() / 86400),
            "stop_exits": int((trades["reason"] == "stop").sum()),
        })
    else:
        m.update({"n_trades": 0})
    return m


LABELS = {
    "final_equity": ("Capital final", "{:,.0f} $"),
    "total_return": ("Rendement total", "{:+.1%}"),
    "cagr": ("Rendement annualisé (CAGR)", "{:+.1%}"),
    "volatility": ("Volatilité annuelle", "{:.1%}"),
    "sharpe": ("Ratio de Sharpe", "{:.2f}"),
    "sortino": ("Ratio de Sortino", "{:.2f}"),
    "max_drawdown": ("Perte max. depuis un sommet", "{:.1%}"),
    "calmar": ("Ratio de Calmar", "{:.2f}"),
    "max_dd_duration_days": ("Plus longue période sous l'eau", "{:.0f} j"),
    "psr": ("Proba. Sharpe > 0 (PSR)", "{:.1%}"),
    "avg_exposure": ("Exposition moyenne", "{:.0%}"),
    "n_trades": ("Nombre de trades", "{:d}"),
    "trades_per_year": ("Trades par an", "{:.1f}"),
    "win_rate": ("Taux de réussite", "{:.1%}"),
    "profit_factor": ("Profit factor", "{:.2f}"),
    "avg_win_pct": ("Gain moyen (trade gagnant)", "{:+.2f} %"),
    "avg_loss_pct": ("Perte moyenne (trade perdant)", "{:+.2f} %"),
    "expectancy_pct": ("Espérance par trade", "{:+.2f} %"),
    "avg_duration_days": ("Durée moyenne d'un trade", "{:.1f} j"),
    "stop_exits": ("Sorties sur stop", "{:d}"),
    "fees_paid": ("Frais payés", "{:,.0f} $"),
}


def fmt(key: str, value) -> str:
    if key not in LABELS:
        return str(value)
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "—"
        return LABELS[key][1].format(value)
    except (ValueError, TypeError):
        return str(value)
