# Multipath Transport in Heterogeneous Networks (MPTCP vs TCP)

**Internet Architecture and Protocols (CS60008) — term project**

Mininet-based experiments comparing **single-path TCP** with **Linux multipath-style** setups (policy routing, kernel MPTCP endpoints, parallel `iperf3` flows bound per interface) under emulated mobility: path loss, bandwidth collapse, RTT spikes, and primary-path RTT ramp (handover stress). Full write-up and figures are in [`IAP_TermProject/`](IAP_TermProject/) (LaTeX).

---

## Repository layout

| Path | Purpose |
|------|--------|
| [`1.py`](1.py) – [`6.py`](6.py) | Experiment drivers (run in order or standalone) |
| [`plot_helpers.py`](plot_helpers.py) | Matplotlib helpers for PNG time series / bar charts |
| [`graphs/`](graphs/) | Generated figures (`fig01_*.png` … `fig05_*.png`; Exp 6 is log-only in default runs) |
| [`Outputs/`](Outputs/) | Example captured stdout (`1.txt` … `6.txt`) for the report |
| [`IAP_TermProject/`](IAP_TermProject/) | LaTeX report (`Main.tex`, chapters, bibliography) |

---

## Prerequisites

- **Linux** host (or VM) with **Mininet**, **Open vSwitch**, and **`iperf3`** installed.
- **Root** (or `sudo`) for Mininet, `tc` / `netem`, and namespace operations.
- **Kernel MPTCP** enabled for scripts **2–6** (`sysctl net.mptcp.enabled=1`, `ip mptcp`); behavior matches your distro’s MPTCP stack.
- **Python 3** with **matplotlib** if you want PNG plots (see below).

---

## Python dependencies (figures only)

```bash
python3 -m pip install -r requirements.txt
```

If Matplotlib is missing, scripts still run but skip PNG generation (you will see an import notice).

### Where PNGs are written

By default [`plot_helpers.py`](plot_helpers.py) saves under `report_figures/`. To match this repo’s `graphs/` folder:

```bash
export MPTCP_REPORT_FIGS=graphs
sudo -E python3 2.py   # -E preserves the env var under sudo
```

---

## Running experiments

From the repository root:

```bash
cd /path/to/IAP-Assignment-2
export MPTCP_REPORT_FIGS=graphs   # optional but recommended

sudo python3 1.py
sudo python3 2.py
# … through 6.py as needed
```

To save logs like the bundled examples:

```bash
sudo python3 4.py 2>&1 | tee Outputs/4.txt
```

| Script | What it does (short) |
|--------|----------------------|
| **1.py** | Dual path (Wi-Fi-like vs LTE-like caps/delays); **sequential TCP** `iperf3` per path |
| **2.py** | Policy routing + MPTCP endpoints; TCP baselines + **parallel streams** (aggregation) |
| **3.py** | Path **failure** on one interface mid-transfer; surviving path continues |
| **4.py** | Symmetric paths; **bandwidth collapse** on path 2 via `tc` |
| **5.py** | **RTT spike** on path 2; TCP-only vs dual-stream comparison |
| **6.py** | **Handover stress**: path 1 RTT stepped up (`netem`); **BLEST** scheduler; console tables (optional plots if wired) |

> **Note:** Experiments **2–6** use two parallel `iperf3` clients as a practical multipath stand-in; interpret results as emulator-scoped, not guaranteed identical to a single MPTCP socket ID in all cases.

---

## Building the PDF report

```bash
cd IAP_TermProject
pdflatex -interaction=nonstopmode Main.tex
bibtex Main
pdflatex -interaction=nonstopmode Main.tex
pdflatex -interaction=nonstopmode Main.tex
```

Figures are resolved via `\graphicspath{{Figures/}{../graphs/}}` in `Main.tex` (logo under `IAP_TermProject/Figures/`, experiment plots under repo `graphs/`).

---

## Authors

See the title page in [`IAP_TermProject/Front_Matter/synopsis-titlepage.tex`](IAP_TermProject/Front_Matter/synopsis-titlepage.tex) for student names and roll numbers.

---

## References in code / report

- **RFC 8684** (MPTCP), **Mininet** (HotNets 2010), **iperf3**, Linux **`tc`/`netem`** — cited in [`IAP_TermProject/mybib.bib`](IAP_TermProject/mybib.bib).
