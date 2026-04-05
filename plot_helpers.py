"""
Report-quality figures for MPTCP/TCP Mininet experiments (PNG, matplotlib Agg backend).
Output directory: report_figures/ or $MPTCP_REPORT_FIGS.
"""
from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

FIG_DIR = os.environ.get("MPTCP_REPORT_FIGS", "report_figures")


def ensure_fig_dir() -> str:
    os.makedirs(FIG_DIR, exist_ok=True)
    return FIG_DIR


def _apply_style() -> None:
    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.labelsize": 11,
            "axes.titlesize": 12,
            "legend.fontsize": 9,
            "figure.dpi": 100,
            "savefig.dpi": 200,
            "axes.grid": True,
            "grid.alpha": 0.35,
            "grid.linestyle": "--",
        }
    )


def save_throughput_timeseries(
    series: dict[str, list[float]],
    title: str,
    filename: str,
    ylabel: str = "Throughput (Mbps)",
    xlabel: str = "Time (s)",
    vlines: list[tuple[float, str]] | None = None,
    ylim_zero: bool = True,
) -> str | None:
    """
    Line plot: keys = legend labels, values = per-interval Mbps (x = 1..N seconds).
    vlines: optional vertical lines at x (seconds), with labels for legend.
    Returns path written or None if no series has data.
    """
    _apply_style()
    if not any(series.values()):
        return None
    ensure_fig_dir()
    path = os.path.join(FIG_DIR, filename)

    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    colors = plt.cm.tab10.colors
    for i, (label, ys) in enumerate(series.items()):
        if not ys:
            continue
        xs = list(range(1, len(ys) + 1))
        ax.plot(
            xs,
            ys,
            marker="o",
            markersize=2.5,
            linewidth=1.6,
            label=label,
            color=colors[i % len(colors)],
        )

    if vlines:
        for x, vl_label in vlines:
            ax.axvline(x, color="0.35", linestyle=":", linewidth=1.2, alpha=0.9)
        # One legend entry for all event markers
        ax.plot(
            [],
            [],
            color="0.35",
            linestyle=":",
            linewidth=1.2,
            label=vlines[0][1] if len(vlines) == 1 else "Marked event(s)",
        )

    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if ylim_zero:
        ax.set_ylim(bottom=0)
    ax.legend(loc="best", framealpha=0.92)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  [figure] Saved {path}")
    return path


def save_bar_comparison(
    labels: list[str],
    values: list[float],
    title: str,
    filename: str,
    ylabel: str = "Average throughput (Mbps)",
) -> str:
    """Simple bar chart for summary metrics."""
    _apply_style()
    ensure_fig_dir()
    path = os.path.join(FIG_DIR, filename)
    fig, ax = plt.subplots(figsize=(6.5, 4.0))
    x = range(len(labels))
    palette = ["#4C72B0", "#55A868", "#C44E52", "#8172B2", "#CCB974", "#64B5CD"]
    bars = ax.bar(
        x,
        values,
        color=[palette[i % len(palette)] for i in range(len(labels))],
        edgecolor="0.2",
        linewidth=0.6,
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=15, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(bottom=0)
    for b, v in zip(bars, values):
        ax.annotate(
            f"{v:.1f}",
            xy=(b.get_x() + b.get_width() / 2, b.get_height()),
            ha="center",
            va="bottom",
            fontsize=9,
        )
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  [figure] Saved {path}")
    return path


def save_grouped_bars(
    categories: list[str],
    group_labels: list[str],
    values_by_group: list[list[float]],
    title: str,
    filename: str,
    ylabel: str = "Throughput (Mbps)",
) -> str:
    """Grouped bars: categories on x-axis, one bar per group per category."""
    _apply_style()
    ensure_fig_dir()
    path = os.path.join(FIG_DIR, filename)
    n = len(categories)
    g = len(group_labels)
    width = 0.8 / g
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    x = range(n)
    for gi, (glab, vals) in enumerate(zip(group_labels, values_by_group)):
        offset = (gi - (g - 1) / 2) * width
        ax.bar(
            [xi + offset for xi in x],
            vals,
            width,
            label=glab,
            edgecolor="0.2",
            linewidth=0.5,
        )
    ax.set_xticks(list(x))
    ax.set_xticklabels(categories)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="best", framealpha=0.92)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"  [figure] Saved {path}")
    return path
