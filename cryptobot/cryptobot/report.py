"""Rapports : tableaux console, graphiques PNG et résumé Markdown."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from .metrics import LABELS, drawdown, fmt  # noqa: E402

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"

MAIN_KEYS = ["final_equity", "total_return", "cagr", "volatility", "sharpe", "sortino", "max_drawdown",
             "calmar", "max_dd_duration_days", "psr", "avg_exposure", "n_trades", "trades_per_year",
             "win_rate", "profit_factor", "avg_win_pct", "avg_loss_pct", "expectancy_pct",
             "avg_duration_days", "stop_exits", "fees_paid"]


def metrics_table(columns: dict[str, dict], keys: list[str] = MAIN_KEYS) -> str:
    """Tableau texte : une colonne par jeu de métriques."""
    names = list(columns)
    w0 = max(len(LABELS[k][0]) for k in keys if k in LABELS) + 2
    widths = [max(len(n), 12) + 2 for n in names]
    lines = ["".ljust(w0) + "".join(n.rjust(w) for n, w in zip(names, widths))]
    lines.append("-" * len(lines[0]))
    for k in keys:
        if k not in LABELS or all(k not in m for m in columns.values()):
            continue
        row = LABELS[k][0].ljust(w0)
        row += "".join(fmt(k, columns[n].get(k)).rjust(w) for n, w in zip(names, widths))
        lines.append(row)
    return "\n".join(lines)


def markdown_table(columns: dict[str, dict], keys: list[str] = MAIN_KEYS) -> str:
    names = list(columns)
    out = ["| Métrique | " + " | ".join(names) + " |", "|---|" + "---:|" * len(names)]
    for k in keys:
        if k not in LABELS or all(k not in m for m in columns.values()):
            continue
        out.append(f"| {LABELS[k][0]} | " + " | ".join(fmt(k, columns[n].get(k)) for n in names) + " |")
    return "\n".join(out)


def plot_equity(curves: dict[str, pd.Series], path: Path, title: str, log: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    palette = ["#2a6fdb", "#9aa0a6", "#e8710a", "#1e8e3e", "#d93025", "#9334e6", "#12b5cb", "#f9ab00"]
    for (name, eq), color in zip(curves.items(), palette):
        style = {"lw": 1.2, "color": color}
        if "hold" in name.lower():
            style.update(ls="--", lw=1.0)
        ax1.plot(eq.index, eq.values, label=name, **style)
        ax2.plot(eq.index, drawdown(eq).values * 100, **style)
    if log:
        ax1.set_yscale("log")
    ax1.set_title(title)
    ax1.set_ylabel("Capital (USDT)")
    ax1.legend(loc="upper left", frameon=False)
    ax1.grid(alpha=0.25)
    ax2.set_ylabel("Drawdown (%)")
    ax2.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
