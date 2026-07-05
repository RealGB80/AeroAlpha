"""
AeroAlpha investor dashboard -- Plotly Dash, hosted, password-gated. HYPERMODERN rebuild (2026-06-17).

Reads ONLY the curated store (dashboard_app/data.py). PAPER ONLY: no Kalshi auth/orders/real money; the
dashboard's own login is unrelated to Kalshi. Multi-account capable (env DASH_USERS), one login now.

Pages: Overview / Forecasts (by source) / Edges / Multi-city / Forecast accuracy / Forward validation /
Sandbox (RMSE/lock-in/cities -> monthly P&L) / Risk & honesty / Methodology. Auto-refreshes intraday via a
60s interval (re-reads the store the pipeline rewrites >10x/day). All prior features retained.

Run locally:   python dashboard_app/app.py     (http://127.0.0.1:8050, login from DASH_USERS)
Deploy:        gunicorn dashboard_app.app:server   (see README_DEPLOY.md)
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dash
import dash_auth
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, dash_table, Input, Output, State, ALL, ctx
from dash.development.base_component import Component

from data import table, meta_value
from components import (icon as svg_icon, info as info_tooltip, scope_bar,   # WP-03/06 design-system
                        panel, caption_drawer)

# ---- palette (mirrors assets/theme.css; SHARP RED/GREEN quant-terminal retheme 2026-06-19) ----
# HARD RULE: every CHART uses ONLY green / red / neutral. Positive = bright GREEN, negative = bright RED,
# neutral series = light slate / white. No cyan/violet/amber in charts. The old CYAN/VIOLET/AMBER chart
# constants are REPURPOSED to neutral/green/red so existing call sites keep working without a rename.
# AMBER survives ONLY as a UI badge/pill accent (warn) in the CSS, never as a chart trace color.
BG, PANEL, INK, DIM = "#070809", "rgba(20,24,28,0.90)", "#eef2f3", "#8a949b"
GREEN, RED = "#00e08a", "#ff4d5e"         # brighter, sharper financial green / red
GREEN_DK, RED_DK = "#0bbf78", "#e23b4c"   # muted variants for fills / secondary
NEUTRAL = "#aeb8c0"                        # light slate = neutral data series (replaces blue/violet)
NEUTRAL_DK = "#5b6770"                     # darker slate
MINT = GREEN                              # alias: "MINT" historically == the primary accent (now green)
# REPURPOSED so charts are green/red/neutral ONLY:
CYAN = NEUTRAL                            # was blue -> now neutral slate
VIOLET = "#7f8a93"                        # was violet -> now darker neutral slate
AMBER = "#d9a23a"                        # UI-only (pills/badges); NOT used as a chart trace
ACCENT = GREEN
# colorway: green primary, neutral slate secondary, darker neutral tertiary, red last (loss).
PALETTE = [GREEN, NEUTRAL, VIOLET, GREEN_DK, "#8a949b", RED]
GRIDCOL = "rgba(138,150,158,0.13)"        # gridlines: neutral slate
AXISCOL = "rgba(138,150,158,0.28)"        # axis lines / ticks: neutral slate
STALE_AFTER_MIN = 90                       # global staleness threshold

# WP-02: 8-page IA with real URL routes. ROUTES = [(path, render-key, icon-glyph, nav-label)]; SVG icons
# arrive in WP-03 (glyphs kept meanwhile). LEGACY_ROUTES redirect old deep links to their new parent.
ROUTES = [("/", "overview", "◉", "Overview"),
          ("/run", "bankroll", "$", "$1,000 Run"),
          ("/markets", "markets", "❖", "Markets"),
          ("/model", "model", "◎", "Model & Accuracy"),
          ("/edges", "edges", "↑", "Edges & Validation"),
          ("/capacity", "capacity", "⤢", "Capacity & Risk"),
          ("/lab", "lab", "⚙", "Lab"),
          ("/methodology", "methodology", "≡", "Methodology")]
PATH_TO_KEY = {p: k for p, k, _, _ in ROUTES}
KEY_TO_PATH = {k: p for p, k, _, _ in ROUTES}
LEGACY_ROUTES = {"/overview": "/", "/forecasts": "/model", "/accuracy": "/model",
                 "/multicity": "/edges", "/forward": "/edges", "/quantlab": "/lab",
                 "/scalability": "/capacity", "/risk": "/capacity", "/sandbox": "/lab"}
NAV = [(k, ic, lbl) for _, k, ic, lbl in ROUTES]


def _tpl(fig, h=300, legend=None):
    """Chart-styling pass. Titles live in the card H3 (NOT in Plotly) -> reclaim top margin.
    Capped ticks (x ~7, y ~5), one gridline style (y dotted / x off), legend ONLY when >1 series.
    Pass legend=True/False to force; default (None) auto-shows only for multi-trace figures."""
    if legend is None:
        legend = sum(1 for t in fig.data if getattr(t, "showlegend", None) is not False) > 1
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color=INK, family="Inter, system-ui", size=12.5), colorway=PALETTE,
                      margin=dict(l=58, r=20, t=18, b=44), height=h,
                      title=None, showlegend=legend,
                      legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=DIM), orientation="h",
                                  yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="",
                                  itemsizing="constant"),
                      hoverlabel=dict(bgcolor=PANEL, bordercolor=GRIDCOL,
                                      font=dict(family="Inter, system-ui", size=12.5, color=INK),
                                      align="left"),
                      bargap=0.42, bargroupgap=0.16, uniformtext=dict(mode="hide", minsize=9))
    fig.update_xaxes(gridcolor="rgba(0,0,0,0)", zerolinecolor=AXISCOL, nticks=7,
                     linecolor=GRIDCOL, showline=True, ticks="outside", ticklen=5, tickcolor=GRIDCOL,
                     tickfont=dict(size=12, color=DIM),
                     title_font=dict(size=11.5, color=DIM), automargin=True)
    fig.update_yaxes(gridcolor=GRIDCOL, griddash="dot", zerolinecolor=AXISCOL, nticks=5,
                     linecolor="rgba(0,0,0,0)", showline=False, ticks="", ticklen=0,
                     tickfont=dict(size=12, color=DIM),
                     title_font=dict(size=11.5, color=DIM), automargin=True)
    fig.update_traces(selector=dict(type="bar"),
                      marker=dict(cornerradius=6, line=dict(width=0)),
                      textfont=dict(family="JetBrains Mono, monospace", size=11.5))
    return fig


def graph(fig):
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


# ============================================================================================
# PAGE CACHE + BACKGROUND PRE-WARM (2026-06-25 PERF). Whole-site slowness was the SAME root cause as the
# sandbox: building a page rebuilds dozens of go.Figure objects (~0.7-1.5 s/page; deepcopy + plotly property
# tree). Hand-converting all ~50 panels to dicts is high-regression-risk, so instead we MEMOIZE each rendered
# page and serve the cached tree -- with its figures converted to PLAIN DICTS ONCE (_dictify_figures) so the
# per-request re-serialization is ~7-65 ms instead of ~40-190 ms. A daemon thread RE-RENDERS every page on a
# 60 s cadence so the cache is always warm (data TTL is 120 s, so a cached page is never staler than the data)
# -> investor navigation is a cache hit ~always. Per-process (each gunicorn worker warms its own cache).
_PAGE_CACHE: dict = {}
_PAGE_TTL = 120.0
_PREWARM_INTERVAL = 60.0
_PREWARM_STARTED = False
_PREWARM_LOCK = threading.Lock()


def _dictify_figures(node):
    """Walk a built Dash component tree and replace every go.Figure with its plain-dict form, so a cached page
    re-serializes at dict speed (no repeated go.Figure->JSON). Mutates in place; safe on dicts/strings."""
    if not isinstance(node, Component):
        return node
    fig = getattr(node, "figure", None)
    if fig is not None and hasattr(fig, "to_plotly_json"):
        try:
            node.figure = fig.to_plotly_json()
        except Exception:
            pass
    ch = getattr(node, "children", None)
    if isinstance(ch, Component):
        _dictify_figures(ch)
    elif isinstance(ch, (list, tuple)):
        for c in ch:
            _dictify_figures(c)
    return node


def _render_page(key):
    """Build a page fresh, convert its figures to dicts, and store it in the cache. Returns the tree."""
    tree = RENDER.get(key, render_overview)()
    _dictify_figures(tree)
    _PAGE_CACHE[key] = (time.time(), tree)
    return tree


def _prewarm_loop():
    # WP-08: stagger each gunicorn worker's first sweep so N workers don't rebuild all pages in lockstep
    # (a thundering herd against Neon on cold start). Jitter is per-process, once.
    import random
    time.sleep(random.uniform(0, 12))
    while True:
        for key in list(RENDER.keys()):
            try:
                _render_page(key)
            except Exception:
                pass            # a degraded page never kills the warmer; the live request path still rebuilds
        time.sleep(_PREWARM_INTERVAL)


def _ensure_prewarm():
    """Start the background pre-warmer once per process, lazily on the first request (survives gunicorn
    preload+fork, unlike a thread started at import). Disable with DASH_PREWARM=0 (used by perf tests)."""
    global _PREWARM_STARTED
    if _PREWARM_STARTED or os.environ.get("DASH_PREWARM", "1") != "1":
        return
    with _PREWARM_LOCK:
        if _PREWARM_STARTED:
            return
        threading.Thread(target=_prewarm_loop, daemon=True, name="page-prewarm").start()
        _PREWARM_STARTED = True


_CB_CACHE: dict = {}


def _cb_memo(key, ttl, build):
    """TTL memo for interactive callbacks whose inputs are a small discrete set (e.g. the equity-window radio,
    the calibration-stream dropdown). build() returns the callback's output tuple; any go.Figure in it should
    already be dict-ified by the caller so repeat serves are fast. First pick of each value pays the build,
    repeats within ttl are instant."""
    now = time.time()
    hit = _CB_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = build()
    _CB_CACHE[key] = (now, val)
    return val


def _as_dict_fig(fig):
    """go.Figure -> plain dict (Dash renders natively, fast re-serialize); pass dicts through unchanged."""
    return fig.to_plotly_json() if hasattr(fig, "to_plotly_json") else fig


# ============================================================================================
# FAST DICT FIGURES (2026-06-24 PERF). Constructing a go.Figure validates + deepcopies every trace and
# layout property (~20-100 ms/figure); a PLAIN DICT is ~0.01 ms and Dash renders it natively. The Sandbox
# callback builds 6 figures on EVERY keystroke -> the go path cost ~800 ms/interaction; these helpers drop
# it to a few ms with IDENTICAL visuals. Use _dfig() + the _hline/_vline/_vrect/_ann shape helpers instead
# of go.Figure()/add_*/update_* in any hot-path (interactive-callback) figure.
_X_STYLE = dict(gridcolor="rgba(0,0,0,0)", zerolinecolor=AXISCOL, nticks=7, linecolor=GRIDCOL,
                showline=True, ticks="outside", ticklen=5, tickcolor=GRIDCOL,
                tickfont=dict(size=12, color=DIM), title_font=dict(size=11.5, color=DIM), automargin=True)
_Y_STYLE = dict(gridcolor=GRIDCOL, griddash="dot", zerolinecolor=AXISCOL, nticks=5,
                linecolor="rgba(0,0,0,0)", showline=False, ticks="", ticklen=0,
                tickfont=dict(size=12, color=DIM), title_font=dict(size=11.5, color=DIM), automargin=True)


def _dfig(data, h=300, legend=False, xaxis=None, yaxis=None, shapes=None, annotations=None, extra=None):
    """Return a PLAIN-DICT Plotly figure styled exactly like _tpl() (template, margins, legend, axes), but
    WITHOUT constructing a go.Figure (the validation/deepcopy that makes the sandbox slow). xaxis/yaxis are
    style-override dicts merged onto the shared axis styling; shapes/annotations/extra go straight to layout."""
    lay = {"template": "plotly_dark", "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
           "font": {"color": INK, "family": "Inter, system-ui", "size": 12.5}, "colorway": PALETTE,
           "margin": {"l": 58, "r": 20, "t": 18, "b": 44}, "height": h, "title": None, "showlegend": legend,
           "legend": {"bgcolor": "rgba(0,0,0,0)", "font": {"size": 11, "color": DIM}, "orientation": "h",
                      "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0, "title_text": "",
                      "itemsizing": "constant"},
           "hoverlabel": {"bgcolor": PANEL, "bordercolor": GRIDCOL,
                          "font": {"family": "Inter, system-ui", "size": 12.5, "color": INK}, "align": "left"},
           "bargap": 0.42, "bargroupgap": 0.16, "uniformtext": {"mode": "hide", "minsize": 9},
           "xaxis": {**_X_STYLE, **(xaxis or {})}, "yaxis": {**_Y_STYLE, **(yaxis or {})}}
    if shapes:
        lay["shapes"] = shapes
    if annotations:
        lay["annotations"] = annotations
    if extra:
        lay.update(extra)
    return {"data": data, "layout": lay}


def _hline(y, color, width=1.0, dash=None):
    ln = {"color": color, "width": width}
    if dash:
        ln["dash"] = dash
    return {"type": "line", "xref": "paper", "yref": "y", "x0": 0, "x1": 1, "y0": y, "y1": y, "line": ln}


def _vline(x, color, width=1.0, dash=None):
    ln = {"color": color, "width": width}
    if dash:
        ln["dash"] = dash
    return {"type": "line", "xref": "x", "yref": "paper", "x0": x, "x1": x, "y0": 0, "y1": 1, "line": ln}


def _vrect(x0, x1, fillcolor):
    return {"type": "rect", "xref": "x", "yref": "paper", "x0": x0, "x1": x1, "y0": 0, "y1": 1,
            "fillcolor": fillcolor, "line": {"width": 0}, "layer": "below"}


def _ann(x, y, text, color, size=10, xref="x", yref="paper", xanchor="center", yanchor="bottom"):
    return {"x": x, "y": y, "xref": xref, "yref": yref, "text": text, "showarrow": False,
            "font": {"color": color, "size": size}, "xanchor": xanchor, "yanchor": yanchor}


# ============================================================================================
# STATISTICAL GRAPHICS (2026-06-19). Each reads ONE curated table and returns a finished card.
# Real units, formatted numbers, an honest one-line caption. PAPER/forward only -- never live P&L.
# Every panel guards its own empty table so a missing source degrades to an empty-state, not a crash.
# ============================================================================================
def _cap(text):
    return html.Div(text, className="sub", style={"marginBottom": "6px"})


def panel_fills_waterfall():
    """Quoted model edge -> -fee -> -slippage -> realized net, per paper stream. Plotly waterfall."""
    d = table("fills_waterfall")
    if d.empty:
        return card([html.H3("Fills-Realism Waterfall"), empty_state("Fills when settled paper signals log.")])
    figs = []
    for _, r in d.iterrows():
        fig = go.Figure(go.Waterfall(
            orientation="v", measure=["absolute", "relative", "relative", "total"],
            x=["Quoted edge", "− fee", "− slippage", "Realized net"],
            y=[r["quoted_edge_c"], -r["fee_c"], -r["slippage_c"], None],
            text=[fmt_s(r["quoted_edge_c"], "c/ct"), f"−{r['fee_c']:.2f}", f"−{r['slippage_c']:.2f}",
                  fmt_s(r["realized_net_c"], "c/ct")],
            textposition="outside", cliponaxis=False,
            connector=dict(line=dict(color=GRIDCOL, width=1)),
            increasing=dict(marker=dict(color=MINT)), decreasing=dict(marker=dict(color=RED)),
            totals=dict(marker=dict(color=CYAN)),
            hovertemplate="%{x}<br>%{y:+.2f} c/ct<extra></extra>"))
        fig.update_layout(title=None)
        fig.update_yaxes(title="c / contract", ticksuffix="c", tickformat="+.1f")
        figs.append(html.Div([html.Div([html.B(r["stream"]),
                                        html.Span(f"  n={int(r['n'])}", className="sub")]),
                              graph(_tpl(fig, h=230, legend=False))],
                             className="col-4"))
    return panel("Fills-Realism Waterfall — Quoted Edge to Realized Net",
                 [html.Div(figs, className="grid12")],
                 caption="Per stream: gross model edge minus modeled fee and VWAP slippage lands at the "
                         "settled realized net (thin sample, never realized P&L).",
                 drawer=("Per paper stream: the gross model edge at top-of-book, minus modeled fee and minus "
                         "VWAP slippage, lands at the settled realized net. Net is paper/backtest c/contract "
                         "on a thin settled sample (n shown) — never realized P&L. Negative streams "
                         "(S3/S3early) show the fills reality: a quoted edge does not survive frictions."))


def panel_divergence():
    """Model P(yes) vs market mid scatter with the no-disagreement diagonal and shaded edge zones."""
    d = table("divergence")
    if d.empty:
        return card([html.H3("Market-Divergence Ribbon"), empty_state("Fills when edge scans log.")])
    fig = go.Figure()
    # edge-zone ribbons: above the diagonal (model > market -> buy-YES edge) and below (sell)
    xs = [0, 1]
    fig.add_scatter(x=xs + xs[::-1], y=[0.05, 1.05, 1.0, 0.0], fill="toself",
                    fillcolor="rgba(22,199,132,.07)", line=dict(width=0), mode="lines",
                    name="model > market", hoverinfo="skip", showlegend=False)
    fig.add_scatter(x=xs + xs[::-1], y=[-0.05, 0.95, 1.0, 0.0], fill="toself",
                    fillcolor="rgba(234,57,67,.06)", line=dict(width=0), mode="lines",
                    name="model < market", hoverinfo="skip", showlegend=False)
    fig.add_scatter(x=[0, 1], y=[0, 1], mode="lines", name="agreement",
                    line=dict(color=DIM, width=1.4, dash="dash"), hoverinfo="skip")
    # COLOR BY SETTLED OUTCOME (not strategy): WON green / LOST red / PENDING neutral. Strategy -> hover.
    d = d.copy()
    def _oc(w):
        return "won" if w == 1 else ("lost" if w == 0 else "pending")
    d["_oc"] = d["win"].apply(_oc)
    if "strategy" not in d.columns:
        d["strategy"] = "—"
    d["strategy"] = d["strategy"].fillna("—")
    for key, label, color in (("won", "WON", GREEN), ("lost", "LOST", RED),
                              ("pending", "PENDING (unsettled)", NEUTRAL)):
        sub = d[d["_oc"] == key]
        if sub.empty:
            continue
        fig.add_scatter(x=sub["market_mid"], y=sub["model_p"], mode="markers", name=label,
                        marker=dict(size=9, color=color, opacity=.82 if key != "pending" else .5,
                                    line=dict(width=1, color="rgba(255,255,255,.2)")),
                        customdata=sub[["ticker", "strategy", "edge"]].values,
                        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]} · " + label +
                                      "<br>market mid %{x:.2f} · model %{y:.2f}"
                                      "<br>edge %{customdata[2]:+.2f}<extra></extra>")
    fig.update_layout(title=None)
    fig.update_xaxes(title="market mid (implied P)", range=[0, 1], tickformat=".0%")
    fig.update_yaxes(title="model P(yes)", range=[0, 1], tickformat=".0%")
    return panel("Market-Divergence — Where We Fired vs the Market, by Outcome",
                 [graph(_tpl(fig, h=360))],
                 caption="Model P(yes) vs market mid per scanned contract, colored by settled outcome "
                         "(green won / red lost / dim pending).",
                 drawer=("Each point is a scanned contract: our model P(yes) vs the market mid, colored by "
                         "settled outcome (green = won, red = lost, dim = not yet settled). The strategy "
                         "(S1/S3/S3early) is in the hover. On the dashed agreement line we have no view; the "
                         "green band (model above market) is our buy-YES edge zone, red the opposite. "
                         "Paper/forward scans only."))


def panel_edge_success():
    """Edge magnitude vs paper return per contract — does a BIGGER model edge actually pay? Success here is
    realized paper NET (EV), NOT directional win-rate (a big-edge longshot loses often yet can be +EV).
    Source: divergence table (settled forward signals carry realized_net)."""
    d = table("divergence")
    if d.empty or "realized_net" not in d.columns:
        return card([html.H3("Edge vs Outcome — Does a Bigger Edge Pay?"),
                     empty_state("Fills when settled paper signals carry realized net.")])
    import numpy as _np
    d = d.copy()
    d = d[d["realized_net"].notna() & d["edge"].notna()]
    if len(d) < 6:
        return card([html.H3("Edge vs Outcome — Does a Bigger Edge Pay?"),
                     empty_state(f"Accumulating — {len(d)} settled paper signals so far (need a few more).")])
    d["abs_edge"] = d["edge"].abs()
    if "strategy" not in d.columns:
        d["strategy"] = "—"
    d["strategy"] = d["strategy"].fillna("—")
    fig = go.Figure()
    fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
    for w, label, color in ((1, "won", GREEN), (0, "lost", RED)):
        sub = d[d["win"] == w]
        if sub.empty:
            continue
        fig.add_scatter(x=sub["abs_edge"], y=sub["realized_net"], mode="markers", name=label,
                        marker=dict(size=9, color=color, opacity=.8,
                                    line=dict(width=1, color="rgba(255,255,255,.2)")),
                        customdata=sub[["ticker", "strategy"]].values,
                        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]} · " + label +
                                      "<br>|edge| %{x:.2f} · paper net $%{y:+.2f}/ct<extra></extra>")
    # binned-mean trend: does mean paper net rise with edge size?
    hi = float(d["abs_edge"].max()) + 1e-9
    bins = _np.linspace(0, hi, 6)
    cx, cy = [], []
    for i in range(len(bins) - 1):
        m = (d["abs_edge"] >= bins[i]) & (d["abs_edge"] < bins[i + 1])
        if int(m.sum()) >= 2:
            cx.append((bins[i] + bins[i + 1]) / 2)
            cy.append(float(d.loc[m, "realized_net"].mean()))
    if len(cx) >= 2:
        fig.add_scatter(x=cx, y=cy, mode="lines+markers", name="binned mean",
                        line=dict(color=NEUTRAL, width=2), marker=dict(size=7),
                        hovertemplate="|edge| ~%{x:.2f}<br>mean paper net $%{y:+.2f}/ct<extra></extra>")
    r = float(_np.corrcoef(d["abs_edge"], d["realized_net"])[0, 1]) if len(d) > 2 else float("nan")
    rtxt = f"  ·  Pearson r = {r:+.2f}" if r == r else ""
    fig.update_layout(title=None)
    fig.update_xaxes(title="model edge magnitude  |edge|  (implied-prob)", tickformat=".0%")
    fig.update_yaxes(title="paper net ($ / contract)", tickprefix="$", tickformat="+.2f")
    return panel("Edge vs Outcome — Does a Bigger Edge Pay?",
                 [graph(_tpl(fig, h=340))],
                 caption=(f"Model edge magnitude vs realized paper net per contract (win/loss + binned mean); "
                          f"success = EV, not hit-rate. n={len(d)} settled.{rtxt}"),
                 drawer=("Each settled paper signal: model edge magnitude (x) vs realized paper NET per "
                         "contract (y), colored by win/loss; the grey line is the binned mean. Success is paper "
                         "RETURN (EV), NOT hit-rate — a big-edge longshot can lose often yet pay, so we measure "
                         f"dollars, not wins. n={len(d)} settled, paper/forward, thin — directional only.{rtxt}"))


def panel_pit():
    """PIT histogram (10 bins) with the uniform expectation line and a KS 95% uniformity band."""
    d = table("pit")
    if d.empty:
        return card([html.H3("Calibration — PIT Histogram"), empty_state("Fills when PIT report runs.")])
    n = int(d["n"].iloc[0])
    exp = float(d["expected"].iloc[0]); lo = float(d["band_lo"].iloc[0]); hi = float(d["band_hi"].iloc[0])
    inside = ((d["count"] >= d["band_lo"]) & (d["count"] <= d["band_hi"])).all()
    fig = go.Figure()
    fig.add_scatter(x=list(d["bin"]) + list(d["bin"])[::-1], y=[hi] * len(d) + [lo] * len(d),
                    fill="toself", fillcolor="rgba(120,140,162,.16)", line=dict(width=0),
                    mode="lines", name="KS 95% band", hoverinfo="skip")
    colors = [MINT if (lo <= c <= hi) else AMBER for c in d["count"]]
    fig.add_bar(x=d["bin"], y=d["count"], marker_color=colors, width=0.78, name="observed",
                hovertemplate="bin %{x}<br>%{y} of " + str(n) + "<extra></extra>")
    fig.add_scatter(x=d["bin"], y=[exp] * len(d), mode="lines", name="uniform",
                    line=dict(color=DIM, width=1.4, dash="dash"), hoverinfo="skip")
    fig.update_layout(title=None)
    fig.update_yaxes(title="count")
    fig.update_xaxes(title="PIT bin (predicted CDF at the observed high)")
    verdict = "all bins inside the band → calibration not rejected" if inside else \
        "some bins fall outside the band → mild miscalibration"
    return card([html.H3("Calibration — PIT Histogram + KS Band"),
                 _cap(f"Probability Integral Transform of the deployed calibrated ensemble (n={n} day-ahead "
                      f"forecasts). A perfectly calibrated model is flat at the dashed uniform line; bars "
                      f"inside the grey KS 95% band are consistent with uniformity. Here {verdict}. "
                      f"Backtest, leak-free walk-forward PIT."),
                 graph(_tpl(fig, h=320, legend=False))])


# ============================================================================================
# PER-STREAM CALIBRATION (Bayes feed 2026-06-21 -> table "calibration_streams"). Leak-free walk-forward
# PIT / coverage / over-confidence diagnostics of the DEPLOYED forecast, one panel per stream via a
# dropdown, plus an at-a-glance confidence-chip grid across all 10 streams. PAPER/backtest only.
# ============================================================================================
def _conf_chip(direction, s_star):
    """Confidence chip from (direction, s*): green=calibrated, red=overconfident/too-narrow,
    amber=underconfident/too-wide. s* shown to 2dp. Returns an html.Span styled inline."""
    d = (direction or "").lower()
    s = ("" if _isnull(s_star) else f"s*={float(s_star):.2f} — ")
    if "over" in d or "narrow" in d:
        col, bg, txt = RED, "rgba(255,77,94,.10)", f"{s}overconfident (too narrow)"
    elif "under" in d or "wide" in d:
        col, bg, txt = AMBER, "rgba(217,162,58,.12)", f"{s}underconfident (too wide)"
    else:  # WELL_CALIBRATED / calibrated
        col, bg, txt = GREEN, "rgba(0,224,138,.10)", f"{s}well calibrated"
    return html.Span(txt, style={"display": "inline-block", "padding": "3px 9px", "borderRadius": "5px",
                                 "fontSize": "11.5px", "fontFamily": "JetBrains Mono, monospace",
                                 "color": col, "background": bg, "border": f"1px solid {col}55"})


def _stream_label(stream):
    """'NY_high' -> 'NY · high'."""
    parts = (stream or "").rsplit("_", 1)
    return f"{parts[0]} · {parts[1]}" if len(parts) == 2 else str(stream)


def _calib_pit_figure(row):
    """PIT histogram (10 bins) for ONE stream from its pit_bins JSON + uniform line + KS-p annotation.
    Mint bars inside a flat-uniform read, amber bars outside (the per-stream KS p is the honest signal)."""
    import json as _json
    try:
        bins = _json.loads(row["pit_bins"]) if isinstance(row["pit_bins"], str) else list(row["pit_bins"])
    except Exception:
        bins = []
    n = int(sum(bins)) if bins else 0
    exp = n / 10.0 if n else 0.0
    labels = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(len(bins))]
    # honest per-bin colour: deviation beyond a sqrt(exp) Poisson-ish ribbon -> amber, else mint.
    tol = (exp ** 0.5) * 1.6 if exp else 0.0
    colors = [MINT if (exp - tol <= c <= exp + tol) else AMBER for c in bins]
    fig = go.Figure()
    fig.add_bar(x=labels, y=bins, marker_color=colors, width=0.82, name="observed",
                hovertemplate="bin %{x}<br>%{y} of " + str(n) + "<extra></extra>")
    if n:
        fig.add_scatter(x=labels, y=[exp] * len(bins), mode="lines", name="uniform",
                        line=dict(color=DIM, width=1.4, dash="dash"), hoverinfo="skip")
    ksp = row.get("pit_ks_p")
    if not _isnull(ksp):
        unif = bool(int(row.get("pit_uniform") or 0))
        fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.97, xanchor="right", yanchor="top",
                           showarrow=False, align="right",
                           text=(f"KS p = {float(ksp):.3f}<br>"
                                 f"<span style='color:{GREEN if unif else AMBER}'>"
                                 f"{'uniform (not rejected)' if unif else 'flags non-uniform'}</span>"),
                           font=dict(size=11, color=DIM),
                           bordercolor=GRIDCOL, borderwidth=1, borderpad=4, bgcolor=PANEL)
    fig.update_yaxes(title="count")
    fig.update_xaxes(title="PIT bin (predicted CDF at the observed temp)")
    return _tpl(fig, h=300, legend=False)


def _calib_cov_figure(row):
    """Coverage readout: empirical cov80/cov90 vs nominal 0.80 / 0.90 as horizontal bars."""
    cov80, cov90 = row.get("cov80"), row.get("cov90")
    cats, emp, nom = [], [], []
    for lab, val, target in (("90% interval", cov90, 0.90), ("80% interval", cov80, 0.80)):
        if not _isnull(val):
            cats.append(lab); emp.append(float(val)); nom.append(target)
    fig = go.Figure()
    if cats:
        # bar = empirical coverage; colour green if within ~3pts of nominal else amber (mild miscoverage).
        bcol = [GREEN if abs(e - t) <= 0.03 else AMBER for e, t in zip(emp, nom)]
        fig.add_bar(y=cats, x=emp, orientation="h", marker_color=bcol, width=0.5, name="empirical",
                    text=[f"{e*100:.1f}%" for e in emp], textposition="outside",
                    textfont=dict(color=INK, size=12),
                    hovertemplate="%{y}<br>empirical %{x:.1%}<extra></extra>")
        # nominal target markers
        for lab, t in zip(cats, nom):
            fig.add_scatter(y=[lab], x=[t], mode="markers", name="nominal",
                            marker=dict(symbol="line-ns", size=22, color=DIM,
                                        line=dict(width=2, color=DIM)),
                            hovertemplate=f"nominal {t:.0%}<extra></extra>", showlegend=False)
    fig.update_xaxes(title="coverage", range=[0, 1.0], tickformat=".0%")
    fig.update_yaxes(title="")
    return _tpl(fig, h=180, legend=False)


def panel_calibration_streams():
    """PER-STREAM calibration deck: a stream dropdown drives a PIT histogram + coverage bars + a
    confidence chip; below, an at-a-glance chip grid across all 10 deployed streams. Source:
    calibration_streams (Bayes leak-free WF feed). PAPER/backtest -- no realized P&L."""
    d = table("calibration_streams")
    if d.empty:
        return card([html.H3("Per-Stream Calibration — PIT & Coverage"),
                     empty_state("Fills from the per-stream walk-forward calibration feed.")])
    d = d.copy()
    streams = list(d["stream"])
    default = "NY_high" if "NY_high" in streams else streams[0]

    # at-a-glance chip grid (all streams), so the reader sees every stream's verdict without clicking.
    chips = []
    for _, r in d.iterrows():
        chips.append(html.Div([
            html.Span(_stream_label(r["stream"]), style={"color": "var(--ink)", "fontSize": "12px",
                                                          "fontFamily": "JetBrains Mono, monospace",
                                                          "marginRight": "8px", "minWidth": "78px",
                                                          "display": "inline-block"}),
            _conf_chip(r.get("direction"), r.get("s_star"))],
            style={"padding": "4px 0"}))
    chip_grid = html.Div(chips, style={"display": "grid",
                                       "gridTemplateColumns": "repeat(auto-fit, minmax(260px, 1fr))",
                                       "gap": "2px 18px", "marginTop": "8px"})

    return card([
        html.H3("Per-Stream Calibration — PIT, Coverage & Confidence"),
        _cap("Leak-free WALK-FORWARD calibration of the DEPLOYED forecast per stream (Bayes 2026-06-21): "
             "HIGH books = members + S2X, LOW books = the Kelvin ridge-EMOS pool. PIT = predicted CDF at "
             "the realized temperature; a calibrated model is flat at the dashed uniform line and its "
             "intervals cover at the nominal rate. Sigma is well calibrated across all streams — NO "
             "rescale is deployed (the variance lever was tested per stream and is dead, s*≈1). "
             "Paper/backtest research only."),
        html.Div([
            html.Span("Stream", style={"color": DIM, "fontSize": "12px", "marginRight": "8px"}),
            dcc.Dropdown(id="calib-stream", options=[{"label": _stream_label(s), "value": s} for s in streams],
                         value=default, clearable=False, style={"width": "220px"},
                         className="calib-dd")],
            style={"display": "flex", "alignItems": "center", "marginBottom": "8px"}),
        html.Div(id="calib-chip", style={"marginBottom": "6px"}),
        graph_holder("calib-pit"),
        html.Div("Interval coverage — empirical vs nominal (the tick marks)", className="sub",
                 style={"margin": "8px 0 2px"}),
        graph_holder("calib-cov"),
        html.Div(id="calib-meta", className="sub", style={"marginTop": "8px"}),
        html.Hr(style={"border": "none", "borderTop": f"1px solid {GRIDCOL}", "margin": "14px 0 8px"}),
        html.Div("All deployed streams — calibration verdict", className="sub",
                 style={"marginBottom": "2px", "color": "var(--ink)"}),
        chip_grid,
        _cap("NOTE (Crucible red-team 2026-06-21): the earlier 'LAX·high Brier trails market' flag was a "
             "wrong-pool artifact — measured on the base-5 set, not the deployed full pool. On the full pool "
             "LAX·high model BEATS the market (.10651 vs .10683) and its +5.9c subset edge survives Bonferroni; "
             "it stays tradable all-season. CHI·high still trails (.11626 vs .11534) and its edge is COLD-"
             "concentrated, so it is restricted to cold-season trading. Its warm leg is UNCONFIRMED (not a "
             "confirmed loser — the warm loss is year-concentrated in 2025; 2026 is mildly positive; the honest "
             "date-clustered CI touches 0) → traded as a warm watch-track, not booked. The s* calibration is "
             "fine everywhere; an edge can live on the tradable-disagreement subset without leading on Brier."),
    ])


def graph_holder(gid):
    """An empty dcc.Graph the calibration callback fills (keeps the dropdown from re-rendering the page)."""
    return dcc.Graph(id=gid, config={"displayModeBar": False})


def panel_brier_decomp():
    """MURPHY BRIER DECOMPOSITION (Part B): our Brier vs the market's, split into reliability / resolution /
    uncertainty. Stacked bars per book; shows WHY we beat the market (low reliability = calibrated, positive
    resolution = discriminating). Green/red/neutral only. Source: brier_decomp."""
    d = table("brier_decomp")
    if d.empty:
        return card([html.H3("Brier Decomposition (Murphy)"),
                     empty_state("Fills from settled binned predictions vs outcomes.")])
    d = d.copy()
    # bars: reliability (RED = penalty, lower better), resolution shown as a CREDIT (GREEN), uncertainty (NEUTRAL).
    fig = go.Figure()
    fig.add_bar(x=d["who"], y=d["reliability"], name="Reliability (penalty ↓)", marker_color=RED,
                hovertemplate="<b>%{x}</b><br>reliability %{y:.4f} (lower = better calibrated)<extra></extra>")
    fig.add_bar(x=d["who"], y=d["uncertainty"], name="Uncertainty (base)", marker_color=NEUTRAL,
                hovertemplate="<b>%{x}</b><br>uncertainty %{y:.4f} (irreducible)<extra></extra>")
    fig.add_bar(x=d["who"], y=-d["resolution"], name="Resolution (credit ↑)", marker_color=GREEN,
                hovertemplate="<b>%{x}</b><br>resolution %{customdata:.4f} (higher = more discriminating)"
                              "<extra></extra>", customdata=d["resolution"])
    # net Brier marker = reliability - resolution + uncertainty
    fig.add_scatter(x=d["who"], y=d["brier"], mode="markers+text", name="Brier (net)",
                    marker=dict(size=12, color=INK, symbol="diamond",
                                line=dict(width=1.4, color="rgba(255,255,255,.4)")),
                    text=[f"{b:.4f}" for b in d["brier"]], textposition="top center",
                    textfont=dict(color=INK, size=11),
                    hovertemplate="<b>%{x}</b><br>Brier %{y:.4f}<extra></extra>")
    fig.update_layout(title=None, barmode="relative")
    fig.update_yaxes(title="Brier contribution (Brier = reliability − resolution + uncertainty)")
    fig.update_xaxes(title="")
    # honest verdict from the numbers
    md = d[d["who"].str.startswith("Model")]
    mk = d[d["who"] == "Market"]
    n = int(d["n"].iloc[0])
    verdict = ""
    if not md.empty and not mk.empty:
        beats = float(md["brier"].iloc[0]) < float(mk["brier"].iloc[0])
        verdict = (f"The model's Brier {float(md['brier'].iloc[0]):.4f} "
                   f"{'beats' if beats else 'trails'} the market's {float(mk['brier'].iloc[0]):.4f}. "
                   f"Its reliability term ({float(md['reliability'].iloc[0]):.4f}) is the calibration penalty "
                   f"(lower = better); its resolution ({float(md['resolution'].iloc[0]):.4f}) is the "
                   f"discrimination credit (higher = better). ")
    return panel("Brier Decomposition (Murphy) — Why the Model Scores Well",
                 [graph(_tpl(fig, h=340))],
                 caption=(f"{verdict}Brier = reliability − resolution + uncertainty over n={n} settled "
                          f"paper signals."),
                 drawer=(f"{verdict}Brier = reliability − resolution + uncertainty over n={n} settled paper "
                         f"signals. RED reliability is a penalty (low = calibrated); GREEN resolution is a "
                         f"credit (high = discriminates winners from losers); NEUTRAL uncertainty is the "
                         f"irreducible base-rate term, shared by both. Small sample — paper/backtest, the same "
                         f"settled set the forward gates accumulate."))


def panel_lead_decay():
    """LEAD-TIME SKILL-DECAY (Part B): RMSE vs forecast lead. HONEST empty-state today -- the decision archive
    logs a SINGLE horizon, so no real decay curve is buildable; we do NOT fake it. Source: lead_decay."""
    d = table("lead_decay")
    if d.empty:
        return card([html.H3("Lead-Time Skill Decay"),
                     html.Div([
                         empty_state("Needs a multi-lead forecast archive (forward collection)."),
                         _cap("The decision/replay archive currently logs a SINGLE forecast horizon "
                              "(one-day-ahead only), so an honest RMSE-vs-lead curve cannot be drawn yet — "
                              "and we do NOT fabricate one. TO BUILD IT, the pipeline would snapshot the "
                              "Open-Meteo Previous-Runs forecast at MULTIPLE leads (day1…day7) per contract "
                              "date and log forecast_high_f PER LEAD; this panel then plots RMSE by lead from "
                              "those leak-free pairs. Forward-collection item for the data pipeline (Conduit).")],
                         style={"display": "flex", "flexDirection": "column", "gap": "8px"})],
                    id="lead-decay-card")
    # real curve (only if >=2 horizons ever land)
    d = d.copy().sort_values("rmse")
    fig = go.Figure()
    fig.add_scatter(x=d["lead_days"], y=d["rmse"], mode="lines+markers", name="RMSE",
                    line=dict(color=GREEN, width=2.2), marker=dict(size=7, color=GREEN),
                    hovertemplate="lead %{x}<br>RMSE %{y:.2f}°F<extra></extra>")
    fig.update_layout(title=None)
    fig.update_yaxes(title="day-ahead RMSE (°F)", ticksuffix="°F")
    fig.update_xaxes(title="forecast lead")
    return card([html.H3("Lead-Time Skill Decay"),
                 _cap("Forecast RMSE by lead time (multi-lead archive). Backtest."),
                 graph(_tpl(fig, h=300, legend=False))], id="lead-decay-card")


def panel_fan():
    """Forecast fan: deployed forecast high +/- sigma band vs realized observed high, recent dates."""
    d = table("fan")
    if d.empty:
        return card([html.H3("Forecast Fan Chart"), empty_state("Fills from the replay dataset.")])
    fig = go.Figure()
    fig.add_scatter(x=list(d["date"]) + list(d["date"])[::-1], y=list(d["hi2"]) + list(d["lo2"])[::-1],
                    fill="toself", fillcolor="rgba(174,184,192,.10)", line=dict(width=0), mode="lines",
                    name="±2σ", hoverinfo="skip")
    fig.add_scatter(x=list(d["date"]) + list(d["date"])[::-1], y=list(d["hi1"]) + list(d["lo1"])[::-1],
                    fill="toself", fillcolor="rgba(174,184,192,.18)", line=dict(width=0), mode="lines",
                    name="±1σ", hoverinfo="skip")
    fig.add_scatter(x=d["date"], y=d["forecast_f"], mode="lines", name="forecast",
                    line=dict(color=CYAN, width=2, shape="spline", smoothing=0.4),
                    hovertemplate="%{x}<br>forecast %{y:.1f}°F<extra></extra>")
    fig.add_scatter(x=d["date"], y=d["observed_f"], mode="markers", name="observed high",
                    marker=dict(size=6, color=MINT, line=dict(width=1, color="rgba(255,255,255,.25)")),
                    hovertemplate="%{x}<br>observed %{y:.1f}°F<extra></extra>")
    fig.update_layout(title=None)
    fig.update_yaxes(title="daily high (°F)", ticksuffix="°F")
    fig.update_xaxes(title="", nticks=8)
    cov = ((d["observed_f"] >= d["lo1"]) & (d["observed_f"] <= d["hi1"])).mean()
    return panel("Forecast Fan — Predictive Band vs Realized High",
                 [graph(_tpl(fig, h=340))],
                 caption=(f"Day-ahead forecast ±1σ/±2σ vs realized settlement highs; over {len(d)} days "
                          f"{100*cov:.0f}% land inside ±1σ (≈68% is well-calibrated)."),
                 drawer=(f"Deployed day-ahead forecast (σ=1.66°F one-day-ahead) with ±1σ/±2σ predictive bands; "
                         f"dots are the realized settlement high. Over these {len(d)} days {100*cov:.0f}% of "
                         f"realized highs land inside ±1σ (well-calibrated band ≈ 68%). Backtest replay."))


def panel_surprise():
    """Settlement-surprise calendar heatmap: weekday x week, signed forecast error (observed - forecast)."""
    d = table("surprise")
    if d.empty:
        return card([html.H3("Settlement-Surprise Calendar"), empty_state("Fills from the replay dataset.")])
    import pandas as _pd
    d = d.copy()
    d["dt"] = _pd.to_datetime(d["date"], errors="coerce")
    d = d.dropna(subset=["dt"]).sort_values("dt")
    d["week"] = d["dt"].dt.strftime("%Y-W%U")
    d["dow"] = d["dt"].dt.dayofweek
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weeks = list(dict.fromkeys(d["week"]))
    # RECOLOR (deliverable #4): color by CLOSENESS = |error|. Continuous GREEN (close) -> RED (far).
    # A DISTINCT NEUTRAL slate fills NULL/missing days so absent data never reads as a green "we nailed it".
    # z = absolute error (closeness metric); a separate slate background layer marks the null cells.
    z = [[None] * len(weeks) for _ in range(7)]          # |error| where present
    sgn = [[None] * len(weeks) for _ in range(7)]        # signed error (hover only)
    cd = [[None] * len(weeks) for _ in range(7)]
    nullmask = [[1] * len(weeks) for _ in range(7)]      # 1 = null/missing day -> slate
    wi = {w: i for i, w in enumerate(weeks)}
    for _, r in d.iterrows():
        di, wj = int(r["dow"]), wi[r["week"]]
        z[di][wj] = abs(float(r["error_f"]))
        sgn[di][wj] = float(r["error_f"])
        cd[di][wj] = r["date"]
        nullmask[di][wj] = None                          # has data -> not slate
    zmax = max((abs(float(e)) for e in d["error_f"]), default=4.0)
    zmax = max(zmax, 4.0)                                  # keep the upper red end stable
    fig = go.Figure()
    # neutral slate layer for NULL cells (drawn first, underneath)
    fig.add_trace(go.Heatmap(z=nullmask, x=weeks, y=dows, xgap=2, ygap=2, showscale=False,
                             colorscale=[[0, NEUTRAL_DK], [1, NEUTRAL_DK]], hoverinfo="skip"))
    # SEGMENTED NON-LINEAR colorscale (deliverable #5): the low end (0-3°F absolute error) was washing out
    # under a linear 0..zmax scale (zmax can be ~8°F, so 0-3°F occupied only ~0-0.37 of the ramp). Here the
    # 0-3°F band is given ~70% of the color range with multiple distinct stops (bright green -> teal-green ->
    # yellow-green -> amber) so small deviations are clearly distinguishable; red is reserved for |err|>3°F.
    # Stops are placed at FIXED °F breakpoints mapped to the 0..zmax domain so semantics stay green=close /
    # red=far regardless of the actual max. NULL/no-data days remain the neutral slate layer beneath.
    def _f(stop_f):                                       # °F breakpoint -> 0..1 position on the scale
        return min(1.0, max(0.0, stop_f / zmax))
    seg = sorted({0.0: GREEN,                              # 0°F  -> bright green (nailed it)
                  _f(0.75): "#3fd29a",                     # 0.75°F -> teal-green
                  _f(1.5): "#9ad04a",                      # 1.5°F  -> yellow-green
                  _f(2.25): "#d9c23a",                     # 2.25°F -> yellow
                  _f(3.0): "#e0902e",                      # 3°F    -> amber (boundary close/far)
                  _f(5.0): "#e8562f",                      # 5°F    -> orange-red
                  1.0: RED}.items())                       # zmax   -> red (big surprise)
    # collapse duplicate positions (when zmax is small the high stops can coincide at 1.0)
    seen = {}
    for pos, col in seg:
        seen[round(pos, 4)] = col
    colorscale = [[p, seen[p]] for p in sorted(seen)]
    # closeness layer: green (|err|=0, close) -> red (|err| large, far), high resolution in 0-3°F
    fig.add_trace(go.Heatmap(z=z, x=weeks, y=dows, customdata=sgn, xgap=2, ygap=2,
                             colorscale=colorscale, zmin=0, zmax=zmax,
                             colorbar=dict(title="|err| °F", thickness=10, len=0.8,
                                           tickvals=[0, 0.75, 1.5, 2.25, 3, 5, zmax] if zmax > 5 else None,
                                           tickfont=dict(size=10, color=DIM)),
                             hovertemplate="%{x}<br>error %{customdata:+.1f}°F "
                                           "(|err| %{z:.1f}°F)<extra></extra>"))
    fig.update_layout(title=None)
    fig.update_xaxes(title="", showticklabels=False, nticks=12)
    fig.update_yaxes(title="", autorange="reversed")
    mae = float(d["error_f"].abs().mean())
    return panel("Settlement-Surprise Calendar — Forecast Closeness",
                 [graph(_tpl(fig, h=240, legend=False))],
                 caption=(f"Date grid colored by forecast closeness over ~{len(d)} settled days (green = "
                          f"close, red = >3°F miss); mean absolute error {mae:.2f}°F."),
                 drawer=(f"GitHub-style date grid colored by how CLOSE the forecast was on each of ~{len(d)} "
                         f"settled days. The color scale is HIGH-RESOLUTION in the 0–3°F band (green → teal → "
                         f"yellow-green → amber) so small day-to-day deviations are distinguishable, with RED "
                         f"reserved for larger |error| (>3°F) surprise days; slate-gray = no settled data. Mean "
                         f"absolute error {mae:.2f}°F. Red clusters reveal regime surprises. Backtest."))


def panel_blotter():
    """Recent settled paper signals: model edge / entry / realized net / win-loss colored table."""
    d = table("blotter")
    if d.empty:
        return card([html.H3("Trade Blotter"), empty_state("Fills when settled paper signals log.")])
    show = present(d, drop=["scan_utc"],
                   rename={"model_edge_c": "Model Edge", "net_c": "Net", "entry": "Entry"},
                   fmt={"model_edge_c": _cents, "net_c": _cents,
                        "entry": lambda v: "—" if _isnull(v) else f"{v:.2f}",
                        "win": lambda v: "WIN" if v == 1 else ("LOSS" if v == 0 else "—")},
                   order=["city", "stream", "ticker", "side", "model_edge_c", "entry", "net_c", "win"])
    return panel(["Trade Blotter — Recent Settled Paper Signals  ", info_dot()],
                 [pro_table(show, present_df=False, align_left=("Win",))],
                 caption="The last settled paper signals: model edge at entry, entry price, realized net, "
                         "win/loss. The edge lives in the average, not any one row.",
                 drawer=("The last settled paper signals across streams/cities: model edge at entry, effective "
                         "entry price, realized paper net, and win/loss. Individual outcomes are noisy (small "
                         "stakes, thin sample); the edge lives in the average, not any one row. Paper/forward."))


def panel_funnel():
    """Signal funnel: candidate scans -> disagreement -> filters -> fillable -> net-positive."""
    d = table("funnel")
    if d.empty:
        return card([html.H3("Signal Funnel"), empty_state("Fills when edge scans log.")])
    top = max(d["count"].max(), 1)
    fig = go.Figure(go.Funnel(y=d["stage"], x=d["count"], textposition="inside",
                              textinfo="value+percent initial",
                              marker=dict(color=[GREEN, GREEN_DK, NEUTRAL, NEUTRAL_DK, VIOLET]),
                              connector=dict(line=dict(color=GRIDCOL, width=1)),
                              hovertemplate="%{y}<br>%{x} signals<extra></extra>"))
    fig.update_layout(title=None, margin=dict(l=160, r=20, t=10, b=20))
    return panel("Signal Funnel — Candidate to Net-Positive",
                 [graph(_tpl(fig, h=300, legend=False))],
                 caption="How scanned contracts narrow through each gate to a settled net-positive outcome — "
                         "selectivity is the point.",
                 drawer=("How many scanned contracts survive each gate: a model disagreement, the spread+size "
                         "filters, depth/fillability, and finally a settled net-positive outcome. Most "
                         "candidates are filtered out by design — selectivity is the point. Paper counts."))


def panel_decay():
    """Per-stream cumulative-mean realized net over settled signals (edge half-life / decay sparklines)."""
    d = table("decay")
    if d.empty:
        return card([html.H3("Edge Decay"), empty_state("Fills when settled paper signals log.")])
    fig = go.Figure()
    for strat, color in zip(["S1", "S3early", "S3"], [MINT, CYAN, VIOLET]):
        sub = d[d["stream"] == strat].sort_values("seq")
        if sub.empty:
            continue
        cummean = sub["net_c"].expanding().mean()
        fig.add_scatter(x=sub["seq"], y=cummean, mode="lines+markers", name=f"{strat} (cum. mean)",
                        line=dict(color=color, width=2, shape="linear"),
                        marker=dict(size=4),
                        hovertemplate=strat + " · signal %{x}<br>running net %{y:+.2f} c/ct<extra></extra>")
    fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
    fig.update_layout(title=None)
    fig.update_yaxes(title="running mean net (c / contract)", ticksuffix="c", tickformat="+.0f")
    fig.update_xaxes(title="settled signal # (chronological)")
    return panel("Edge Decay — Running Mean Net per Stream",
                 [graph(_tpl(fig, h=300))],
                 caption="Cumulative-mean realized net per stream as signals settle — a line drifting toward "
                         "zero is an edge decaying or never-real.",
                 drawer=("Cumulative-mean realized net as each settled paper signal lands, per stream. A line "
                         "drifting toward or below zero is an edge decaying or never-real (the early-warning "
                         "we want before committing). Thin samples — directional, not conclusive. "
                         "Paper/backtest."))


def panel_latency():
    """Lock-in latency histogram vs the ~128s METAR floor (why NYC lock-in is a latency artifact)."""
    d = table("latency")
    if d.empty:
        return card([html.H3("Lock-In Latency"), empty_state("Fills from the lock-in latency log.")])
    import numpy as _np
    v = d["latency_s"].astype(float)
    med = float(v.median()); frac = float((v > 128).mean())
    fig = go.Figure()
    fig.add_histogram(x=v, nbinsx=28, marker_color=CYAN, opacity=0.9, name="obs",
                      hovertemplate="%{x:.0f}s<br>%{y} scans<extra></extra>")
    fig.add_vline(x=128, line=dict(color=AMBER, width=1.8, dash="dash"),
                  annotation_text="128s METAR floor", annotation_position="top",
                  annotation_font=dict(color=AMBER, size=10))
    fig.add_vline(x=med, line=dict(color=MINT, width=1.6),
                  annotation_text=f"median {med:.0f}s", annotation_position="top right",
                  annotation_font=dict(color=MINT, size=10))
    fig.update_layout(title=None)
    fig.update_yaxes(title="scans")
    fig.update_xaxes(title="orderbook-publish latency after the :51 obs (seconds)")
    return panel("Lock-In Post-Mortem — Why We Killed It (2026-06-25)",
                 [graph(_tpl(fig, h=300, legend=False))],
                 caption=f"Median publish latency was {med:.0f}s with {100*frac:.0f}% at/above the ~128s METAR "
                         f"floor — the artifact that retired NYC lock-in.",
                 drawer=f"Seconds between the :51 KNYC observation and when the priced orderbook updated "
                        f"(n={len(v)} paper scans). Median was {med:.0f}s and {100*frac:.0f}% sat at/above the "
                        f"~128s METAR floor — which is WHY NYC lock-in was RETIRED: it was a latency artifact of "
                        f"KNYC's slow feed, not a fat edge, and no faster free KNYC source exists. Kept as a "
                        f"retrospective.")


def panel_emos_skill():
    """Deployed EMOS vs baselines: RMSE / CRPS bars (lower is better)."""
    d = table("emos_skill")
    if d.empty:
        return card([html.H3("Ensemble Skill"), empty_state("Fills from the EMOS validation run.")])
    fig = go.Figure()
    colors = [MINT if "deployed" in m else DIM for m in d["model"]]
    fig.add_bar(x=d["model"], y=d["rmse"], marker_color=colors, width=0.6, name="RMSE",
                text=[f"{x:.2f}" for x in d["rmse"]], textposition="outside", cliponaxis=False,
                hovertemplate="%{x}<br>RMSE %{y:.2f}°F<extra></extra>")
    fig.update_layout(title=None)
    fig.update_yaxes(title="honest-test RMSE (°F)", ticksuffix="°F",
                     range=[0, float(d["rmse"].max()) * 1.18])
    fig.update_xaxes(title="")
    return card([html.H3("Ensemble Skill — Deployed EMOS vs Baselines"),
                 _cap("Leak-free honest-test RMSE by model variant (lower is better). The deployed EMOS "
                      "(green) beats the inverse-variance and simple-mean baselines, which is WHY the "
                      "ensemble is preferred. Cold honest-test window — strictly out-of-sample."),
                 graph(_tpl(fig, h=300, legend=False))])


def panel_brier_gauges():
    """Per-city Brier skill vs market as horizontal BULLET BARS (WP-09: replaced the six radial gauges —
    low data-ink, hard to compare — with one sorted bar row, zero line, green/red by sign)."""
    d = table("brier_gauge")
    if d.empty:
        return card([html.H3("Brier Skill vs Market — Per City"),
                     empty_state("Fills from the multi-city edge run.")])
    d = d.sort_values("skill", ascending=True).reset_index(drop=True)   # ascending -> best at top on a barh
    sk = [float(v) * 100.0 for v in d["skill"]]
    colors = [GREEN if v > 0 else RED for v in sk]
    fig = go.Figure()
    fig.add_bar(y=list(d["city"]), x=sk, orientation="h", marker_color=colors, width=0.62,
                text=[f"{v:+.1f}%" for v in sk], textposition="outside", cliponaxis=False,
                textfont=dict(size=11, color=INK),
                hovertemplate="<b>%{y}</b><br>Brier skill %{x:+.1f}% vs market<extra></extra>")
    fig.add_vline(x=0, line=dict(color=DIM, width=1.2, dash="dot"))
    lim = max(8.0, max(abs(v) for v in sk) * 1.25)
    fig.update_xaxes(title="Brier skill vs market (%)", range=[-lim, lim], ticksuffix="%")
    fig.update_yaxes(title="")
    return card([html.H3("Brier Skill vs Market — Per City"),
                 _cap("Brier skill score = 1 − (model Brier ÷ market Brier) on ALL settled day-ahead "
                      "contracts; positive (green) = our probabilities beat the market on aggregate accuracy. "
                      "Only NY clears on this raw-skill aggregate — the validated multi-city S1 edge comes "
                      "from the market's overconfidence on selected contracts, not raw skill everywhere. "
                      "Honest framing, paper/backtest."),
                 graph(_tpl(fig, h=max(180, 40 + 30 * len(d)), legend=False))])


# ============================================================================================
# QUANT-TERMINAL PANELS (Iris 2026-06-19): dense live/terminal + Quant Lab graphics. Green/red/neutral
# ONLY. Each reads ONE curated table, guards its own empty case, and carries an honest paper caption.
# ============================================================================================
def _spark(values, color=None, height=34, fill=True, width=90):
    """Tiny inline sparkline. Green if last>=first else red unless color given.
    PERF (2026-06-25): returns a ~250-byte inline-SVG DATA-URI <img> -- NOT a dcc.Graph/Plotly figure. These
    are built MANY per page (one per open-position row); a Plotly graph is ~1.5 KB of payload AND a full
    Plotly instance to render in the browser (~25 of them was a real client-side drag). A data-URI image is a
    fraction of the payload and renders instantly with zero JS."""
    if not values or len(values) < 2:
        return html.Div(className="spark-empty", style={"height": f"{height}px"})
    color = color or (GREEN if values[-1] >= values[0] else RED)
    vmin, vmax = min(values), max(values)
    rng = (vmax - vmin) or 1.0
    n = len(values); pad = 2.0

    def _x(i):
        return pad + (width - 2 * pad) * i / (n - 1)

    def _y(v):
        return pad + (height - 2 * pad) * (1 - (v - vmin) / rng)

    pts = " ".join(f"{_x(i):.1f},{_y(v):.1f}" for i, v in enumerate(values))
    parts = []
    if fill:
        parts.append(f'<polygon points="{_x(0):.1f},{height - pad:.1f} {pts} {_x(n - 1):.1f},{height - pad:.1f}" '
                     f'fill="rgba({_rgb(color)},0.12)" stroke="none"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
           f'preserveAspectRatio="none">{"".join(parts)}</svg>')
    from urllib.parse import quote
    return html.Img(src="data:image/svg+xml," + quote(svg),
                    style={"width": "100%", "height": f"{height}px", "display": "block"})


def _rgb(hex_or_name):
    h = {"#00e08a": "0,224,138", "#ff4d5e": "255,77,94", "#aeb8c0": "174,184,192"}.get(hex_or_name)
    if h:
        return h
    hx = hex_or_name.lstrip("#")
    if len(hx) == 6:
        return f"{int(hx[0:2],16)},{int(hx[2:4],16)},{int(hx[4:6],16)}"
    return "174,184,192"


def _spark_for(metric):
    """Pull a KPI sparkline series from the kpi_spark table (returns list of floats or [])."""
    d = table("kpi_spark")
    if d.empty or "metric" not in d:
        return []
    sub = d[d["metric"] == metric].sort_values("seq")
    return list(sub["value"]) if not sub.empty else []


def kpi_spark_card(label, value, unit, status, spark_metric=None, delta=None, spark_color=None, tip=None):
    """Compact KPI card: label, big value, optional green/red delta, and a sparkline (reference design)."""
    val, suffix = fmt(value, unit)
    sp = _spark_for(spark_metric) if spark_metric else []
    delta_el = None
    if delta is not None and not _isnull(delta):
        dcls = "kpi-delta pos" if delta >= 0 else "kpi-delta neg"
        delta_el = html.Span(f"{delta:+.2f}", className=dcls)
    badge_kind = {"BACKTEST": "good", "WALK-FORWARD": "good", "FORWARD": "good"}.get(
        status, "warn" if status not in ("NOT STARTED", "PAPER") else "neut")
    label_el = html.Div([label, info_dot(tip)] if tip else label, className="label")
    return html.Div([
        label_el,
        html.Div([html.Span(val, className="val mono"), html.Span(suffix, className="unit"),
                  delta_el], className="kpi-valrow"),
        html.Div(_spark(sp, color=spark_color), className="kpi-spark") if sp else html.Div(),
        badge(status, badge_kind)], className="card kpi kpi-spark-card")


def kpi_spark_row():
    """The dense 6-card KPI strip with sparklines from real history (reference Image 1 KPI ROW)."""
    kpi = table("kpi"); rmse = table("forecast_rmse"); cn = table("city_network")
    eq = table("equity_curve"); strat = table("strategy_perf")
    def kget(name, col="value"):
        r = kpi[kpi["name"] == name] if not kpi.empty and "name" in kpi else kpi.iloc[0:0]
        return r[col].iloc[0] if not r.empty else None
    ny_rmse = kget("ny_rmse")
    cities = kget("cities_beating_market")
    ny_edge = kget("ny_s1_edge_c")
    # best validated stream edge (high or low)
    best_edge = None
    if not strat.empty and "edge_c" in strat:
        ev = strat["edge_c"].dropna()
        best_edge = float(ev.max()) if not ev.empty else None
    # PAPER NET headline = AVG net per contract, matching this card's own "avg net cents/contract"
    # tooltip. equity_c is CUMULATIVE cents and `trades` is the per-day contract count, so the raw last
    # point (+2429c over 771 contracts) was a 10-month total mislabeled "c/ct". Divide to get +3.15 c/ct.
    paper_net_avg = None
    if not eq.empty and "equity_c" in eq and "trades" in eq:
        _cum = float(eq["equity_c"].iloc[-1]); _n = float(eq["trades"].sum())
        paper_net_avg = (_cum / _n) if _n else None
    cards = [
        # WP-06: re-wired to the HONEST series WP-05 now emits (ny_rmse = rolling day-ahead RMSE;
        # best_edge_c = per-stream validated-edge distribution). WP-01 had blanked these because they were
        # plotting the wrong metrics (latency_s / monthly_net_c).
        kpi_spark_card("NY DAY-AHEAD RMSE", ny_rmse, "F", "WALK-FORWARD", "ny_rmse",
                       spark_color=NEUTRAL),
        kpi_spark_card("NY S1 EDGE (S2X)", ny_edge, "c/contract", "BACKTEST", "ny_edge_c"),
        kpi_spark_card("BEST STREAM EDGE", best_edge, "c/contract", "BACKTEST", "best_edge_c"),
        kpi_spark_card("CITIES BEAT MARKET", cities, "cities", "BACKTEST", None),
        kpi_spark_card("PAPER NET (BACKTEST)", paper_net_avg, "c/contract", "BACKTEST", "equity_c",
                       tip=PAPER_NET_TIP),
        kpi_spark_card("LIVE CAPITAL", 0, "$", "PAPER", None),
    ]
    return html.Div(cards, className="grid kpi-strip")


# ---------- Markets / Live page panels ----------
def _utc_to_et_str(v, fmt="%m-%d %H:%M ET"):
    """WP-01: display a UTC scan timestamp as ET wall-clock (the market's clock; site-wide standard).
    Safe on unparseable input -> returns the raw string."""
    try:
        t = pd.to_datetime(v, utc=True, errors="coerce")
        if pd.isna(t):
            return "—" if _isnull(v) else str(v)
        return t.tz_convert("America/New_York").strftime(fmt)
    except (ValueError, TypeError):
        return "—" if _isnull(v) else str(v)


def panel_market_feed(compact=False):
    """Live-ish Kalshi market feed: recent scanned quotes per city/market (time/city/market/quote/edge/side).
    Honest: these are PAPER scans of public quotes, NOT orders. Source: forward monitor logs."""
    d = table("market_feed")
    if d.empty:
        return card([html.H3("Live Market Feed"), empty_state("Fills as the paper monitors scan quotes.")])
    show = present(d, drop=["status"],
                   rename={"scan_utc": "Time", "quote_c": "Quote", "model_p": "Model P", "edge_c": "Edge"},
                   fmt={"scan_utc": _utc_to_et_str,
                        "quote_c": lambda v: "—" if _isnull(v) else f"{v:.0f}c",
                        "model_p": lambda v: "—" if _isnull(v) else f"{v:.0%}",
                        "edge_c": _cents1},
                   order=["scan_utc", "city", "market", "ticker", "side", "quote_c", "model_p", "edge_c"])
    return card([html.H3("Live Market Feed — Paper Quote Scans"),
                 _cap("Most-recent public Kalshi quotes the paper monitors scanned, per city/market: the "
                      "quoted entry, our model probability, and the signed edge. PAPER scans of public "
                      "data — never orders, never real money. Time is ET."),
                 pro_table(show, present_df=False, max_rows=18 if compact else 40,
                           align_left=("Side", "Market"))], cls="feed-card", id="market-feed-card")


def panel_quote_board():
    """Live per-city quote board: latest scanned quote + model P + edge per city (compact tiles). A denser
    'live market info' surface for Markets/Live. Source: market_feed (most-recent scan per city). Paper."""
    d = table("market_feed")
    if d.empty:
        return card([html.H3("Live Quote Board"), empty_state("Fills as the paper monitors scan quotes.")])
    d = d.copy()
    # most-recent row per city (feed is already newest-first)
    seen = {}
    for _, r in d.iterrows():
        c = r["city"]
        if c not in seen:
            seen[c] = r
    tiles = []
    for c, r in seen.items():
        e = r["edge_c"]
        ecls = "pos" if (not _isnull(e) and e >= 0) else ("neg" if not _isnull(e) else "")
        ev = "—" if _isnull(e) else f"{e:+.1f}c"
        q = "—" if _isnull(r["quote_c"]) else f"{r['quote_c']:.0f}c"
        mp = "—" if _isnull(r["model_p"]) else f"{r['model_p']:.0%}"
        tiles.append(html.Div([
            html.Div([html.Span(c, className="qb-city"),
                      html.Span(str(r["market"]), className="qb-mkt")], className="qb-top"),
            html.Div([html.Span("QUOTE ", className="u-label"), html.Span(q, className="mono qb-q")],
                     className="qb-line"),
            html.Div([html.Span("MODEL ", className="u-label"), html.Span(mp, className="mono")],
                     className="qb-line"),
            html.Div([html.Span("EDGE ", className="u-label"),
                      html.Span(ev, className=f"mono qb-edge {ecls}")], className="qb-line")],
            className="qb-tile"))
    return card([html.H3(["Live Quote Board  ", html.Span(className="stream-pulse")]),
                 _cap("Most-recent public Kalshi quote the paper monitors scanned per city/market, with our "
                      "model probability and the signed edge. PAPER scans of public data — no orders. Updates "
                      "as the monitors run."),
                 html.Div(tiles, className="qb-grid")], id="quote-board-card")


def panel_scan_stream():
    """Rolling 'last N scans' stream: a compact terminal-style log of the most recent paper quote scans with
    a subtle streaming pulse. Source: market_feed. Honest: paper scans of public data, not a trade tape."""
    d = table("market_feed")
    if d.empty:
        return card([html.H3("Scan Stream"), empty_state("Fills as the paper monitors scan quotes.")])
    rows = []
    for _, r in d.head(14).iterrows():
        e = r["edge_c"]
        ecls = "pos" if (not _isnull(e) and e >= 0) else ("neg" if not _isnull(e) else "")
        ev = "—" if _isnull(e) else f"{e:+.1f}c"
        rows.append(html.Div([
            html.Span(_utc_to_et_str(r["scan_utc"]), className="ss-t mono"),
            html.Span(str(r["city"]), className="ss-c"),
            html.Span(str(r["market"]), className="ss-m"),
            html.Span(str(r["side"]) if not _isnull(r["side"]) else "—", className="ss-s"),
            html.Span(str(r["ticker"]) if not _isnull(r["ticker"]) else "—", className="ss-tk mono"),
            html.Span(ev, className=f"ss-e mono {ecls}")], className="ss-row"))
    return card([html.H3(["Scan Stream — Last 14 Paper Scans  ", html.Span(className="stream-pulse")]),
                 _cap("Rolling tape of the most recent public-quote scans across the forward monitors (ET). "
                      "These are PAPER scans of public data — not a trade tape, no orders, no real money."),
                 html.Div(rows, className="ss-list")], id="scan-stream-card")


def panel_city_network():
    """City-network map: 7 Kalshi cities as glowing nodes on a US map (geo scatter), sized by edge, colored
    by deployed status, with forecast/temp labels. Honest paper edges; status badges. Source: city_network."""
    d = table("city_network")
    if d.empty:
        return card([html.H3("City Network"), empty_state("Fills from the city forecast snapshot.")])
    d = d.copy()
    stat_color = {"tradable": GREEN, "watch": AMBER, "not-deployed": NEUTRAL}
    colors = [stat_color.get(s, NEUTRAL) for s in d["status"]]
    sizes = [10 + 2.4 * (abs(e) if e == e and e is not None else 0) for e in d["edge_c"].fillna(0)]
    fig = go.Figure()
    # faint arcs from NY to every other city (network look)
    ny = d[d["city"] == "NY"]
    if not ny.empty:
        nlat, nlon = float(ny["lat"].iloc[0]), float(ny["lon"].iloc[0])
        for _, r in d.iterrows():
            if r["city"] == "NY":
                continue
            fig.add_scattergeo(lat=[nlat, r["lat"]], lon=[nlon, r["lon"]], mode="lines",
                               line=dict(width=0.7, color="rgba(0,224,138,.18)"), hoverinfo="skip",
                               showlegend=False)
    fig.add_scattergeo(
        lat=d["lat"], lon=d["lon"], mode="markers+text",
        text=[f"{c}" for c in d["city"]], textposition="top center",
        textfont=dict(size=10, color=INK),
        marker=dict(size=sizes, color=colors, opacity=0.92, line=dict(width=1.2, color="rgba(255,255,255,.4)")),
        customdata=d[["name", "station", "forecast_f", "edge_c", "status"]].values,
        hovertemplate="<b>%{customdata[0]}</b> (%{customdata[1]})<br>forecast %{customdata[2]:.1f}°F"
                      "<br>paper edge %{customdata[3]:+.2f}c · %{customdata[4]}<extra></extra>",
        showlegend=False)
    fig.update_geos(scope="usa", bgcolor="rgba(0,0,0,0)", landcolor="rgba(40,46,52,.55)",
                    lakecolor="rgba(0,0,0,0)", subunitcolor="rgba(138,150,158,.18)",
                    countrycolor="rgba(138,150,158,.25)", coastlinecolor="rgba(138,150,158,.22)",
                    showlakes=False, framecolor="rgba(0,0,0,0)")
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=340, paper_bgcolor="rgba(0,0,0,0)",
                      geo=dict(bgcolor="rgba(0,0,0,0)"))
    return panel("City Network — Day-Ahead Forecast & Paper Edge",
                 [graph(fig)],
                 caption="Kalshi daily-high cities at their settlement stations; node size = paper S1 edge, "
                         "color = deployed status.",
                 drawer=("The seven Kalshi daily-high cities at their settlement stations. Node size = "
                         "validated paper S1 edge magnitude; color = deployed status (green tradable / amber "
                         "watch / slate not-deployed). Arcs are illustrative. Paper/forward, never live P&L."))


# WP-01 interim: CHI-high is deployed COLD-ONLY (see CLAUDE.md / monitor_multicity_s1). Until WP-05 adds a
# `season_scope` store column, surface the restriction display-side so a tradable chip never implies
# all-season. Keyed on the city code; safe on any label ("TRADABLE"/"TRADABLE" variants).
_COLD_ONLY_CITIES = {"CHI"}


def _season_scoped_label(city, base_label):
    if str(city).upper() in _COLD_ONLY_CITIES and base_label.upper().startswith("TRADABLE"):
        return base_label + " · COLD-ONLY"
    return base_label


def panel_city_rank():
    """Ranked city cards (rank, city, station, forecast, paper edge + status) -- the reference 'city cards'."""
    d = table("city_network")
    if d.empty:
        return card([html.H3("City Rankings"), empty_state("Fills from the city forecast snapshot.")])
    d = d.copy()
    d["_e"] = d["edge_c"].fillna(-999)
    d = d.sort_values("_e", ascending=False).reset_index(drop=True)
    stat_kind = {"tradable": "good", "watch": "warn", "not-deployed": "neut"}
    rows = []
    for i, r in d.iterrows():
        edge = "—" if _isnull(r["edge_c"]) else f"{r['edge_c']:+.2f}c"
        ecls = "pos" if (not _isnull(r["edge_c"]) and r["edge_c"] >= 0) else "neg"
        fc = "—" if _isnull(r["forecast_f"]) else f"{r['forecast_f']:.0f}°F"
        rows.append(html.Div([
            html.Span(f"{i+1}", className="cn-rank"),
            html.Div([html.Div(r["name"], className="cn-city"),
                      html.Div(r["station"], className="cn-station")], className="cn-id"),
            html.Div(fc, className="cn-temp mono"),
            html.Div(edge, className=f"cn-edge mono {ecls}"),
            badge(_season_scoped_label(r["city"], str(r["status"]).upper().replace("-", " ")),
                  stat_kind.get(r["status"], "neut"))], className="cn-row"))
    return card([html.H3("City Rankings — by Paper Edge"),
                 _cap("Ranked by validated paper S1 edge. Forecast = ensemble day-ahead high. Status is the "
                      "deployed paper path, not a tall-bar artifact. Paper/forward."),
                 html.Div(rows, className="cn-list")], id="city-rank-card")


def panel_alerts():
    """Severity-tagged operational alerts from the integrity sentinel (CRITICAL/HIGH/MEDIUM/LOW)."""
    d = table("alerts")
    if d.empty:
        return card([html.H3("Alerts"), empty_state("Fills from the integrity sentinel.")])
    sev_kind = {"CRITICAL": "bad", "HIGH": "warn", "MEDIUM": "warn", "LOW": "good"}
    rows = []
    for _, r in d.iterrows():
        rows.append(html.Div([
            badge(r["severity"], sev_kind.get(r["severity"], "neut")),
            html.Div([html.Div(r["name"].replace("_", " ").title(), className="al-name"),
                      html.Div(r["detail"], className="al-detail")], className="al-body")],
            className="al-row"))
    return card([html.H3("Alerts — Integrity Sentinel"),
                 _cap("Live operational alerts from the daily integrity sentinel: settlement alignment, "
                      "calibration drift, source liveness, latency, false-lock guard. Paper-monitor scope."),
                 html.Div(rows, className="al-list")], id="alerts-card")


def panel_source_health():
    """Source-health table: source / status (LIVE/DEGRADED) / freshness / detail (reference design)."""
    d = table("source_health")
    if d.empty:
        return card([html.H3("Source Health"), empty_state("Fills from the integrity sentinel.")])
    rows = []
    for _, r in d.iterrows():
        live = str(r["status"]).upper() == "LIVE"
        age = "—" if _isnull(r["age_min"]) else f"{r['age_min']:.0f}m"
        rows.append(html.Div([
            html.Span(className=f"sh-dot {'live' if live else 'deg'}"),
            html.Div(r["source"], className="sh-src"),
            html.Span("LIVE" if live else "DEGRADED", className=f"sh-st {'live' if live else 'deg'}"),
            html.Div(age, className="sh-age mono"),
            html.Div(str(r["detail"]), className="sh-detail")], className="sh-row"))
    return card([html.H3("Source Health"),
                 _cap("Each public data source the bot reads, its liveness and freshness. All unauthenticated "
                      "public feeds. Paper-monitor telemetry."),
                 html.Div([html.Div([html.Span("SOURCE", className="sh-h"), html.Span("STATUS", className="sh-h"),
                                     html.Span("AGE", className="sh-h"), html.Span("DETAIL", className="sh-h")],
                                    className="sh-row sh-head")] + rows, className="sh-list")],
                id="source-health-card")


def panel_model_drift():
    """Model-drift monitor: per-city RMSE vs deployed sigma drift score + status (reference design)."""
    d = table("model_drift")
    if d.empty:
        return card([html.H3("Model Drift"), empty_state("Fills from the multi-city forecast run.")])
    show = present(d, rename={"drift_score": "Drift", "rmse": "RMSE"},
                   fmt={"drift_score": lambda v: "—" if _isnull(v) else f"{v:.2f}×",
                        "rmse": _degf, "n": _intf},
                   order=["city", "model", "rmse", "drift_score", "status", "n"])
    return card([html.H3("Model-Drift Monitor"),
                 _cap("Per-city day-ahead RMSE as a multiple of the deployed ~1.66°F NY sigma target. A high "
                      "score here means a NOISIER city forecast (DEN/AUS are intrinsically harder), not a "
                      "live degradation — NY/MIA sit on-spec. Backtest/walk-forward."),
                 pro_table(show, present_df=False, align_left=("Status", "Model"))], id="model-drift-card")


def panel_strategy_perf():
    """Strategy-performance table: strategy / paper edge / win-rate / PF / status (reference design)."""
    d = table("strategy_perf")
    if d.empty:
        return card([html.H3("Strategy Performance"), empty_state("Fills as streams validate.")])
    show = present(d, rename={"edge_c": "Paper Edge", "pf": "PF", "win_rate": "Win Rate"},
                   fmt={"edge_c": _cents, "win_rate": _pct01,
                        "pf": lambda v: "—" if _isnull(v) else f"{v:.2f}", "n": _intf,
                        "status": lambda v: str(v).upper()},
                   order=["strategy", "edge_c", "win_rate", "pf", "n", "status", "note"])
    return panel(["Strategy Performance — Paper Streams  ", info_dot()],
                 [pro_table(show, present_df=False, align_left=("Strategy", "Status", "Note"))],
                 caption="Every paper stream: validated edge, win-rate, profit factor, deploy status, and an "
                         "honest one-line note.",
                 drawer=("Every paper stream: validated edge (c/contract), win-rate, profit factor, deploy "
                         "status, and the honest one-line note. TRADABLE = live paper signal; WATCH = logged "
                         "not trusted; DEPRIORITIZED = real but not bankable. Paper/backtest, never live P&L."),
                 id="strategy-perf-card")


# ---------- Quant Lab page panels ----------
def panel_equity_curve():
    """BACKTEST paper equity curve (cumulative cents/contract) vs flat take-the-mark benchmark. Green area."""
    d = table("equity_curve")
    if d.empty:
        return card([html.H3("Backtest Equity Curve"), empty_state("Fills from the walk-forward backtest.")])
    fig = go.Figure()
    last = float(d["equity_c"].iloc[-1])
    eqcol = GREEN if last >= 0 else RED
    fig.add_scatter(x=d["date"], y=d["equity_c"], mode="lines", name="paper S1 (backtest)",
                    line=dict(color=eqcol, width=2.2, shape="linear"),
                    fill="tozeroy", fillcolor=f"rgba({_rgb(eqcol)},.10)",
                    customdata=d["trades"],
                    hovertemplate="%{x}<br>cumulative %{y:+,.0f}c"
                                  "<br>%{customdata} contract(s) settled that day<extra></extra>")
    # the zero line IS the take-the-mark / no-edge benchmark (taking every quote at the mark = 0 cumulative
    # edge by construction; benchmark_c is flat 0). Drawn as a single labelled baseline, not a redundant trace.
    fig.add_hline(y=0, line=dict(color=NEUTRAL, width=1.2, dash="dash"),
                  annotation_text="take-the-mark (no edge)", annotation_position="bottom right",
                  annotation_font=dict(size=10, color=DIM))
    # annotate the peak and the endpoint (designed, not default-Plotly)
    try:
        import numpy as _np
        eqv = d["equity_c"].astype(float).values
        ipk = int(_np.argmax(eqv))
        fig.add_annotation(x=d["date"].iloc[ipk], y=float(eqv[ipk]), text=f"peak {eqv[ipk]:+.0f}c",
                           showarrow=True, arrowhead=0, arrowcolor=DIM, ax=0, ay=-22,
                           font=dict(size=10, color=DIM))
    except Exception:
        pass
    fig.update_layout(title=None)
    fig.update_yaxes(title="cumulative paper net  (c/contract, 1 ct per signal)", ticksuffix="c",
                     tickformat="+,.0f")
    # range selector buttons on the time axis (designed time-series UX)
    fig.update_xaxes(title="", nticks=8, rangeslider=dict(visible=False),
                     rangeselector=dict(
                         bgcolor="rgba(0,0,0,0)", activecolor="rgba(0,224,138,.18)",
                         bordercolor=GRIDCOL, borderwidth=1, x=0, y=1.08,
                         font=dict(size=10, color=DIM),
                         buttons=[dict(count=1, label="1M", step="month", stepmode="backward"),
                                  dict(count=3, label="3M", step="month", stepmode="backward"),
                                  dict(count=6, label="6M", step="month", stepmode="backward"),
                                  dict(step="all", label="ALL")]))
    d0, d1 = str(d["date"].iloc[0]), str(d["date"].iloc[-1])
    return panel("Backtest Equity Curve — Leak-Free Walk-Forward S1",
                 [graph(_tpl(fig, h=360))],
                 caption=(f"Cumulative paper net from trading one contract of every settled S1 signal over "
                          f"{len(d)} days, ending {last:+,.0f}c (~{last/100:+.2f} dollars). Backtest "
                          f"cents/contract — not the $1k run."),
                 drawer=(f"CUMULATIVE paper net from trading ONE contract of every settled S1 signal, summed in "
                         f"order over {len(d)} settled days ({d0} to {d1}). It ends at {last:+,.0f}c "
                         f"(about {last/100:+.2f} dollars total) — a running SUM across all those contracts, so "
                         f"each new day's settled contracts ADD to it; it is NOT a per-contract average and NOT "
                         f"annualised. The dashed zero line is the no-edge baseline (take every quote at the "
                         f"mark). This is BACKTEST research in cents/contract; the DEPLOYED $1,000 paper run "
                         f"with real Kelly sizing lives on the \"$1k Run\" page (different measure — don't "
                         f"conflate). Cents/contract, never realized P&L."))


def panel_drawdown():
    """Underwater drawdown chart (red) from the backtest equity curve + max-DD stat."""
    d = table("equity_curve")
    if d.empty:
        return card([html.H3("Drawdown"), empty_state("Fills from the walk-forward backtest.")])
    import numpy as _np
    eq = d["equity_c"].astype(float).values
    peak = _np.maximum.accumulate(eq)
    dd = eq - peak                     # underwater in cents/contract (<=0)
    maxdd = float(dd.min())
    fig = go.Figure()
    fig.add_scatter(x=d["date"], y=dd, mode="lines", name="drawdown",
                    line=dict(color=RED, width=1.2), fill="tozeroy",
                    fillcolor="rgba(255,77,94,.16)",
                    hovertemplate="%{x}<br>underwater %{y:+.0f} c/ct<extra></extra>")
    fig.update_layout(title=None)
    fig.update_yaxes(title="underwater (c / contract)", ticksuffix="c", tickformat="+,.0f")
    fig.update_xaxes(title="", nticks=8)
    return panel("Drawdown — Underwater Curve",
                 [graph(_tpl(fig, h=240, legend=False))],
                 caption=f"Peak-to-trough underwater of the backtest equity; max drawdown {maxdd:+.0f} c/ct.",
                 drawer=(f"Peak-to-trough underwater of the backtest equity, in cents/contract. Max backtest "
                         f"drawdown {maxdd:+.0f} c/contract. Drawdowns are part of any real edge — the curve "
                         f"recovers, but losing stretches happen. Backtest, never live."))


def panel_monthly_returns():
    """Monthly paper-return distribution histogram: green positive / red negative bars + stats."""
    d = table("monthly_returns")
    if d.empty:
        return card([html.H3("Monthly Returns"), empty_state("Fills from the walk-forward backtest.")])
    colors = [GREEN if v >= 0 else RED for v in d["net_c"]]
    fig = go.Figure()
    fig.add_bar(x=d["month"], y=d["net_c"], marker_color=colors, width=0.7,
                text=[f"{v:+.0f}" for v in d["net_c"]], textposition="outside", cliponaxis=False,
                hovertemplate="%{x}<br>%{y:+.0f} c/ct · %{customdata} trades<extra></extra>",
                customdata=d["trades"])
    fig.add_hline(y=0, line=dict(color=AXISCOL, width=1))
    fig.update_layout(title=None)
    fig.update_yaxes(title="monthly paper net (c / contract)", ticksuffix="c", tickformat="+,.0f")
    fig.update_xaxes(title="")
    pos = int((d["net_c"] >= 0).sum()); tot = len(d)
    return panel("Monthly Returns Distribution",
                 [graph(_tpl(fig, h=300, legend=False))],
                 caption=f"Paper net summed by calendar month (backtest); {pos} of {tot} months positive.",
                 drawer=(f"Paper net per contract summed by calendar month from the walk-forward backtest. "
                         f"{pos} of {tot} months positive (green). The edge lives in the average — individual "
                         f"months swing, including losers. Backtest, never realized P&L."))


def panel_model_compare():
    """Model-comparison table: EMOS variants RMSE/CRPS/logscore/cov90, deployed row highlighted."""
    d = table("model_compare")
    if d.empty:
        return card([html.H3("Model Comparison"), empty_state("Fills from the EMOS validation run.")])
    show = present(d.drop(columns=["deployed"]),
                   rename={"rmse": "RMSE", "crps": "CRPS", "logscore": "Log-Score", "cov90": "90% Cov"},
                   fmt={"rmse": _degf, "crps": lambda v: "—" if _isnull(v) else f"{v:.3f}",
                        "logscore": lambda v: "—" if _isnull(v) else f"{v:.3f}",
                        "cov90": _pct01},
                   order=["model", "rmse", "crps", "logscore", "cov90"])
    return panel("Model Comparison — EMOS Variants",
                 [pro_table(show, present_df=False, align_left=("Model",))],
                 caption="Leak-free out-of-sample scores by variant; the deployed EMOS-full wins.",
                 drawer=("Leak-free out-of-sample scores by model variant (lower RMSE/CRPS/log-score is better; "
                         "90% coverage should be ≈90%). The deployed EMOS-full wins — which is why it ships. "
                         "Backtest, strictly out-of-sample."),
                 id="model-compare-card")


def panel_scenario():
    """Scenario analysis cards: regime / season conditioning edges (cold/warm, sharp/noisy model)."""
    d = table("scenario")
    if d.empty:
        return card([html.H3("Scenario Analysis"), empty_state("Fills from the regime-conditioning study.")])
    cards = []
    for _, r in d.iterrows():
        e = r["edge_c"]
        col = GREEN if (not _isnull(e) and e >= 0) else RED
        cards.append(html.Div([
            html.Div(r["scenario"], className="u-label"),
            html.Div(f"{e:+.1f}c" if not _isnull(e) else "—", className="sc-val mono",
                     style={"color": col}),
            html.Div(str(r["detail"]), className="sub", style={"fontSize": "11px"})],
            className="card sc-card col-3"))
    return card([html.H3("Scenario / Regime Analysis"),
                 _cap("How the paper edge changes by regime: cold vs warm season (the daily-low edge "
                      "concentrates in winter) and sharp vs noisy model (counterfactual RMSE sweep). These "
                      "are conditioning slices, not promises. Backtest."),
                 html.Div(cards, className="grid12")], id="scenario-card")


def panel_dailylow_edge():
    """Validated daily-LOW S1 edge per city: net + CI error bars colored by tier (green/red/neutral)."""
    d = table("dailylow_edge")
    if d.empty:
        return card([html.H3("Daily-Low S1 Edge"), empty_state("Fills from the daily-low edge backtest.")])
    d = d.copy().sort_values("net_c", ascending=False)
    tier_col = {"TRADABLE": GREEN, "WATCH": AMBER, "DEAD": RED}
    colors = [tier_col.get(str(t).upper(), NEUTRAL) for t in d["tier"]]
    err_plus = (d["ci_hi"] - d["net_c"]).clip(lower=0)
    err_minus = (d["net_c"] - d["ci_lo"]).clip(lower=0)
    fig = go.Figure()
    fig.add_bar(x=d["city"], y=d["net_c"], marker_color=colors, width=0.62,
                customdata=d[["tier", "recent_q_c"]].values,
                hovertemplate="<b>%{x}</b><br>net %{y:+.2f}c · %{customdata[0]}"
                              "<br>recent-Q %{customdata[1]:+.1f}c<extra></extra>",
                error_y=dict(type="data", array=err_plus, arrayminus=err_minus,
                             color=DIM, thickness=1.4, width=4))
    fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
    fig.update_layout(title=None)
    fig.update_yaxes(title="daily-low S1 net (c / contract)", ticksuffix="c", tickformat="+,.0f")
    fig.update_xaxes(title="")
    return panel("Daily-Low S1 Edge — the Orthogonal Overnight Book",
                 [graph(_tpl(fig, h=320, legend=False))],
                 caption="Validated daily-LOW S1 net per city with 95% CIs (green tradable / amber watch) — "
                         "a diversifier roughly orthogonal to the daily high.",
                 drawer=("Validated daily-LOW S1 net per city with 95% bootstrap CIs (green = TRADABLE, amber "
                         "= WATCH). The overnight-low market is roughly orthogonal to the daily high — a real "
                         "diversifier. The edge concentrates in the cold season; recent-quarter is the forward "
                         "decay watch. Paper/backtest, never realized P&L."))


# ============================================================================================
# $1,000 STAGED RUN PAGE (Iris 2026-06-19, user-approved). Surfaced HONESTLY: equity FLAT at $1,000,
# LIVE allocation $0 (all STAGED), the per-edge GATE board (centerpiece), staged Kelly stakes, and the
# OPEN paper signals. Every figure paper-only -- capital moves ONLY on a forward-gate PASS. No live P&L.
# ============================================================================================
# Shared INFO tooltip for the "Paper Net (backtest)" metric (deliverable #7): one definition, used
# everywhere the metric appears. A small (i) glyph with a native title= tooltip (no JS, no deps).
PAPER_NET_TIP = ("Avg net cents/contract a simulated trade would earn after modeled fees + slippage, "
                 "replayed on historical settled outcomes. Paper, not live, not realized P&L.")


def info_dot(tip=PAPER_NET_TIP):
    # WP-03: delegate to the keyboard-focusable, touch-friendly CSS tooltip (tokens.css .info2). Replaces the
    # native title= dot (invisible on touch, 1s hover delay). One helper -> every info dot on the site upgrades.
    return info_tooltip(tip)


def _run_meta(key, default="—"):
    d = table("run_meta")
    if d.empty or "key" not in d:
        return default
    r = d[d["key"] == key]
    return str(r["value"].iloc[0]) if not r.empty else default


def _latest_equity():
    """The CURRENT $1k paper equity (RESET 2026-06-21): the LATEST point of the rebased realized+unrealized
    bankroll_equity_timeline (equity = $1,000 - invested + current value of open positions, marked to public
    quotes). Falls back to the ledger's realized run_meta['equity'] only if the timeline is empty. Returns a
    float. PAPER, unrealized mark, never realized P&L."""
    tl = table("bankroll_equity_timeline")
    if not tl.empty and "equity" in tl.columns:
        t = tl.copy()
        if "ts" in t.columns:
            t["__dt"] = pd.to_datetime(t["ts"], utc=True, format="ISO8601", errors="coerce")
            t = t.dropna(subset=["__dt"]).sort_values("__dt")
        if not t.empty:
            try:
                return float(t["equity"].iloc[-1])
            except (TypeError, ValueError):
                pass
    try:
        return float(str(_run_meta("equity", "1000")).replace(",", ""))
    except (TypeError, ValueError):
        return 1000.0


def _latest_cash_positions():
    """Latest (cash, positions, realized, unrealized) from bankroll_equity_timeline. cash = uninvested bankroll
    ($1,000 + realized - cost of open positions); positions = current MARKET value of open holdings (cost +
    unrealized). cash + positions = equity. Falls back to (equity, 0, ...) if the columns are absent."""
    tl = table("bankroll_equity_timeline")
    eq = _latest_equity()
    if tl.empty:
        return eq, 0.0, 0.0, 0.0
    t = tl.copy()
    if "ts" in t.columns:
        t["__dt"] = pd.to_datetime(t["ts"], utc=True, format="ISO8601", errors="coerce")
        t = t.dropna(subset=["__dt"]).sort_values("__dt")
    if t.empty:
        return eq, 0.0, 0.0, 0.0
    row = t.iloc[-1]

    def _f(col, default=0.0):
        try:
            return float(row[col]) if col in t.columns and pd.notna(row[col]) else default
        except (TypeError, ValueError):
            return default
    cash = _f("cash", eq)
    positions = _f("positions", 0.0)
    return cash, positions, _f("realized"), _f("unrealized")


_GATE_STATUS_STYLE = {
    "DEPLOYED-live":      ("good", GREEN, "DEPLOYED · LIVE"),
    "DEPLOYED-tradable":  ("good", GREEN, "DEPLOYED · TRADABLE"),
    "WATCH-accumulating": ("warn", AMBER, "WATCH · ACCUMULATING"),
    "WATCH-no-path":      ("neut", NEUTRAL, "WATCH · NO PATH"),
    "candidate":          ("neut", NEUTRAL, "CANDIDATE"),
}


def panel_run_header():
    """$1k Run header KPI strip. THREE distinct allocation pools shown side-by-side: ACTIVE-PAPER (deployed
    in the paper run NOW, user-activated ahead of the gate), STAGED (the $0-live Kelly stakes the gate still
    governs), and LIVE REAL-DEPLOY ($0 — no real money). Plus equity (flat $1,000) + gates passing + the
    honest pre-gate-activation callout. HONEST: activation is PAPER, ahead of the forward gate; gates UNCHANGED."""
    # RESET 2026-06-21: the run restarted fresh at $1,000 ("the algorithm changed"; prior track archived).
    reset_date = _run_meta("reset_date", "2026-06-21")
    active_p = _run_meta("active_paper_allocation_dollars", "0.00")
    staged_z = _run_meta("staged_zero_allocation_dollars", _run_meta("staged_total_dollars", "—"))
    live_real = _run_meta("live_real_deploy_allocation_dollars", _run_meta("live_allocation_dollars", "0"))
    npass = _run_meta("n_gates_pass", "0"); ngate = _run_meta("n_gates", "0")
    nactive = _run_meta("n_active_paper_streams", "0")
    kf = _run_meta("kelly_fraction", "0.50")
    activated = _run_meta("activated_streams", "—")
    act_note = _run_meta("activation_note", "—")
    # HEADLINE EQUITY = latest rebased timeline (realized $1,000 baseline + unrealized MTM), NOT the flat
    # ledger 'equity' field. Live = $1,000 - invested + current value of open positions (public-quote mark).
    eq_f = _latest_equity()
    eq = f"{eq_f:,.2f}"
    delta = eq_f - 1000.0
    dd = f"{(min(delta, 0.0) / 1000.0 * 100.0):.1f}"   # paper drawdown from the $1,000 reset baseline
    dd_clause = (f"currently {'+' if delta >= 0 else '−'}${abs(delta):,.2f} ({delta/1000.0*100.0:+.1f}%) "
                 f"vs the $1,000 reset baseline" if abs(delta) >= 0.005
                 else "currently at the $1,000 reset baseline")

    def tile(label, value, sub, accent="var(--ink)"):
        return html.Div([html.Div(label, className="label"),
                         html.Div([html.Span(value, className="val mono", style={"color": accent})]),
                         html.Div(sub, className="sub", style={"fontSize": "10.5px", "marginTop": "2px"})],
                        className="card kpi")
    delta_sub = (f"paper · {'+' if delta >= 0 else '−'}${abs(delta):,.2f} vs $1,000"
                 if abs(delta) >= 0.005 else "paper · at $1,000 baseline")
    cash_f, pos_f, _rz, _ur = _latest_cash_positions()
    cash_pct = (cash_f / eq_f * 100.0) if eq_f else 0.0
    pos_pct = (pos_f / eq_f * 100.0) if eq_f else 0.0
    tiles = [
        tile("PAPER BANKROLL", "$1,000", f"reset baseline {reset_date}", "var(--ink)"),
        tile("CURRENT EQUITY", f"${eq}", delta_sub,
             GREEN if eq_f >= 1000 else RED),
        tile("CASH", f"${cash_f:,.2f}", f"{cash_pct:.0f}% · uninvested bankroll", "var(--ink)"),
        tile("IN POSITIONS", f"${pos_f:,.2f}", f"{pos_pct:.0f}% · market value of open holds", NEUTRAL),
        tile("ACTIVE · PAPER", f"${active_p}", f"{nactive} streams · paper, pre-gate", GREEN),
        tile("STAGED · $0 LIVE", f"${staged_z}", f"Kelly {kf}x · awaits gate", NEUTRAL),
        tile("LIVE REAL-DEPLOY", f"${live_real}", "no real money, no orders", AMBER),
        tile("GATES PASSED", f"{npass}/{ngate}", "real deploy needs a PASS", AMBER),
    ]
    # headline projection stats (read straight from the curated run_projection table so they always match the
    # fan). median +14.63%/m / stress +0.70%/m (~breakeven) for the 7-stream warm book.
    rp = table("run_projection")
    med_mo = stress_mo = None
    if not rp.empty:
        if "mc_median_mo" in rp and rp["mc_median_mo"].notna().any():
            med_mo = float(rp["mc_median_mo"].dropna().iloc[0])
        if "mc_stress_mo" in rp and rp["mc_stress_mo"].notna().any():
            stress_mo = float(rp["mc_stress_mo"].dropna().iloc[0])
    med_str = f"{100*med_mo:+.2f}%/mo" if med_mo is not None else "—"
    stress_str = f"{100*stress_mo:+.2f}%/mo" if stress_mo is not None else "—"
    note = card([html.Div([html.Span("●", style={"color": GREEN, "marginRight": "7px"}),
                           html.B(f"Fresh start: the $1,000 paper run RESET on {reset_date} (the algorithm "
                                  f"changed; the prior track is archived).")],
                          className="sub", style={"fontSize": "12.5px", "color": "var(--ink)"}),
                 html.Div([f"The run rebaselined to $1,000 on {reset_date}; only settlements resolving on or "
                           f"after that date count. {nactive} warm-season-applicable edges ({activated}) are "
                           f"ACTIVE in the PAPER run ({act_note}) — paper allocation ${active_p}. Current paper "
                           f"equity ${eq} = $1,000 baseline − invested + the current value of open positions "
                           f"(unrealized public-quote mark-to-market), {dd_clause}. Paper projection (BACKTEST, "
                           f"not realized): MEDIAN ", html.B(med_str), " vs STRESS ", html.B(stress_str),
                           " (~breakeven — the honest planning number if the underpowered warm edges are ~0). ",
                           html.B("Paper $1,000 only — no real money, no orders, $0 live real-deploy. "),
                           "The forward-validation gates (reconcile_forward_edges.py / FORWARD_PROTOCOL) are "
                           "UNCHANGED and still govern any REAL deployment. Activation does NOT mean these "
                           "edges PASSED their gates — each is still ACCUMULATING settled signals below. "
                           f"Equity rebased to $1,000 on {reset_date} (no backfill) and moves with open-book "
                           "mark-to-market plus post-reset settlements. Never realized P&L."],
                          className="sub", style={"marginTop": "4px"})],
                cls="run-note")
    # plain-language legend defining ACTIVE PAPER vs STAGED (deliverable #2)
    defs = card([html.Div("What the labels mean", className="u-label", style={"marginBottom": "6px"}),
                 html.Div([
                     html.Div([badge("ACTIVE · PAPER", "good"),
                               html.Span(" a user-activated paper stream accumulating forward (simulated) "
                                         "results at a simulated stake — $0 real money, activated PRE-GATE "
                                         "by user decision.", className="sub")],
                              style={"display": "flex", "gap": "8px", "alignItems": "baseline",
                                     "marginBottom": "6px"}),
                     html.Div([badge("STAGED", "neut"),
                               html.Span(" sized but holding $0 simulated stake until its forward-validation "
                                         "gate PASSES.", className="sub")],
                              style={"display": "flex", "gap": "8px", "alignItems": "baseline"})])])
    return html.Div([html.Div(tiles, className="grid kpi-strip"),
                     html.Div([html.Div(note, className="col-8"),
                               html.Div(defs, className="col-4")], className="grid12",
                              style={"marginTop": "10px"})])


# Time-window selector for the $1k paper-equity chart (USER ASK 2026-06-21). value = lookback hours
# (None = All). Intraday windows (12hr/1D) format the x axis as HH:MM; longer windows as dates.
_EQ_WINDOWS = [("12hr", 12), ("1D", 24), ("3D", 72), ("1W", 168), ("1M", 720), ("All", None)]
_EQ_WINDOW_HOURS = {lbl: hrs for lbl, hrs in _EQ_WINDOWS}
_EQ_INTRADAY = {"12hr", "1D"}                  # show HH:MM tick labels; longer windows show dates


_DISPLAY_TZ = "America/New_York"   # all $1k-page time displays are ET (EDT/EST), not UTC (2026-06-24)


def _to_et_naive(ts):
    """A UTC timestamp (tz-aware, or naive assumed-UTC) -> tz-naive America/New_York, so Plotly shows the ET
    wall-clock literally. Handles DST automatically (EDT in summer, EST in winter)."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(_DISPLAY_TZ).tz_localize(None)


def _downsample_list(seq, max_n):
    """PERF (2026-06-22): downsample a list to <= max_n items, KEEPING the first and last (even index
    sampling). A chart can't show more than a few hundred points on screen anyway, and the dense series
    bloat the to_json payload + browser render. Visual shape is preserved."""
    n = len(seq)
    if n <= max_n or max_n < 2:
        return seq
    import numpy as _np
    idx = sorted(set(int(i) for i in _np.linspace(0, n - 1, max_n).round()))
    return [seq[i] for i in idx]


def _downsample_df(df, max_n):
    """Same idea for a DataFrame (keeps first+last rows via even index sampling)."""
    n = len(df)
    if n <= max_n or max_n < 2:
        return df
    import numpy as _np
    idx = sorted(set(int(i) for i in _np.linspace(0, n - 1, max_n).round()))
    return df.iloc[idx]


def _equity_points():
    """Return (points, source) for the $1k paper-equity chart. points = list of dicts with a real datetime x:
    {dt (pandas Timestamp), equity, drawdown}. PREFER the timestamped LIVE bankroll_equity_timeline (realized +
    unrealized mark-to-market, ~10x/day -> a SMOOTH intraday curve); else the realized-only bankroll_marks;
    else the settlement-keyed bankroll_run (equity_curve), spaced one-day apart so long windows still render.
    source tags which fed the chart (shown in the cap)."""
    # ---- preferred: the continuous realized+unrealized MTM timeline (FIX 1, 2026-06-21). Moves with quotes. ----
    tl = table("bankroll_equity_timeline")
    if not tl.empty and len(tl) >= 2 and "ts" in tl.columns:
        # PERF (2026-06-22): VECTORIZED build (was a per-row iterrows over ~27k rows = seconds/render).
        t = tl.copy()
        t["dt"] = pd.to_datetime(t["ts"], utc=True, format="ISO8601", errors="coerce")
        t["equity"] = pd.to_numeric(t["equity"], errors="coerce")
        t = t.dropna(subset=["dt", "equity"]).sort_values("dt")
        has_r = "realized" in t.columns
        has_u = "unrealized" in t.columns
        if has_r:
            t["realized"] = pd.to_numeric(t["realized"], errors="coerce")
        if has_u:
            t["unrealized"] = pd.to_numeric(t["unrealized"], errors="coerce")
        recs = t.to_dict("records")
        pts = [{"dt": r["dt"], "equity": r["equity"], "drawdown": None,
                "realized": (r["realized"] if has_r and not pd.isna(r["realized"]) else None),
                "unrealized": (r["unrealized"] if has_u and not pd.isna(r["unrealized"]) else None)}
               for r in recs]
        if len(pts) >= 2:
            return pts, "timeline"
    marks = table("bankroll_marks")
    if not marks.empty and len(marks) >= 2 and "ts" in marks.columns:
        m = marks.copy()
        m["dt"] = pd.to_datetime(m["ts"], utc=True, format="ISO8601", errors="coerce")
        m = m.dropna(subset=["dt"]).sort_values("dt")
        pts = [{"dt": r["dt"], "equity": float(r["equity"]),
                "drawdown": (None if pd.isna(r.get("drawdown")) else float(r["drawdown"]))}
               for _, r in m.iterrows()]
        if len(pts) >= 2:
            return pts, "marks"
    # ---- fallback: the daily settlement curve (sparse marks / long windows). Synthesize a date x.
    d = table("bankroll_run")
    if not d.empty:
        dd = d.copy()
        dd["dt"] = pd.to_datetime(dd["date"], utc=True, format="ISO8601", errors="coerce")
        # rows can repeat a date (multiple same-day settlements) -> keep order, nudge dupes by index seconds
        dd = dd.reset_index(drop=True)
        out = []
        for i, r in dd.iterrows():
            base = r["dt"]
            if pd.isna(base):
                continue
            out.append({"dt": base + pd.Timedelta(seconds=int(i)), "equity": float(r["bankroll"]),
                        "drawdown": None})
        if out:
            # if there is exactly ONE marks row, splice it on as the latest point so the head is current
            if not marks.empty and "ts" in marks.columns and len(marks) == 1:
                m0 = marks.iloc[0]
                mdt = pd.to_datetime(m0["ts"], utc=True, format="ISO8601", errors="coerce")
                if pd.notna(mdt):
                    out.append({"dt": mdt, "equity": float(m0["equity"]),
                                "drawdown": (None if pd.isna(m0.get("drawdown")) else float(m0["drawdown"]))})
            out.sort(key=lambda r: r["dt"])
            return out, "curve"
    # last resort: a single flat marks row (or nothing) -> one point so the chart still renders
    if not marks.empty and "ts" in marks.columns:
        m0 = marks.iloc[0]
        mdt = pd.to_datetime(m0["ts"], utc=True, format="ISO8601", errors="coerce")
        if pd.notna(mdt):
            return [{"dt": mdt, "equity": float(m0["equity"]),
                     "drawdown": (None if pd.isna(m0.get("drawdown")) else float(m0["drawdown"]))}], "marks"
    return [], "none"


# Kelly what-if overlay (toggle on the $1k equity chart): counterfactual sizings + a forward projection.
_EQ_KELLY_FRACS = [0.25, 0.30, 0.40, 0.50, 0.75, 1.00]
_EQ_KELLY_COLORS = {0.25: "#5AC8FA", 0.30: "#2DD4BF", 0.40: "#9CCC65",
                    0.50: GREEN, 0.75: "#FFB020", 1.00: "#FF5C5C"}


def _equity_figure(window_label, kelly_overlay=False):
    """Build the windowed $1k paper-equity figure + the (raw $ change, % change) readout for `window_label`.
    kelly_overlay=True swaps the single line for a family of counterfactual curves at 0.25-1.0x Kelly (solid =
    realized past, linear Kelly-scaling of the P&L deviation; dashed = forward projection at the run's realized
    growth scaled per Kelly). The 0.50x member == the actual deployed path. Paper / hypothetical what-if.
    Returns (figure, readout_children, readout_color). Time-aware x (HH:MM intraday, dates for long windows).
    Graceful with 1-2 points (renders a single marker only when there is no line to draw). PAPER equity,
    hypothetical."""
    pts, source = _equity_points()
    hours = _EQ_WINDOW_HOURS.get(window_label, None)
    fig = go.Figure()
    if not pts:
        fig.add_scatter(x=[pd.Timestamp.utcnow()], y=[1000], mode="markers",
                        marker=dict(color=GREEN, size=8),
                        hovertemplate="$1,000 (paper)<extra></extra>", showlegend=False)
        fig.add_hline(y=1000, line=dict(color=NEUTRAL, width=1.2, dash="dot"))
        fig.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.2f", range=[900, 1100])
        return _tpl(fig, h=240, legend=False), "—", DIM
    # window the series by the lookback from the LATEST point
    last_dt = pts[-1]["dt"]
    if hours is not None:
        cutoff = last_dt - pd.Timedelta(hours=hours)
        win = [p for p in pts if p["dt"] >= cutoff]
        # keep >=1 anchor point at the window edge even if nothing falls inside (sparse data)
        if len(win) < 2:
            win = pts[-2:] if len(pts) >= 2 else pts[-1:]
    else:
        win = pts
    win = _downsample_list(win, 5000)  # keep the real 5-min live-mark history; only guard pathological payloads
    xs = [_to_et_naive(p["dt"]) for p in win]   # ET wall-clock display (was UTC)
    ys = [p["equity"] for p in win]
    dds = [p["drawdown"] for p in win]
    first_eq, last_eq = ys[0], ys[-1]
    raw = last_eq - first_eq
    pct = (100.0 * raw / first_eq) if first_eq else 0.0
    col = GREEN if raw >= 0 else RED
    # SMOOTH spline for the live realized+unrealized timeline; hv-step for realized-only marks; linear else.
    if source == "timeline":
        line_kw = dict(color=col, width=2.4, shape="linear")
    elif source == "marks":
        line_kw = dict(color=col, width=2.4, shape="hv")
    else:
        line_kw = dict(color=col, width=2.4, shape="linear")
    # hover: precise datetime + equity, + realized/unrealized split when the live timeline feeds it
    if source == "timeline":
        cd = [[("—" if p.get("realized") is None else f"${p['realized']:,.2f}"),
               ("—" if p.get("unrealized") is None else f"${p['unrealized']:,.2f}")] for p in win]
        hovertmpl = ("%{x|%Y-%m-%d %H:%M} ET<br>$%{y:,.2f} paper equity"
                     "<br>realized %{customdata[0]} · unrealized MTM %{customdata[1]}<extra></extra>")
    else:
        cd = [[("—" if dd is None else f"{100.0*dd:.2f}%")] for dd in dds]
        hovertmpl = ("%{x|%Y-%m-%d %H:%M} ET<br>$%{y:,.2f} (paper)"
                     "<br>drawdown %{customdata[0]}<extra></extra>")
    # Keep the equity chart as a clean line. Sparse backfilled windows should not render as dotted markers;
    # hover still exposes every point. Use a marker only for the degenerate one-point case.
    mode = "lines" if len(win) > 1 else "markers"
    overlay_on = bool(kelly_overlay) and source in ("timeline", "marks", "curve") and len(win) >= 2
    if overlay_on:
        # ---- KELLY WHAT-IF FAMILY: counterfactual equity at each sizing (solid past + dashed forward). ----
        kdep = float(_run_meta("kelly_fraction", 0.5) or 0.5) or 0.5
        e_now = ys[-1]
        # forward DRIFT grounded in the run's REALIZED (locked-in) growth, not a model; clamped (5 days of
        # data can't justify a big rate). Kelly scales the per-period return linearly in the small-edge regime.
        realized_now = None
        if source == "timeline":
            for _p in reversed(pts):
                if _p.get("realized") is not None:
                    realized_now = float(_p["realized"]); break
        span_days = max((pts[-1]["dt"] - pts[0]["dt"]).total_seconds() / 86400.0, 0.5)
        base_now = (1000.0 + realized_now) if realized_now is not None else e_now
        g0 = (max(base_now, 1.0) / 1000.0) ** (1.0 / span_days) - 1.0
        g0 = max(min(g0, 0.01), -0.01)                          # +/-1%/day cap
        hist_span = max((last_dt - win[0]["dt"]).total_seconds() / 86400.0, 0.25)
        fwd_span = min(max(hist_span, 0.5), 30.0)               # mirror the visible window, capped at 30d
        NS = 16
        fwd_x = [_to_et_naive(last_dt + pd.Timedelta(days=fwd_span * i / NS)) for i in range(NS + 1)]
        all_y = list(ys)
        for kf in _EQ_KELLY_FRACS:
            ratio = kf / kdep
            ckf = _EQ_KELLY_COLORS.get(kf, NEUTRAL)
            wkf = 3.0 if abs(kf - kdep) < 1e-9 else 1.6
            hist_y = [1000.0 + ratio * (e - 1000.0) for e in ys]          # linear Kelly-scaling of the P&L
            e_k_now = 1000.0 + ratio * (e_now - 1000.0)
            gk = g0 * ratio
            fwd_y = [max(e_k_now * (1.0 + gk) ** (fwd_span * i / NS), 1.0) for i in range(NS + 1)]
            all_y += hist_y + fwd_y
            lbl = f"{kf:.2f}x Kelly" + ("  (deployed)" if abs(kf - kdep) < 1e-9 else "")
            fig.add_scatter(x=xs, y=hist_y, mode="lines", name=lbl, legendgroup=lbl,
                            line=dict(color=ckf, width=wkf, shape="linear"),
                            hovertemplate="%{x|%b %d %H:%M} ET<br>$%{y:,.2f}<br>" + f"{kf:.2f}x Kelly<extra></extra>")
            fig.add_scatter(x=fwd_x, y=fwd_y, mode="lines", name=lbl, legendgroup=lbl, showlegend=False,
                            line=dict(color=ckf, width=wkf, dash="dot"),
                            hovertemplate="%{x|%b %d} ET<br>$%{y:,.2f}<br>" + f"{kf:.2f}x Kelly (projected)<extra></extra>")
        fig.add_vline(x=_to_et_naive(last_dt), line=dict(color=NEUTRAL, width=1, dash="dot"),
                      annotation_text="now", annotation_position="top",
                      annotation_font=dict(color=NEUTRAL, size=9))
        ov_lo, ov_hi = min(all_y), max(all_y)
    else:
        fig.add_scatter(x=xs, y=ys, mode=mode, name="paper equity",
                        line=line_kw, marker=dict(size=6, color=col), customdata=cd,
                        hovertemplate=hovertmpl)
        ov_lo, ov_hi = min(ys), max(ys)
    fig.add_hline(y=1000, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                  annotation_text="$1,000 baseline", annotation_position="bottom right",
                  annotation_font=dict(color=NEUTRAL, size=10))
    pad_span = (ov_hi - ov_lo) or 1.0
    pad = 0.10 if overlay_on else 0.6
    lo = min(ov_lo, 1000) - pad_span * pad
    hi = max(ov_hi, 1000) + pad_span * pad
    fig.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.2f", range=[lo, hi])
    # time-aware x: HH:MM for intraday windows, dates otherwise (proper datetime axis -> Plotly auto-formats).
    # With the Kelly overlay the forward projection spans days, so force a date format even on intraday windows.
    if window_label in _EQ_INTRADAY and not overlay_on:
        fig.update_xaxes(title=None, tickformat="%H:%M", type="date")
    else:
        fig.update_xaxes(title=None, tickformat="%b %d", type="date")
    readout = html.Span([
        html.Span(f"{'+' if raw >= 0 else '−'}${abs(raw):,.2f}", className="mono",
                  style={"fontWeight": "800"}),
        html.Span(f"  ·  {pct:+.2f}%", className="mono"),
        html.Span(f"  ({window_label})", className="sub", style={"marginLeft": "4px"})],
        style={"color": col, "fontSize": "15px"})
    return _tpl(fig, h=240, legend=overlay_on), readout, col


def panel_run_equity():
    """The $1,000 PAPER equity chart with a time-window selector (12hr / 1D / 3D / 1W / 1M / All). Prefers the
    timestamped bankroll_marks series; falls back to the daily equity_curve for long windows / sparse marks.
    Shows the raw $ + % change over the selected window. PAPER only, never realized P&L."""
    reset_date = _run_meta("reset_date", "2026-06-21")
    pts, source = _equity_points()
    cur_val = pts[-1]["equity"] if pts else _latest_equity()
    cur_str = f"${cur_val:,.2f}"
    delta = (cur_val - 1000.0) if cur_val is not None else 0.0
    dd = f"{min(delta, 0.0) / 1000.0 * 100.0:.1f}"
    # post-reset the meaningful default window is intraday (the timeline starts at the reset, ~today)
    default_win = "1D"
    fig, readout, _col = _equity_figure(default_win)
    src_note = ("LIVE realized + unrealized mark-to-market timeline (moves with public quotes ~10x/day)"
                if source == "timeline"
                else ("timestamped realized paper-equity marks" if source == "marks"
                      else ("daily settlement curve (timestamped marks still sparse — falling back)"
                            if source == "curve" else "no equity data yet")))
    selector = dcc.RadioItems(
        id="run-equity-window",
        options=[{"label": " " + lbl, "value": lbl} for lbl, _ in _EQ_WINDOWS],
        value=default_win, className="sb-radio",
        labelStyle={"display": "inline-block", "marginRight": "12px", "fontSize": "12px"},
        style={"marginBottom": "6px"})
    kelly_toggle = dcc.Checklist(
        id="run-equity-kelly",
        options=[{"label": "  Kelly what-if overlay (0.25–1.0× sizing + forward projection)", "value": "on"}],
        value=[], className="sb-radio",
        labelStyle={"display": "inline-block", "fontSize": "12px"},
        style={"marginBottom": "6px"})
    return panel(f"Paper Equity — {cur_str}",
                 [selector, kelly_toggle,
                  html.Div(readout, id="run-equity-readout", style={"marginBottom": "4px"}),
                  dcc.Graph(id="run-equity-graph", figure=fig, config={"displayModeBar": False})],
                 caption=(f"$1,000 paper bankroll (reset {reset_date}), marked continuously to public quotes — "
                          f"currently {cur_str} ({'+' if delta >= 0 else '−'}${abs(delta):,.2f} vs baseline; "
                          f"max drawdown {dd}%). Pick a window; toggle the Kelly what-if. $0 real."),
                 drawer=(f"The $1,000 PAPER bankroll, RESET to $1,000 on {reset_date} (the algorithm changed; the "
                         f"prior track is archived). PAPER equity = $1,000 reset baseline − invested + the current "
                         f"value of open positions (unrealized public-quote mark-to-market) — so the curve moves "
                         f"CONTINUOUSLY with quotes from a $1,000 start, not just at settlements. Current paper "
                         f"equity {cur_str} ({'+' if delta >= 0 else '−'}${abs(delta):,.2f} vs the $1,000 "
                         f"baseline; max paper drawdown {dd}%). Pick a time window below; the readout shows the $ "
                         f"and % change over it. Source: {src_note}. Toggle the Kelly what-if overlay to see the "
                         f"SAME realized path re-sized at 0.25–1.0× Kelly (solid = past, linear sizing-scaled; "
                         f"dashed = a forward projection at the run's realized growth, scaled per sizing; 0.50× = "
                         f"the deployed path). LIVE real-deploy capital = $0 — promotion to REAL still requires a "
                         f"forward-gate PASS. Paper / hypothetical what-if, never realized P&L."))


def panel_equity_composition():
    """CASH vs IN-POSITIONS split of the $1k paper net equity (USER ASK 2026-06-24), with the realized/
    unrealized breakdown. Cash = uninvested bankroll = $1,000 + realized - cost of open positions; positions =
    current MARKET value of open holds = cost + unrealized. cash + positions = net equity. PAPER, no real money."""
    cash, positions, realized, unreal = _latest_cash_positions()
    eq = cash + positions
    if eq <= 0:
        return card([html.H3("Cash vs Positions"), _cap("No equity data yet.")])
    fig = go.Figure(go.Pie(
        labels=["Cash", "In positions"], values=[max(cash, 0.0), max(positions, 0.0)],
        hole=0.64, sort=False, direction="clockwise",
        marker=dict(colors=[GREEN_DK, NEUTRAL], line=dict(color=PANEL, width=2)),
        textinfo="percent", textfont=dict(size=12, color="#0b0f12"),
        hovertemplate="%{label}<br>$%{value:,.2f} · %{percent}<extra></extra>"))
    fig.add_annotation(text=f"<b>${eq:,.0f}</b><br><span style='font-size:10px'>net equity</span>",
                       showarrow=False, font=dict(size=17, color=INK))
    fig = _tpl(fig, h=230, legend=False)
    fig.update_layout(margin=dict(l=8, r=8, t=8, b=8))

    def _row(label, amount, color, note):
        return html.Div([
            html.Span("●  ", style={"color": color, "fontSize": "14px"}),
            html.Span(label, style={"fontWeight": "700", "minWidth": "104px", "display": "inline-block"}),
            html.Span(f"{'+' if amount >= 0 else '−'}${abs(amount):,.2f}", className="mono",
                      style={"color": color, "fontWeight": "800", "marginRight": "8px"}),
            html.Span(note, className="sub")],
            style={"marginBottom": "7px", "fontSize": "13px"})

    cash_pct = cash / eq * 100.0
    pos_pct = positions / eq * 100.0
    rz_col = GREEN if realized >= 0 else RED
    ur_col = GREEN if unreal >= 0 else RED
    legend = html.Div([
        _row("Cash", cash, GREEN_DK, f"{cash_pct:.0f}% · uninvested bankroll (not in any trade)"),
        _row("In positions", positions, NEUTRAL, f"{pos_pct:.0f}% · live market value of open holds"),
        html.Div(style={"height": "1px", "background": "var(--grid)", "margin": "8px 0"}),
        _row("Realized", realized, rz_col, "locked in on SETTLED markets — final, cannot change"),
        _row("Unrealized", unreal, ur_col, "paper mark on OPEN holds — moves with quotes until settled")],
        style={"flex": "1", "minWidth": "260px", "paddingLeft": "8px"})
    body = html.Div([html.Div(graph(fig), style={"flex": "0 0 250px", "minWidth": "220px"}), legend],
                    style={"display": "flex", "alignItems": "center", "gap": "16px", "flexWrap": "wrap"})
    return panel("Cash vs Positions",
                 [body],
                 caption="How the $1,000 paper equity splits into cash vs open-position market value "
                         "(realized locked-in, unrealized still moving with quotes).",
                 drawer=("How the $1,000 paper net equity splits. CASH = uninvested bankroll ($1,000 + realized "
                         "− cost of open positions). IN POSITIONS = current market value of what we hold (cost + "
                         "unrealized). The two always sum to net equity. REALIZED P&L is locked in once a market "
                         "SETTLES; UNREALIZED is the still-moving paper mark on positions we still hold (not "
                         "real until they settle). Paper only — never real money."))


# ---- TASK B (2026-06-21): value-vs-paid intraday curve per RESOLUTION DATE, with a current/next toggle ----
def _today_et():
    """Today's calendar date (YYYY-MM-DD) in the $1k page's display timezone (ET)."""
    return pd.Timestamp.now(tz=_DISPLAY_TZ).date().isoformat()


def _resolution_dates_all():
    """ALL distinct resolution_date values in resolution_day_curve, sorted ascending (incl settled history)."""
    d = table("resolution_day_curve")
    if d.empty or "resolution_date" not in d.columns:
        return []
    return sorted(str(v) for v in d["resolution_date"].dropna().unique())


def _resolution_dates():
    """ACTIVE (open) resolution dates only = today + any future/pending day. Settled PAST days are EXCLUDED
    here (they moved behind the 'past days' dropdown) -- the table now holds full 6/21+ history, so the old
    'all dates, first = today' assumption showed 6/21 as TODAY and never dropped settled days. Ascending."""
    today = _today_et()
    return [d for d in _resolution_dates_all() if d >= today]


def _resolution_dates_past():
    """SETTLED resolution dates (before today), most-recent first -- shown on demand via the dropdown."""
    today = _today_et()
    return sorted((d for d in _resolution_dates_all() if d < today), reverse=True)


_RESDAY_MEMO: dict = {}   # WP-08: (resolution_date, 20s-bucket) -> summary; collapses the ~10 repeat calls
                          # per $1k render (net-per-day block + book summary + sections) to one filter/sort.


def _resday_summary(resolution_date):
    """Latest-ts totals for one resolution date from resolution_day_curve: dict(paid$, value$, net$, pct,
    n_pos, n_ct) or None. The last ts row holds the current cumulative paid / value / contracts."""
    _bucket = int(time.time() // 20)   # <=20s staleness on a paper mark -> fine; bounds repeated work
    _key = (str(resolution_date), _bucket)
    if _key in _RESDAY_MEMO:
        return _RESDAY_MEMO[_key]
    if len(_RESDAY_MEMO) > 256:        # keep the memo from growing unbounded across buckets
        _RESDAY_MEMO.clear()
    val = _resday_summary_uncached(resolution_date)
    _RESDAY_MEMO[_key] = val
    return val


def _resday_summary_uncached(resolution_date):
    d = table("resolution_day_curve")
    if d.empty or "resolution_date" not in d.columns or "ts" not in d.columns:
        return None
    sel = d[d["resolution_date"].astype(str) == str(resolution_date)].copy()
    if sel.empty:
        return None
    # Sort by PARSED datetime (ISO8601 handles the mixed micros/no-micros ts formats) so this headline picks the
    # SAME latest row the chart plots. A plain string sort kept a micros-format row the chart was DROPPING as NaT
    # (pre-fix), so the headline "Current value" could read a different (newer) point than the chart's last point
    # -- the 44.36-vs-41.79 discrepancy (2026-06-26).
    sel["__dt"] = pd.to_datetime(sel["ts"], utc=True, format="ISO8601", errors="coerce")
    last = sel.sort_values("__dt").iloc[-1]
    paid = float(last.get("cumulative_paid_c") or 0.0) / 100.0
    value = float(last.get("cumulative_value_c") or 0.0) / 100.0
    npos = int(last.get("n_entered") or 0)
    nct = (float(last.get("n_contracts"))
           if ("n_contracts" in sel.columns and pd.notna(last.get("n_contracts"))) else None)
    net = value - paid
    pct = (net / paid * 100.0) if paid > 0 else None
    return {"paid": paid, "value": value, "net": net, "pct": pct, "n_pos": npos, "n_ct": nct}


def _resolution_day_figure(resolution_date):
    """Two-line chart over ts for one resolution_date: cumulative VALUE (prominent) vs cumulative PAID (cost
    basis, reference), with a green area where value>paid and red where value<paid. Cents -> dollars. PAPER:
    value = public-quote mark (or settled payout once resolved), not realized P&L."""
    d = table("resolution_day_curve")
    fig = go.Figure()
    if d.empty or resolution_date is None or "resolution_date" not in d.columns:
        return _tpl(fig, h=300, legend=False)
    sel = d[d["resolution_date"].astype(str) == str(resolution_date)].copy()
    if sel.empty or "ts" not in sel.columns:
        return _tpl(fig, h=300, legend=False)
    sel["dt"] = pd.to_datetime(sel["ts"], utc=True, format="ISO8601", errors="coerce")
    sel = sel.dropna(subset=["dt"]).sort_values("dt")
    if sel.empty:
        return _tpl(fig, h=300, legend=False)
    sel["dt"] = sel["dt"].dt.tz_convert(_DISPLAY_TZ).dt.tz_localize(None)   # ET wall-clock display (was UTC)
    sel = _downsample_df(sel, 5000)    # keep the real 5-min live-mark history; only guard pathological payloads
    xs = list(sel["dt"])
    paid = [float(v) / 100.0 for v in sel["cumulative_paid_c"]]      # cents -> dollars
    value = [float(v) / 100.0 for v in sel["cumulative_value_c"]]
    nent = [int(v) if pd.notna(v) else 0 for v in sel.get("n_entered", [0] * len(sel))]
    # PAID reference line first (so VALUE draws on top). The shaded area between them is built by drawing the
    # PAID line, then VALUE with fill='tonexty'. Plotly fills with one color, so split into green/red segments
    # by drawing two overlaid value traces masked to value>=paid (green) and value<paid (red).
    fig.add_scatter(x=xs, y=paid, name="cumulative paid (cost basis)", mode="lines",
                    line=dict(color=NEUTRAL, width=1.8, dash="dot"),
                    hoverinfo="skip", showlegend=True)
    # green fill where value >= paid
    green_y = [v if v >= p else p for v, p in zip(value, paid)]
    fig.add_scatter(x=xs, y=green_y, mode="lines", line=dict(width=0), showlegend=False,
                    hoverinfo="skip", fill="tonexty",
                    fillcolor="rgba(0,224,138,0.18)")
    # red fill where value < paid (re-draw paid as the base, then value clipped to the under-water region)
    fig.add_scatter(x=xs, y=paid, mode="lines", line=dict(width=0), showlegend=False, hoverinfo="skip")
    red_y = [v if v < p else p for v, p in zip(value, paid)]
    fig.add_scatter(x=xs, y=red_y, mode="lines", line=dict(width=0), showlegend=False,
                    hoverinfo="skip", fill="tonexty", fillcolor="rgba(255,77,94,0.18)")
    # the prominent VALUE line on top, color = green if currently above paid else red
    up = value[-1] >= paid[-1]
    cd = [[f"${p:,.2f}", f"${v:,.2f}", f"{'+' if (v - p) >= 0 else '−'}${abs(v - p):,.2f}", n]
          for p, v, n in zip(paid, value, nent)]
    fig.add_scatter(x=xs, y=value, name="cumulative value (mark/payout)", mode="lines",
                    line=dict(color=(GREEN if up else RED), width=2.6), customdata=cd,
                    hovertemplate=("%{x|%Y-%m-%d %H:%M} ET<br>value %{customdata[1]} · paid %{customdata[0]}"
                                   "<br>delta %{customdata[2]} · entered %{customdata[3]}<extra></extra>"))
    allv = paid + value
    span = (max(allv) - min(allv)) or 1.0
    fig.update_yaxes(title="cumulative $ (paper)", tickprefix="$", tickformat=",.2f",
                     range=[min(allv) - span * 0.15, max(allv) + span * 0.25])
    fig.update_xaxes(title=None, tickformat="%b %d %H:%M", type="date")
    return _tpl(fig, h=300, legend=True)


def _resday_metric(lbl, val, color="var(--ink)", big=False):
    return html.Div([html.Div(lbl, className="u-label", style={"fontSize": "10px"}),
                     html.Div(val, className="mono",
                              style={"fontSize": "22px" if big else "19px", "fontWeight": "800",
                                     "color": color})],
                    style={"flex": "1", "minWidth": "100px" if big else "94px"})


def _resday_metric_row(s, big=False):
    """The paid / current-value / net+% / positions / contracts metric row for a summary dict s."""
    net_col = GREEN if s["net"] >= 0 else RED
    pct_s = "" if s["pct"] is None else f" ({s['pct']:+.1f}%)"
    ct_s = "—" if s.get("n_ct") is None else f"{s['n_ct']:,.0f}"
    return html.Div([
        _resday_metric("Paid (cost basis)", f"${s['paid']:,.2f}", big=big),
        _resday_metric("Current value", f"${s['value']:,.2f}", net_col, big=big),
        _resday_metric("Net (paper)", f"{'+' if s['net'] >= 0 else '−'}${abs(s['net']):,.2f}{pct_s}",
                       net_col, big=big),
        _resday_metric("Positions", f"{s['n_pos']}", big=big),
        _resday_metric("Contracts", ct_s, big=big)],
        style={"display": "flex", "flexWrap": "wrap", "gap": "10px", "margin": "4px 0 10px"})


def _resday_book_summary(dates):
    """Book-level totals across ALL open resolution dates (sum of each day's paid/value/positions/contracts)."""
    tot = {"paid": 0.0, "value": 0.0, "n_pos": 0, "n_ct": 0.0, "any": False}
    for dt in dates:
        s = _resday_summary(dt)
        if not s:
            continue
        tot["any"] = True
        tot["paid"] += s["paid"]; tot["value"] += s["value"]; tot["n_pos"] += s["n_pos"]
        tot["n_ct"] += (s["n_ct"] or 0.0)
    if not tot["any"]:
        return None
    tot["net"] = tot["value"] - tot["paid"]
    tot["pct"] = (tot["net"] / tot["paid"] * 100.0) if tot["paid"] > 0 else None
    return tot


def _resday_section(resolution_date, idx=0):
    """One FULL section per resolution date: header + day tag (by ACTUAL date, not list position), a summary
    metric row (paid / current value / net / positions / contracts) and the full cumulative value-vs-paid chart."""
    s = _resday_summary(resolution_date)
    today = _today_et()
    nextday = (pd.Timestamp(today) + pd.Timedelta(days=1)).date().isoformat()
    tag = ("TODAY" if resolution_date == today
           else ("NEXT DAY" if resolution_date == nextday
                 else ("SETTLED" if resolution_date < today else "")))
    metrics = _resday_metric_row(s) if s else html.Div()
    hdr = html.Div([html.H3(f"Resolving {resolution_date}",
                            style={"display": "inline-block", "margin": "0 8px 0 0"}),
                    (badge(tag, "good" if tag == "TODAY" else "neut") if tag else html.Span())],
                   style={"display": "flex", "alignItems": "baseline", "marginBottom": "2px"})
    return card([hdr, metrics, graph(_resolution_day_figure(resolution_date))],
                style={"marginBottom": "10px"})


def panel_resolution_day_curve():
    """Per-resolution-day FULL SECTIONS (USER ASK 2026-06-22; was a single current/next toggle): a BOOK-TOTAL
    header (all open resolution days) + for EACH open resolution date a header + summary (paid / current value /
    net / positions / contracts) + the full cumulative value-vs-paid chart over [day-before 00:01 ->
    resolution-day 23:59]. PAPER: value = public-quote mark (or settled payout once resolved), not realized P&L."""
    dates = _resolution_dates()           # OPEN days only (today + next); settled days -> the dropdown below
    past = _resolution_dates_past()
    if not dates and not past:
        return card([html.H3("Positions Resolving — Cumulative Value vs Paid"),
                     empty_state("Fills from the resolution_day_curve table once positions are entered.")])
    intro = _cap("A BOOK TOTAL across the OPEN resolution days, then one section PER open resolution date. The "
                 "prominent line = cumulative VALUE of the positions resolving that day (fluctuates with public "
                 "quotes, or the settled payout once resolved); the dotted line = cumulative PRICE PAID (cost "
                 "basis, steps up as positions are entered). Band is GREEN when value > paid, RED when under. "
                 "Each spans 12:01 AM the day before through 11:59 PM the resolution day (ET). Settled past days "
                 "move to the dropdown at the bottom. NOTE: each day's net here is the LIFETIME value − paid on "
                 "just the positions resolving THAT date — a different measure from the whole-book Daily Equity "
                 "Change calendar above (that day's total-equity swing), so the two 'today' figures differ by "
                 "construction. PAPER / UNREALIZED — $0 real, no orders.")
    bs = _resday_book_summary(dates)
    book = []
    if bs:
        book = [card([html.Div([html.H3("Open resolution days — book total",
                                        style={"display": "inline-block", "margin": "0 8px 0 0"}),
                                badge(f"{len(dates)} open", "neut")],
                               style={"display": "flex", "alignItems": "baseline", "marginBottom": "2px"}),
                      _resday_metric_row(bs, big=True)],
                     style={"marginBottom": "12px",
                            "borderColor": "color-mix(in srgb, var(--mint) 35%, transparent)"})]
    sections = [_resday_section(dt) for dt in dates]
    if not sections:
        sections = [_cap("No open resolution days right now — next-day positions appear after the day-ahead scan.")]
    # SETTLED past days -> a dropdown (on-demand chart) so they don't clutter the live view but stay accessible.
    past_block = []
    if past:
        past_block = [card([
            html.Div([html.H3("Past (settled) resolution days",
                              style={"display": "inline-block", "margin": "0 8px 0 0"}),
                      badge(f"{len(past)} settled", "neut")],
                     style={"display": "flex", "alignItems": "baseline", "marginBottom": "4px"}),
            _cap("Already-resolved days. Pick one to view its final cumulative value-vs-paid chart."),
            dcc.Dropdown(id="resday-past-select",
                         options=[{"label": f"Resolved {d}", "value": d} for d in past],
                         placeholder="Select a past resolution day…",
                         maxHeight=340,
                         className="sb-dropdown", style={"maxWidth": "340px", "marginBottom": "8px"}),
            html.Div(id="resday-past-section")],
            # overflow VISIBLE so the open dropdown menu isn't clipped by the card's overflow:hidden
            # (that clipping was why past days couldn't be selected); z-index keeps it above sibling cards.
            style={"marginBottom": "10px", "overflow": "visible", "position": "relative", "zIndex": 20})]
    return html.Div([section("Positions Resolving — Value vs Paid (per day)"), intro]
                    + book + sections + past_block)


def panel_daily_pnl():
    """DAILY paper P&L calendar (USER ASK 2026-07-01): ONE SQUARE PER CALENDAR DAY, colored by that day's paper
    P&L = the CHANGE in $1k equity over the day (end-of-day equity minus the prior day's) — GREEN = up day, RED
    = down day — in the same weekday×week calendar format as the settlement-surprise chart. Sourced from the
    authoritative bankroll_equity_timeline (NOT the resolution marks, which don't reflect true settlements), so
    the squares SUM EXACTLY to equity − $1,000 = the run's total P&L. PAPER, no real money."""
    et = table("bankroll_equity_timeline")
    if et.empty or "ts" not in et.columns or "equity" not in et.columns:
        return card([html.H3("Daily Equity Change — Whole-Book Calendar"),
                     empty_state("Fills as the $1k equity series accumulates.")])
    import pandas as _pd
    e = et.copy()
    e["dt"] = _pd.to_datetime(e["ts"], utc=True, format="ISO8601", errors="coerce")
    e["equity"] = _pd.to_numeric(e["equity"], errors="coerce")
    e = e.dropna(subset=["dt", "equity"]).sort_values("dt")
    if e.empty:
        return card([html.H3("Daily Equity Change — Whole-Book Calendar"), empty_state("No equity data yet.")])
    e["date"] = e["dt"].dt.tz_convert(_DISPLAY_TZ).dt.strftime("%Y-%m-%d")   # ET calendar day
    daily = e.groupby("date", sort=True)["equity"].last().reset_index()
    start = float(_run_meta("bankroll_start", 1000.0) or 1000.0)
    rows, prev = [], start
    for _, r in daily.iterrows():
        rows.append({"date": r["date"], "pnl": float(r["equity"]) - prev, "eq": float(r["equity"])})
        prev = float(r["equity"])
    import datetime as _dtmod
    # date -> (pnl, eq) for days that actually HAVE equity data (the only ones we color)
    data_by_date = {r["date"]: (float(r["pnl"]), float(r["eq"])) for r in rows}
    def _d(s):
        return _dtmod.date.fromisoformat(s)
    first = min(_d(k) for k in data_by_date)
    last_data = max(_d(k) for k in data_by_date)
    today_et = _pd.Timestamp.now(tz=_DISPLAY_TZ).date()
    # show ~a week of upcoming (no-data) days in gray; snap the grid to whole Mon..Sun weeks
    horizon = max(today_et, last_data) + _dtmod.timedelta(days=7)
    grid_start = first - _dtmod.timedelta(days=first.weekday())          # back to Monday
    grid_end = horizon + _dtmod.timedelta(days=(6 - horizon.weekday()))  # forward to Sunday
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    week_starts, wcur = [], grid_start
    while wcur <= grid_end:
        week_starts.append(wcur)
        wcur += _dtmod.timedelta(days=7)
    weeks = [f"{w.strftime('%b')} {w.day}" for w in week_starts]         # column = week-of Monday
    nweeks = len(week_starts)
    z = [[None] * nweeks for _ in range(7)]        # colored P&L, data days only
    cd = [[None] * nweeks for _ in range(7)]
    future = [[None] * nweeks for _ in range(7)]   # gray squares for upcoming no-data days
    for wj, ws in enumerate(week_starts):
        for di in range(7):
            day = ws + _dtmod.timedelta(days=di)
            key = day.isoformat()
            if key in data_by_date:
                pnl, eq = data_by_date[key]
                z[di][wj] = pnl
                cd[di][wj] = [key, f"{'+' if pnl >= 0 else '−'}${abs(pnl):,.2f}", f"${eq:,.2f}"]
            elif day > today_et:                   # future -> gray "no data yet"
                future[di][wj] = 1
                cd[di][wj] = [key, "no data yet", "upcoming day"]
            # else: past day with no equity data -> left transparent (only show days WITH data)
    zmax = max((abs(p) for p, _ in data_by_date.values()), default=1.0) or 1.0
    fig = go.Figure()
    # FUTURE gray layer (upcoming days, no data yet)
    fig.add_trace(go.Heatmap(z=future, x=weeks, y=dows, xgap=3, ygap=3, showscale=False,
                             colorscale=[[0, NEUTRAL_DK], [1, NEUTRAL_DK]], customdata=cd,
                             hovertemplate="%{customdata[0]}<br>%{customdata[1]}<extra></extra>"))
    # DATA layer: diverging RED (down) -> AMBER (flat / mid) -> GREEN (up), symmetric about 0
    fig.add_trace(go.Heatmap(z=z, x=weeks, y=dows, customdata=cd, xgap=3, ygap=3,
                             colorscale=[[0.0, RED], [0.5, AMBER], [1.0, GREEN]],
                             zmin=-zmax, zmax=zmax, zmid=0,
                             colorbar=dict(title="$ / day", thickness=10, len=0.8,
                                           tickfont=dict(size=10, color=DIM)),
                             hovertemplate="%{customdata[0]}<br>day P&L %{customdata[1]} · "
                                           "end equity %{customdata[2]}<extra></extra>"))
    fig.update_layout(title=None)
    fig.update_xaxes(title="", showticklabels=True, side="top", tickangle=0,
                     tickfont=dict(size=9, color=DIM))
    fig.update_yaxes(title="", autorange="reversed")
    # SQUARE tiles: lock 1:1 data aspect so every cell is a square regardless of week count
    fig.update_yaxes(scaleanchor="x", scaleratio=1, constrain="domain")
    fig.update_xaxes(constrain="domain")
    vals = list(data_by_date.values())
    tot = float(sum(p for p, _ in vals)); nd = len(vals)
    wins = sum(1 for p, _ in vals if p > 0); losses = sum(1 for p, _ in vals if p < 0)
    return card([html.H3("Daily Equity Change — Whole-Book Calendar"),
                 _cap(f"One SQUARE per calendar day, colored by that day's change in TOTAL $1,000 equity "
                      f"(GREEN = up, AMBER = flat, RED = down); upcoming days show GRAY. This is the "
                      f"WHOLE-BOOK daily swing — every open position marked to quotes, plus anything that "
                      f"settled that day — so it SUMS EXACTLY to equity − $1,000. It is a DIFFERENT measure "
                      f"from the per-resolution-day 'value vs paid' below (which is one settlement date's "
                      f"LIFETIME net on just the positions resolving that date), so the two 'today' numbers "
                      f"are not expected to match. {nd} days · {wins} up / {losses} down · cumulative "
                      f"{'+' if tot >= 0 else '−'}${abs(tot):,.2f} (= equity − $1,000). PAPER — $0 real, "
                      f"no orders."),
                 graph(_tpl(fig, h=240, legend=False))])


def panel_run_projection():
    """MONTE-CARLO equity FAN for the $1,000 paper run under the ACTIVATED 4-edge book: P95 (top, green-ish) /
    MEDIAN (green) / P5 (bottom, red-ish) simulated 12-month paper-equity paths, with a 'YOU ARE HERE' marker
    at month 0 = $1,000. Source: run_projection (curated, seeded MC). PAPER projection, NOT realized P&L."""
    d = table("run_projection")
    if d.empty:
        return card([html.H3("Projected Paper-Equity Fan — 12-Month Monte-Carlo"),
                     empty_state("Fills from the activated-book Kelly projection.")])
    d = d.sort_values("month")
    months = list(d["month"]); p5 = list(d["p5"]); med = list(d["median"]); p95 = list(d["p95"])
    start = float(d["start_equity"].iloc[0])
    med_mo = float(d["mc_median_mo"].iloc[0]); p5_mo = float(d["mc_p5_mo"].iloc[0])
    p95_mo = float(d["mc_p95_mo"].iloc[0])
    # STRESS leg (deliverable #1): a deterministic line compounding the stress %/m (every edge at its CI
    # lower bound -> the underpowered warm edges ~0). Present only if the producer emitted it.
    has_stress = "stress" in d.columns and d["stress"].notna().any()
    stress = [float(v) if v == v and v is not None else None for v in d["stress"]] if has_stress else None
    stress_mo = (float(d["mc_stress_mo"].iloc[0]) if "mc_stress_mo" in d.columns
                 and d["mc_stress_mo"].notna().any() else None)
    fig = go.Figure()
    # shaded NEUTRAL band between P5 and P95
    fig.add_scatter(x=months + months[::-1], y=p95 + p5[::-1], fill="toself",
                    fillcolor="rgba(174,184,192,.12)", line=dict(width=0), mode="lines",
                    name="P5–P95 band", hoverinfo="skip", showlegend=False)
    # P95 run (green-ish)
    fig.add_scatter(x=months, y=p95, mode="lines", name="P95 path",
                    line=dict(color=GREEN_DK, width=1.6, dash="dot"),
                    hovertemplate="month %{x}<br>P95 $%{y:,.0f} (paper)<extra></extra>")
    # MEDIAN run (bright green)
    fig.add_scatter(x=months, y=med, mode="lines", name="Median path",
                    line=dict(color=GREEN, width=2.6, shape="spline", smoothing=0.4),
                    hovertemplate="month %{x}<br>median $%{y:,.0f} (paper)<extra></extra>")
    # P5 run (red-ish)
    fig.add_scatter(x=months, y=p5, mode="lines", name="P5 path",
                    line=dict(color=RED, width=1.6, dash="dot"),
                    hovertemplate="month %{x}<br>P5 $%{y:,.0f} (paper)<extra></extra>")
    # STRESS trajectory (deliverable #1): a DASHED WARNING line ON TOP of the variance fan. Compounds the
    # stress %/m (~+0.70%/m, ~breakeven) -> shows the downside if the underpowered warm edges are actually ~0.
    # This is EDGE uncertainty, NOT the monthly-variance fan -> drawn distinctly in amber/dashed.
    if stress is not None:
        fig.add_scatter(x=months, y=stress, mode="lines",
                        name="Stress (warm edges → ~breakeven)",
                        line=dict(color=AMBER, width=2.0, dash="dash"),
                        hovertemplate="month %{x}<br>stress $%{y:,.0f} (paper, warm edges ~0)<extra></extra>")
    # baseline + YOU ARE HERE marker at month 0
    fig.add_hline(y=start, line=dict(color=NEUTRAL, width=1, dash="dash"))
    fig.add_scatter(x=[0], y=[start], mode="markers+text", name="you are here",
                    marker=dict(size=12, color=INK, symbol="circle",
                                line=dict(width=2, color=GREEN)),
                    text=["  YOU ARE HERE"], textposition="middle right",
                    textfont=dict(color=INK, size=11),
                    hovertemplate=f"month 0 · ${start:,.0f} (paper, flat so far)<extra></extra>",
                    showlegend=False)
    fig.update_layout(title=None)
    fig.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.0f")
    fig.update_xaxes(title="forward month", nticks=13)
    end_med = med[-1]; end_p5 = p5[-1]; end_p95 = p95[-1]
    end_stress = stress[-1] if stress is not None else None
    stress_txt = ""
    if stress_mo is not None and end_stress is not None:
        stress_txt = (f" The dashed amber STRESS line compounds {100*stress_mo:+.1f}%/mo — the honest "
                      f"downside if the underpowered warm edges turn out to be ~0 — reaching only "
                      f"${end_stress:,.0f} after 12 paper months.")
    return panel("Projected Paper-Equity Fan — 12-Month Monte-Carlo",
                 [graph(_tpl(fig, h=320))],
                 caption=(f"Paper projection (model estimate, NOT realized): MC of the activated book at 0.50x "
                          f"Kelly. Median {100*med_mo:+.1f}%/mo -> ${end_med:,.0f} after 12 months (P5 "
                          f"${end_p5:,.0f} / P95 ${end_p95:,.0f}). $0 real."),
                 drawer=("Paper projection (model estimate, NOT realized). Monte-Carlo of the activated 7-edge "
                         "book at 0.50x Kelly; the P5 / median / P95 BANDS propagate MONTHLY-RETURN VARIANCE "
                         "ONLY (they assume the edges hold, and widen with time). The dashed amber STRESS line "
                         "is different: it propagates EDGE UNCERTAINTY by compounding the stress %/mo. The run "
                         f"RESET to $1,000 on {_run_meta('reset_date', '2026-06-21')} (algorithm changed; prior "
                         "track archived) — month 0 = $1,000 (YOU ARE HERE). Inputs: median "
                         f"{100*med_mo:+.1f}%/mo, P5 {100*p5_mo:+.1f}%/mo, P95 {100*p95_mo:+.1f}%/mo -> after 12 "
                         f"paper months the median path reaches ${end_med:,.0f} (P5 ${end_p5:,.0f} / P95 "
                         f"${end_p95:,.0f}).{stress_txt} Activated AHEAD of the forward gate; paper $1,000 only, "
                         "$0 REAL, never realized P&L."),
                 id="run-projection-card")


def panel_gate_board():
    """THE centerpiece: per-edge gate board. Each edge -> status badge, the specific gate it must pass, a
    settled-progress bar (n/threshold), and the staged Kelly stake. Source: run_gates. Paper/forward only."""
    d = table("run_gates")
    if d.empty:
        return card([html.H3("Deploy-Gate Board"), empty_state("Fills from the $1,000 staged-harness ledger.")])
    has_state = "paper_state" in d.columns
    rows = []
    for _, r in d.iterrows():
        kind, col, lbl = _GATE_STATUS_STYLE.get(r["status"], ("neut", NEUTRAL, str(r["status"]).upper()))
        nset = int(r["n_settled"] or 0); nreq = int(r["n_required"] or 1)
        pct = min(100, int(100 * nset / max(nreq, 1)))
        edge = "—" if _isnull(r["edge_c"]) else f"{r['edge_c']:+.2f}c"
        cil = "—" if _isnull(r["ci_lo"]) else f"{r['ci_lo']:+.1f}"
        cih = "—" if _isnull(r["ci_hi"]) else f"{r['ci_hi']:+.1f}"
        stake = float(r["staged_stake"] or 0.0)
        stake_str = f"${stake:,.2f}" if stake > 0 else "$0"
        bar_col = "var(--accent)" if kind == "good" else ("var(--amber)" if kind == "warn" else "var(--neutral)")
        # ACTIVATION (orthogonal to the gate): ACTIVE = user-activated in the PAPER run ahead of the gate.
        is_active = has_state and str(r.get("paper_state") or "").upper() == "ACTIVE"
        active_stake = float(r.get("active_stake") or 0.0) if has_state else 0.0
        # State chip: green ACTIVE·PAPER vs neutral STAGED. Stake line shows the live PAPER stake when active.
        if is_active:
            state_chip = badge("ACTIVE · PAPER", "good")
            stake_block = html.Div([
                html.Span("PAPER STAKE ", className="u-label"),
                html.Span(f"${active_stake:,.2f}", className="mono",
                          style={"fontWeight": "700", "color": GREEN}),
                html.Span("  · active in the $1k paper run · $0 REAL until gate PASS", className="sub",
                          style={"fontSize": "10px", "color": AMBER})], className="gb-stake")
        else:
            state_chip = badge("STAGED", "neut")
            stake_block = html.Div([
                html.Span("STAGED STAKE ", className="u-label"),
                html.Span(stake_str, className="mono",
                          style={"fontWeight": "700", "color": (NEUTRAL if stake > 0 else DIM)}),
                html.Span("  · $0 until gate PASS", className="sub",
                          style={"fontSize": "10px", "color": AMBER})], className="gb-stake")
        rows.append(html.Div([
            html.Div([html.Div(r["edge_label"], className="gb-name"),
                      html.Div([state_chip, badge(lbl, kind)],
                               style={"display": "flex", "gap": "5px"})], className="gb-head"),
            html.Div([html.Span("EDGE ", className="u-label"),
                      html.Span(edge, className="mono", style={"color": col, "fontWeight": "700"}),
                      html.Span(f"  CI [{cil}, {cih}]c", className="sub", style={"fontSize": "10.5px"}),
                      html.Span(f"  ·  {r['season']}", className="sub", style={"fontSize": "10.5px"})],
                     className="gb-edge"),
            html.Div(r["gate_desc"], className="gb-gate sub"),
            html.Div([
                html.Div([html.Span("FORWARD GATE PROGRESS", className="u-label"),
                          html.Span(f"{nset} / {nreq} settled  ·  {str(r['status']).split('-')[-1].upper()}",
                                    className="mono", style={"fontSize": "11px", "color": DIM})],
                         className="gb-prog-lbl"),
                html.Div(html.Div(className="bar-fill", style={"width": f"{pct}%", "background": bar_col}),
                         className="bar-track")], className="gb-prog"),
            stake_block],
            className="gb-card", style=({"borderLeft": f"3px solid {GREEN}"} if is_active else {})))
    n_active = sum(1 for _, r in d.iterrows()
                   if has_state and str(r.get("paper_state") or "").upper() == "ACTIVE")
    return panel("Deploy-Gate Board — Active vs Staged, and What Unlocks REAL Capital",
                 [html.Div(rows, className="gb-grid")],
                 caption=(f"Each stream's paper state ({n_active} ACTIVE · PAPER vs the rest STAGED), the "
                          f"pre-registered gate it must pass, forward progress, and its stake. REAL deploy "
                          f"($0 live) still needs a gate PASS."),
                 drawer=(f"Each paper stream shows its PAPER state ({n_active} green ACTIVE · PAPER vs the rest "
                         "STAGED), the SPECIFIC pre-registered gate it must pass (docs/FORWARD_PROTOCOL A2/A3/"
                         "A4), forward progress, and its stake. ACTIVE = user-activated in the $1,000 PAPER run "
                         "AHEAD of the gate (paper money only). Activation is ORTHOGONAL to the gate — every row "
                         "is still ACCUMULATING, shown beside its stake. REAL deployment ($0 live) still "
                         "requires a gate PASS. WATCH · NO PATH = validated-but-not-promotable. Paper/forward "
                         "only, never realized P&L."),
                 id="gate-board-card")


def panel_staged_alloc():
    """Per-stream allocation bar: ACTIVE-paper stakes (green) and STAGED-$0 Kelly stakes (neutral). Each bar
    is the dollar amount the stream holds in the PAPER run (ACTIVE) or WOULD hold if its gate passes (STAGED).
    Source: run_gates."""
    d = table("run_gates")
    if d.empty:
        return card([html.H3("Allocation"), empty_state("Fills from the staged-harness ledger.")])
    d = d.copy()
    has_state = "paper_state" in d.columns
    if has_state:
        d["is_active"] = d["paper_state"].astype(str).str.upper() == "ACTIVE"
        # bar value: ACTIVE rows use the live paper stake; STAGED rows use the staged ($0-live) Kelly stake.
        d["alloc"] = d.apply(lambda r: float(r.get("active_stake") or 0.0) if r["is_active"]
                             else float(r.get("staged_stake") or 0.0), axis=1)
    else:
        d["is_active"] = False
        d["alloc"] = d["staged_stake"].fillna(0.0)
    d = d[d["alloc"].fillna(0) > 0].sort_values("alloc", ascending=True)
    if d.empty:
        return card([html.H3("Allocation — All $0"),
                     _cap("Every stream is STAGED at $0 until its forward gate passes."),
                     empty_state("No stake yet.")])
    colors = [GREEN if a else NEUTRAL for a in d["is_active"]]
    labels = [f"{lbl}  ·  {'ACTIVE' if a else 'STAGED'}"
              for lbl, a in zip(d["edge_label"], d["is_active"])]
    fig = go.Figure()
    fig.add_bar(y=labels, x=d["alloc"], orientation="h", marker_color=colors,
                text=[f"${v:,.2f}" for v in d["alloc"]], textposition="outside", cliponaxis=False,
                customdata=d[["season", "edge_c", "is_active"]].values,
                hovertemplate="<b>%{y}</b><br>$%{x:,.2f}"
                              "<br>%{customdata[0]} · edge %{customdata[1]:+.2f}c"
                              "<br>paper only — $0 REAL until gate PASS<extra></extra>")
    fig.update_layout(title=None, margin=dict(l=210, r=44, t=10, b=36))
    fig.update_xaxes(title="paper allocation ($ — $0 REAL until gate PASS)", tickprefix="$", tickformat=",.0f")
    fig.update_yaxes(title="")
    active_total = float(d[d["is_active"]]["alloc"].sum())
    staged_total = float(d[~d["is_active"]]["alloc"].sum())
    return card([html.H3("Per-Stream Allocation — Active (Paper) vs Staged"),
                 _cap(f"GREEN = ACTIVE in the $1,000 PAPER run (${active_total:,.2f} total — warm streams "
                      f"user-activated ahead of the gate, plus any cold stream auto-staged once the season "
                      f"turns AND its gate passes); NEUTRAL = STAGED at $0 live (${staged_total:,.2f}, the gate "
                      f"still governs). The cold-season daily-low streams carry the largest STAGED stakes. "
                      f"All are $0 REAL today — REAL deploy needs a gate PASS. Paper model, never realized P&L."),
                 graph(_tpl(fig, h=320, legend=False))])


def panel_open_positions():
    """PENDING PAPER TRADES: open, unsettled paper signals -- timeline scatter by target date + a table.
    Source: open_positions. Honest: paper signals awaiting settlement; no real orders."""
    d = table("open_positions")
    if d.empty:
        return card([html.H3("Pending Paper Trades"),
                     empty_state("No open paper signals — all logged signals have settled.")])
    d = d.copy()
    # FIX 4 (2026-06-21): the $1,000 run pending list shows ONLY the deployed in-book streams (S1 high NY/LAX/
    # CHI + daily-low S1). S3/S3early (in_1k_book==False) are research signals surfaced in their own panel.
    if "in_1k_book" in d.columns:
        d = d[d["in_1k_book"] == True].copy()        # noqa: E712 -- explicit bool match
    # HELD = FUNDED only (2026-06-22): contracts/paid are set ONLY for positions the harness allocated a stake
    # to. Deployed-but-UNFUNDED daily-low signals (e.g. NY/CHI/PHIL-low — tradable but NOT in the activated $1k
    # book) previously showed a "—" for contracts. We DON'T hold them, so drop them from the held list and note
    # the count below (they still settle/score normally; this is display only).
    unfunded_n = 0
    unfunded_label = ""
    if "contracts" in d.columns:
        _unf = d[d["contracts"].isna()]
        unfunded_n = int(len(_unf))
        if unfunded_n:
            unfunded_label = ", ".join(sorted({f"{c}-{str(m).lower()}"
                                               for c, m in zip(_unf["city"], _unf["market"])}))
        d = d[d["contracts"].notna()].copy()
    if d.empty:
        return card([html.H3("Pending Paper Trades"),
                     empty_state("No funded open positions in the $1,000 book right now."
                                 + (f"  ({unfunded_n} deployed-but-unfunded daily-low signal(s) tracked: "
                                    f"{unfunded_label}.)" if unfunded_n else ""))])
    import pandas as _pd
    d["_dt"] = _pd.to_datetime(d["target_date"], errors="coerce")
    scat = d.dropna(subset=["_dt"])
    fig = go.Figure()
    if not scat.empty:
        for mkt, color in (("HIGH", GREEN), ("LOW", NEUTRAL)):
            sub = scat[scat["market"] == mkt]
            if sub.empty:
                continue
            fig.add_scatter(x=sub["_dt"], y=sub["edge_c"], mode="markers", name=f"{mkt} book",
                            marker=dict(size=11, color=color, opacity=.82,
                                        line=dict(width=1, color="rgba(255,255,255,.25)"),
                                        symbol=["circle" if s == "YES" else "diamond" for s in sub["side"]]),
                            customdata=sub[["city", "stream", "side", "ticker"]].values,
                            hovertemplate="<b>%{customdata[0]} %{customdata[1]}</b> %{customdata[2]}"
                                          "<br>%{customdata[3]}<br>model edge %{y:+.1f}c"
                                          "<br>target %{x|%Y-%m-%d}<extra></extra>")
        fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
        fig.update_layout(title=None)
        fig.update_yaxes(title="model edge at scan (c / contract)", ticksuffix="c", tickformat="+,.0f")
        fig.update_xaxes(title="target settle date", nticks=8)
    # FIX 5 (2026-06-19): mark-to-market column "entry -> current" with a green up / red down chip on the
    # side held. PAPER MARK, UNREALIZED -- not realized P&L, not a real position.
    has_mark = "current_price" in d.columns
    def _entry_to_current(row):
        ep = row.get("entry_price"); cp = row.get("current_price"); md = row.get("mark_delta")
        dirn = row.get("direction")
        ep_s = "—" if _isnull(ep) else f"{ep:.0f}c"
        if _isnull(cp):
            return html.Span([html.Span(ep_s, className="mono"),
                              html.Span(" → —", className="sub")], title="live quote unavailable")
        arrow = "▲" if dirn == "up" else ("▼" if dirn == "down" else "▬")
        chip_cls = "pos" if dirn == "up" else ("neg" if dirn == "down" else "")
        dtxt = "" if _isnull(md) else f" {md:+.0f}c"
        return html.Span([html.Span(f"{ep_s} → ", className="mono sub"),
                          html.Span(f"{cp:.0f}c", className="mono"),
                          html.Span(f"  {arrow}{dtxt}", className=f"mono qb-edge {chip_cls}",
                                    style={"marginLeft": "4px", "fontSize": "11px"})])
    # PRICE SPARKLINE (Part B): a tiny per-row line of entry -> snapshots -> current (cents). Grows over
    # runs as each materialize appends a snapshot. Green if last>=first else red. Honest: PAPER mark.
    import json as _json
    has_series = "price_series" in d.columns
    def _row_spark(row):
        raw = row.get("price_series")
        try:
            seq = _json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (ValueError, TypeError):
            seq = []
        seq = [float(v) for v in seq if v is not None]
        ep = row.get("entry_price"); cp = row.get("current_price")
        ep_f = None if _isnull(ep) else float(ep)
        # ANCHOR the trend at the ENTRY price (cost basis) so the sparkline's shape AND color reflect P&L
        # vs entry -- matching the "mark" chip. Coloring by first-snapshot-vs-last put GREEN lines on
        # positions that are actually BELOW entry (and RED on winners) whenever the first snapshot != entry
        # (2026-07-01 fix: 9/28 positions were mismatched). The anchor also fixes flat-at-0 losers.
        if ep_f is not None and (not seq or abs(seq[0] - ep_f) > 1e-9):
            seq = [ep_f] + seq
        if len(seq) < 2:
            # one point (or none) -> a single dot so the cell is non-empty + honest about thin history
            pts = seq or ([ep_f] if ep_f is not None else [])
            lbl = f"{pts[0]:.0f}c" if pts else "—"
            return html.Span(["• ", html.Span(lbl, className="mono sub")],
                             title="accumulating price snapshots (grows each run)")
        # color EXPLICITLY by current-vs-entry (the P&L sign) so it can NEVER disagree with the mark chip
        col = (GREEN if float(cp) >= ep_f else RED) if (not _isnull(cp) and ep_f is not None) else None
        return _spark(seq, color=col, height=30, fill=True)   # taller + filled = more detail
    # NET-PER-DAY SWING (USER ASK 2026-06-24: make it match the per-resolution-day charts EXACTLY). Built from
    # the SAME source as those charts -- _resday_summary(date) (last-ts cumulative paid vs value from
    # resolution_day_curve) -- so the numbers are identical to the section headers below by construction.
    def _mmdd(day):
        s = str(day)
        return f"{int(s[5:7])}/{int(s[8:10])}" if len(s) >= 10 else s

    def _net_per_day_block():
        recs = [(dt, _resday_summary(dt)) for dt in _resolution_dates()]
        recs = [(dt, s) for dt, s in recs if s]
        if not recs:
            return html.Div("Accumulating net-per-day marks (grows each run).", className="sub",
                            style={"fontSize": "11px"})
        items = []
        for dt, s in recs:
            paid, val, net, pct, nct = s["paid"], s["value"], s["net"], s["pct"], s["n_ct"]
            up = net > 0; down = net < 0
            dcls = "pos" if up else ("neg" if down else "")
            arrow = "▲" if up else ("▼" if down else "▬")
            pct_s = "" if pct is None else f" ({pct:+.1f}%)"
            dl_s = f"{arrow}${abs(net):,.2f}{pct_s}"
            nc_s = "" if nct is None else f" · {int(nct)} ct"
            items.append(html.Div([
                html.Span(f"{_mmdd(dt)}: ", className="sub", style={"opacity": .75}),
                html.Span("paid ", className="sub", style={"fontSize": "10.5px"}),
                html.Span(f"${paid:,.2f}", className="mono sub",
                          title="cumulative cost basis of positions resolving that day (= the chart)"),
                html.Span(" → now ", className="sub", style={"fontSize": "10.5px"}),
                html.Span(f"${val:,.2f}", className="mono",
                          title="cumulative current value (= the per-resolution-day chart)"),
                html.Span("  " + dl_s, className=f"mono qb-edge {dcls}",
                          style={"marginLeft": "6px", "fontSize": "11px"}),
                html.Span(nc_s, className="sub", style={"fontSize": "10px", "opacity": .7})],
                style={"whiteSpace": "nowrap", "lineHeight": "1.6"}))
        return html.Div(items, style={"fontSize": "12px"})

    if has_mark:
        d = d.copy()
        d["mark"] = d.apply(_entry_to_current, axis=1)
    if has_series:
        if not has_mark:
            d = d.copy()
        d["trend"] = d.apply(_row_spark, axis=1)
    # contracts/paid_c are added by the producer (2026-06-22); guard so the app degrades gracefully if it
    # deploys before the producer re-materializes the new columns into the cloud table.
    _has_size = "contracts" in d.columns and "paid_c" in d.columns
    cols = ["target_date", "city", "market", "stream", "ticker", "side", "edge_c"]
    if _has_size:
        cols += ["contracts", "paid_c"]
    cols += ["age_h"]
    rename = {"edge_c": "Model Edge", "age_h": "Age", "target_date": "Target Settle",
              "contracts": "Contracts", "paid_c": "Paid ($)"}
    fmtmap = {"edge_c": _cents1, "age_h": lambda v: "—" if _isnull(v) else f"{v:.0f}h",
              "contracts": lambda v: "—" if _isnull(v) else f"{float(v):,.0f}",
              "paid_c": lambda v: "—" if _isnull(v) else f"${float(v) / 100.0:,.2f}"}
    if has_mark:
        cols = cols + ["mark"]
        rename["mark"] = "Entry → Current (paper mark)"
        fmtmap["mark"] = lambda v: v          # already an html element
    if has_series:
        cols = cols + ["trend"]
        rename["trend"] = "Price Trend (paper)"
        fmtmap["trend"] = lambda v: v         # already an html element
    show = present(d, drop=["_dt", "price_series", "entry", "entry_price", "current_price",
                            "mark_delta", "direction", "in_1k_book"],
                   rename=rename, fmt=fmtmap, order=cols)
    n = len(d)
    return panel(["Pending Paper Trades — Open, Unsettled Signals  ", info_dot(
                    "Paper signals the monitors have LOGGED but that have NOT yet settled. The Entry → Current "
                    "column is a PAPER MARK, UNREALIZED — not real money, not realized P&L, not a real "
                    "position. No orders, no account. Current = live public YES-mid for the side held.")],
                 [graph(_tpl(fig, h=300)) if not scat.empty else html.Div(),
                 # NET-PER-DAY swing across all open contracts (USER ASK 2026-06-21)
                 html.Div([html.Div("Net-Per-Day Swing (paper, unrealized — all open contracts)",
                                     className="u-label", style={"margin": "6px 0 4px"}),
                           _net_per_day_block()],
                          style={"margin": "4px 0 10px"}),
                 # deliverable #3: show ALL pending rows (no row cap / no truncation footer)
                 pro_table(show, present_df=False,
                           align_left=("Side", "Market", "Stream", "Ticker",
                                       "Entry → Current (paper mark)")),
                 # FUNDED-only note (2026-06-22): explain the deployed-but-unfunded daily-low signals we dropped
                 (html.Div([html.B(f"{unfunded_n} deployed daily-low signal(s) "),
                            f"({unfunded_label}) are logged and tradable but NOT funded in this $1,000 run — no "
                            f"stake is allocated, so they're not held above. They still settle and score."],
                           className="sub", style={"fontSize": "11px", "marginTop": "8px", "opacity": .85})
                  if unfunded_n else html.Div())],
                 caption=(f"{n} paper signals logged but not yet settled, plotted by target date and model "
                          f"edge. The Net-Per-Day Swing aggregates entry cost vs current mark across all open "
                          f"contracts — a paper, unrealized mark. $0 real."),
                 drawer=(f"{n} paper signals logged across the forward monitors but NOT yet settled, plotted by "
                         f"target settle date and model edge (circle = YES side, diamond = NO). Contracts = the "
                         f"staged $1,000-harness size and Paid ($) = the cost basis per position (contracts × "
                         f"entry price; paper stake, no real orders). The Entry → Current column marks each "
                         f"paper signal to the live public quote with a green up / red down chip. The "
                         f"Net-Per-Day Swing below AGGREGATES across ALL open paper contracts per calendar day "
                         f"(sum of entry cost vs sum of current marks) — a PAPER, UNREALIZED mark, NOT real "
                         f"money or realized P&L. Once signals settle they feed the gate-progress counters "
                         f"above."),
                 id="open-positions-card")


def panel_research_edge_signals():
    """RESEARCH-EDGE signals that are NOT in the $1,000 book (FIX 4, 2026-06-21): the open S3 / S3early
    near-money signals (in_1k_book==False). Kept VISIBLE for transparency but clearly separated from the
    $1k run so the two are never conflated. Source: open_positions (in_1k_book==False). Paper / unrealized
    mark of public quotes, never realized P&L. Empty -> a short note."""
    d = table("open_positions")
    if d.empty or "in_1k_book" not in d.columns:
        return card([html.H3("Research Edge Signals — S3 / S3early + watch streams (NOT in the $1,000 book)"),
                     empty_state("No open research-edge signals right now.")])
    d = d[d["in_1k_book"] == False].copy()           # noqa: E712 -- explicit bool match
    if d.empty:
        return card([html.H3("Research Edge Signals — S3 / S3early + watch streams (NOT in the $1,000 book)"),
                     empty_state("No open research-edge signals right now.")])

    def _entry_to_current(row):
        ep = row.get("entry_price"); cp = row.get("current_price"); md = row.get("mark_delta")
        dirn = row.get("direction")
        ep_s = "—" if _isnull(ep) else f"{ep:.0f}c"
        if _isnull(cp):
            return html.Span([html.Span(ep_s, className="mono"),
                              html.Span(" → —", className="sub")], title="live quote unavailable")
        arrow = "▲" if dirn == "up" else ("▼" if dirn == "down" else "▬")
        chip_cls = "pos" if dirn == "up" else ("neg" if dirn == "down" else "")
        dtxt = "" if _isnull(md) else f" {md:+.0f}c"
        return html.Span([html.Span(f"{ep_s} → ", className="mono sub"),
                          html.Span(f"{cp:.0f}c", className="mono"),
                          html.Span(f"  {arrow}{dtxt}", className=f"mono qb-edge {chip_cls}",
                                    style={"marginLeft": "4px", "fontSize": "11px"})])
    has_mark = "current_price" in d.columns
    if has_mark:
        d["mark"] = d.apply(_entry_to_current, axis=1)
    cols = ["target_date", "city", "market", "stream", "ticker", "side", "edge_c", "age_h"]
    rename = {"edge_c": "Model Edge", "age_h": "Age", "target_date": "Target Settle"}
    fmtmap = {"edge_c": _cents1, "age_h": lambda v: "—" if _isnull(v) else f"{v:.0f}h"}
    if has_mark:
        cols = cols + ["mark"]
        rename["mark"] = "Entry → Current (paper mark)"
        fmtmap["mark"] = lambda v: v
    show = present(d, drop=["_dt", "price_series", "entry", "entry_price", "current_price",
                            "mark_delta", "direction", "in_1k_book"],
                   rename=rename, fmt=fmtmap, order=cols)
    n = len(d)
    return card([html.H3(["Research Edge Signals — S3 / S3early + watch streams (NOT in the $1,000 book)  ", info_dot(
                    "Signals the monitors log but that are NOT deployed in the $1,000 paper run: S3 / S3early "
                    "near-money research edges plus any non-deployed WATCH stream (e.g. MIA-high S1). Shown "
                    "here for transparency, kept SEPARATE from the run's pending list and net-per-day so they "
                    "are never conflated with the $1k book. Paper / unrealized mark of public quotes — no "
                    "orders, no account, never realized P&L.")]),
                 _cap(f"{n} open paper signals outside the $1,000 book (S3 / S3early + non-deployed watch). "
                      f"These do "
                      f"NOT affect the $1,000 paper equity, the gate board, or the net-per-day swing — they are "
                      f"a separate research stream. Paper / unrealized, never realized P&L."),
                 pro_table(show, present_df=False,
                           align_left=("Side", "Market", "Stream", "Ticker",
                                       "Entry → Current (paper mark)"))],
                id="research-edge-signals-card")


def render_bankroll():
    reset_date = _run_meta("reset_date", "2026-06-21")
    return html.Div([section("$1,000 Paper Run — Honest Gate Tracker"),
                     html.Div([f"The $1,000 paper bankroll, RESET fresh to $1,000 on {reset_date} (the algorithm "
                               "changed; the prior track is archived), and the pre-registered gates that govern "
                               "it. ", html.B("LIVE capital today = $0"), " — every edge is STAGED until its "
                               "forward gate passes. No orders, no account, no real money anywhere. Every figure "
                               "is paper/backtest/forward, never realized P&L."], className="sub",
                              style={"marginBottom": "12px"}),
                     panel_run_header(),
                     html.Div([html.Div(panel_run_equity(), className="col-5"),
                               html.Div(panel_staged_alloc(), className="col-7")], className="grid12",
                              style={"marginTop": "10px"}),
                     html.Div([html.Div(panel_equity_composition(), className="col-12")], className="grid12",
                              style={"marginTop": "10px"}),
                     html.Div([html.Div(panel_daily_pnl(), className="col-12")], className="grid12",
                              style={"marginTop": "10px"}),
                     html.Div([html.Div(panel_resolution_day_curve(), className="col-12")], className="grid12",
                              style={"marginTop": "10px"}),
                     html.Div([html.Div(panel_run_projection(), className="col-12")], className="grid12",
                              style={"marginTop": "10px"}),
                     html.Div([html.Div(panel_gate_board(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_open_positions(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_research_edge_signals(), className="col-12")], className="grid12")])


def card(children, cls="", **kw):
    return html.Div(children, className=f"card {cls}".strip(), **kw)


def badge(text, kind="neut"):
    return html.Span(text, className=f"badge {kind}")


def section(title):
    return html.Div(title, className="page-title")


def empty_state(msg, icon=None):
    """Standard empty panel: icon + 'fills when X runs' message. WP-03: unicode glyph -> SVG clock."""
    ic = icon if icon is not None else svg_icon("clock", size=24)
    return html.Div([html.Div(ic, className="es-ic"), html.Div(msg, className="es-msg")],
                    className="empty-state")


# ---- ONE number/unit registry: shared by KPIs, tables, AND hovertemplates ----
def _isnull(v):
    return v is None or (isinstance(v, float) and v != v)


def fmt(value, unit, dash="—"):
    """Single source of truth for value formatting. Returns (display_value, suffix).
    unit one of: $, c/contract, F/°F, cities, pct (0..1), brier, int, min, count, "" (plain).
    Use fmt_s() for a single joined string (tables/hovertemplates)."""
    if _isnull(value):
        return dash, ""
    if unit == "$":
        return (f"${value:,.2f}" if abs(value) < 10000 else f"${value:,.0f}"), ""   # WP-09: cents under $10k
    if unit in ("c/contract", "c/ct"):
        return f"{value:+.2f}", " c/ct"
    if unit in ("F", "°F"):
        return f"{value:.2f}", "°F"
    if unit == "cities":
        return f"{int(round(value))}", " cities"
    if unit == "pct":
        return f"{100 * value:.1f}", "%"
    if unit == "brier":
        return f"{value:.4f}", ""
    if unit == "int":
        return f"{value:,.0f}", ""
    if unit == "min":
        return f"{value:.0f}", " min"
    if isinstance(value, float):
        return f"{value:.2f}", ""
    return f"{value}", ""


def fmt_s(value, unit, dash="—"):
    v, s = fmt(value, unit, dash)
    return f"{v}{s}"


def kpi_card(label, value, unit, status):
    val, suffix = fmt(value, unit)
    kind = {"BACKTEST": "good", "WALK-FORWARD": "good"}.get(status, "warn" if status != "NOT STARTED" else "neut")
    return html.Div([html.Div(label, className="label"),
                     html.Div([html.Span(val, className="val mono"),
                               html.Span(suffix, className="unit")]),
                     badge(status, kind)], className="card kpi")


# ---- presentation helpers (column formatters all delegate to the fmt() registry) ----
# raw store column -> (Title-Case header, formatter). Unmapped columns fall back to Title Case + str.
def _f2(v):    # 2-decimal float
    return "—" if _isnull(v) else f"{v:.2f}"


def _f1(v):
    return "—" if _isnull(v) else f"{v:.1f}"


def _cents(v):   # signed cents -> "+1.23c"
    return "—" if _isnull(v) else fmt(v, "c/contract")[0] + "c"


def _cents1(v):
    return "—" if _isnull(v) else f"{v:+.1f}c"


def _degf(v):
    return fmt_s(v, "°F")


def _pct01(v):   # 0..1 -> %
    return fmt_s(v, "pct")


def _brier(v):
    return fmt_s(v, "brier")


def _intf(v):
    return fmt_s(v, "int")


def _minf(v):
    return fmt_s(v, "min")


COLUMN_FMT = {
    "city": ("City", None),
    "stream": ("Stream", None),
    "source": ("Source", None),
    "status": ("Status", None),
    "n": ("Trades (n)", _intf),
    "brier_model": ("Brier (model)", _brier),
    "brier_market": ("Brier (market)", _brier),
    "avg_net_c": ("Avg Net", _cents),
    "s1_net_c": ("S1 Net", _cents),
    "win_rate": ("Win Rate", _pct01),
    "beats_market": ("Beats Market", lambda v: "Yes" if v == 1 else "No"),
    "rmse_base": ("RMSE (base-5)", _degf),
    "rmse_exp": ("RMSE (expanded)", _degf),
    "members_rmse": ("RMSE (members)", _degf),
    "s2x_rmse": ("RMSE (S2X)", _degf),
    "warm": ("Warm-season RMSE", _degf),
    "cold": ("Cold-season RMSE", _degf),
    "ci_lo": ("CI Low", _cents1),
    "ci_hi": ("CI High", _cents1),
    "p_gt0": ("P(edge > 0)", _pct01),
    "trades_per_month": ("Trades / Month", _f1),
    "forecast_f": ("Forecast High", _degf),
    "target_date": ("Target Date", None),
    "n_locks": ("Locks (n)", _intf),
    "lead_min": ("Median Lead", _minf),
    "gap_fine_c": ("Gap at Detection", _cents1),
    "gap_metar_c": ("Gap at METAR", _cents1),
    "frac_early_capturable": ("Early-Capturable", _pct01),
    "n_settled": ("Settled (n)", _intf),
    "n_required": ("Required (n)", _intf),
    "gate_status": ("Gate Status", None),
    "changepoint_date": ("Changepoint", None),
    "item": ("Item", None),
    "detail": ("Detail", None),
    "value": ("Value", None),
    "unit": ("Unit", None),
    "label": ("Metric", None),
    "market": ("Market", None),
    "ticker": ("Ticker", None),
    "side": ("Side", None),
    "edge_label": ("Edge", None),
    "season": ("Season", None),
    "age_h": ("Age", None),
}


def _titlecase(col):
    return " ".join(w.upper() if w.isupper() else w.capitalize() for w in col.replace("_", " ").split())


def present(df, drop=(), rename=None, fmt=None, order=None):
    """Return a display-ready copy: format numbers, humanize headers. rename/fmt override COLUMN_FMT."""
    rename = rename or {}; fmt = fmt or {}
    df = df.drop(columns=[c for c in drop if c in df.columns]).copy()
    if order:
        df = df[[c for c in order if c in df.columns] + [c for c in df.columns if c not in order]]
    headers = {}
    for c in df.columns:
        hdr, formatter = COLUMN_FMT.get(c, (_titlecase(c), None))
        if c in fmt:
            formatter = fmt[c]
        if c in rename:
            hdr = rename[c]
        if formatter is not None:
            df[c] = df[c].map(formatter)
        headers[c] = hdr
    return df.rename(columns=headers)


# Header names that are LABELS (left-aligned, regular weight). Everything else is treated as a
# numeric/metric column -> right-aligned, mono tabular-nums. (Headers are post-present() Title-Case.)
_LABEL_HEADERS = {"City", "Stream", "Source", "Status", "Item", "Detail", "Metric", "Gate Status",
                  "Changepoint", "Target Date", "Unit", "Beats Market", "Market", "Ticker", "Side",
                  "Edge", "Season", "Target Settle"}


def _is_label_col(header, series):
    if header in _LABEL_HEADERS:
        return True
    # any column whose cells are non-numeric strings (e.g. "—" only is still numeric-ish) -> label
    for v in series:
        s = str(v).strip()
        if s in ("", "—", "-"):
            continue
        # strip the formatting glyphs present() adds, then test numeric
        t = s.replace(",", "").replace("$", "").replace("%", "").replace("°F", "").replace("c", "")
        t = t.replace("+", "").replace(" min", "").replace("F", "").strip()
        try:
            float(t)
        except ValueError:
            return True
    return False


def pro_table(df, present_df=True, max_rows=None, align_left=None, **_ignore):
    """Investor-grade custom HTML table (NOT a DataTable). Neon glass theme, no spreadsheet grid:
    uppercase dim header w/ a single bottom rule, thin zebra rows + hover, right-aligned mono numerics,
    left-aligned labels. Keeps present()/COLUMN_FMT formatting. `max_rows` truncates (footer note)."""
    if present_df:
        df = present(df)
    if df is None or df.empty:
        return html.Div("—", className="sub")
    cols = list(df.columns)
    forced_left = set(align_left or ())
    left = {c: (c in forced_left or _is_label_col(c, df[c])) for c in cols}

    def th(c):
        cls = "pt-th " + ("pt-l" if left[c] else "pt-r")
        return html.Th(c, className=cls)

    from dash.development.base_component import Component as _Comp
    def td(c, v):
        cls = "pt-td " + ("pt-l" if left[c] else "pt-r mono")
        # pass Dash components (e.g. the mark-to-market chip) through as-is, never stringify them
        if isinstance(v, _Comp):
            return html.Td(v, className=cls)
        s = "—" if (v is None or (isinstance(v, float) and v != v)) else str(v)
        # desaturated sign-aware numeric color (only on signed numeric cells like +/-c)
        if not left[c] and s not in ("—", "-", ""):
            if s.lstrip().startswith("+"):
                cls += " pos"
            elif s.lstrip().startswith("-") and any(ch.isdigit() for ch in s):
                cls += " neg"
        return html.Td(s, className=cls)

    rows = df.to_dict("records")
    truncated = max_rows is not None and len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    body = [html.Tr([td(c, r.get(c)) for c in cols]) for r in rows]
    table_el = html.Table([html.Thead(html.Tr([th(c) for c in cols])), html.Tbody(body)],
                          className="pro-table")
    wrap = [html.Div(table_el, className="pt-wrap")]
    if truncated:
        wrap.append(html.Div(f"Showing {max_rows} of {len(df)} rows.", className="sub pt-foot"))
    return html.Div(wrap)


# back-compat alias: every old call site used dt(...); map page_size -> max_rows.
def dt(df, present_df=True, **kw):
    return pro_table(df, present_df=present_df, max_rows=kw.pop("page_size", None),
                     align_left=kw.pop("align_left", None))


# ---- global staleness (from generated_at_utc) ----
def _data_age_min():
    """Minutes since the store was generated, or None if unparseable."""
    raw = meta_value("generated_at_utc", "")
    if not raw or raw == "—":
        return None
    # Primary path: ISO-8601 with an offset like '...+00:00' (what the producer actually writes).
    # The strptime table below never matched an offset, so the chip always read "DATA AGE UNKNOWN".
    try:
        t = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 60.0)
    except ValueError:
        pass
    for f in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            t = datetime.strptime(raw.strip(), f).replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 60.0)
        except ValueError:
            continue
    return None


def staleness_chip():
    age = _data_age_min()
    if age is None:
        return html.Span("DATA AGE UNKNOWN", className="stale-chip stale", id="stale-chip")
    if age >= STALE_AFTER_MIN:
        return html.Span(f"DATA {age:.0f} MIN OLD", className="stale-chip stale", id="stale-chip")
    return html.Span(f"DATA {age:.0f} MIN OLD", className="stale-chip fresh", id="stale-chip")


# ============================================================================================
# FILL-SCALABILITY (Mosaic -> Iris handoff 2026-06-20). The honest "more money != linearly more
# profit" story: a per-stream slippage(size) curve, a net-edge-after-fills(size) curve that bends
# down and crosses zero at the real capacity ceiling, and a bankroll-headroom view of where DEPTH
# stops linear scaling. Reads curated tables fill_scalability / fill_capacity / fill_headroom.
# CAVEAT surfaced on-panel: model_edge HELD FIXED across sizes (isolates FILLS only, not edge-erosion);
# standing re-fetched snapshot (not the lock-moment book); dead-book gaps shown, never fabricated.
# ============================================================================================
_TIER_BADGE = {"tradable": ("DEPLOYED · TRADABLE", "good"), "watch": ("WATCH", "warn"),
               "reference": ("REFERENCE (no model edge)", "neut")}

# The 7 deployed $1,000-run streams (Mosaic one_k_run_stream_coverage). Order = high cities, then low.
ONE_K_STREAMS = ["NY_high_S1", "LAX_high_S1", "CHI_high_S1",
                 "AUS_low_S1", "LAX_low_S1", "DEN_low_S1", "MIA_low_S1"]
# Documented backtest CIs for the snapshot daily-low streams that carry NO live monitor signal
# (AUS/MIA n=0). Surfaced in tooltips so we never imply a confirmed edge. Source: AI_HANDOFF_SUMMARY
# 2026-06-19 warm-season re-exam.
_EDGE_CI_TEXT = {"AUS_low_S1": "+8.38c CI[+2.50,+14.81] (warm-robust, excl 0)",
                 "MIA_low_S1": "+4.37c CI[-0.69,+8.98] (WATCH — CI touches 0)",
                 "LAX_low_S1": "+3.69c (snapshot; no-tradable-path per Crucible within-cold decay)"}
# Per-stream distinct color for the headroom chart (item 1) -- each stream gets ONE hue, no repeats.
_STREAM_COLOR = {"NY_high_S1": "#00e08a", "LAX_high_S1": "#36c5f0", "CHI_high_S1": "#d9a23a",
                 "MIA_high_S1": "#5fd0c0", "AUS_low_S1": "#b07ff0", "LAX_low_S1": "#ff8a5b",
                 "DEN_low_S1": "#7fd6a0", "MIA_low_S1": "#e85f8a"}


def _scal_stream_label(stream_id):
    """Short human label for a Mosaic stream_id, e.g. 'NY_high_S1' -> 'NY high · S1'."""
    parts = (stream_id or "").split("_")
    if len(parts) >= 3:
        return f"{parts[0]} {parts[1]} · {parts[2]}"
    return stream_id


def panel_scalability_curve():
    """AUDIT-CORRECTED 2026-06-21. NY-high is the ONLY stream with real lock-moment depth -> render its REAL
    median per-size curve (slippage rising, net-edge falling, never crossing zero within the observed book)
    + its median book size. The 6 others ONLY ever logged a 25ct fillability scalar -> shown HONESTLY as
    'fills >=25ct confirmed; full slippage curve ACCRUING (logging fixed 2026-06-21)' with NO fabricated
    ceiling. NO degenerate cent-floor 5-6 figure ceilings. Units labeled PER-MARKET (per-strike, per-day)."""
    sc = table("fill_scalability")
    cap = table("fill_capacity")
    if sc.empty and cap.empty:
        return card([html.H3("Scalability Curve — Net Edge vs Order Size"),
                     empty_state("Fills when the next pipeline run materializes the fill tables.")])
    figs = []
    present = list(dict.fromkeys(sc["stream_id"].tolist())) if not sc.empty else []
    ordered = [s for s in ONE_K_STREAMS if s in present] + [s for s in present if s not in ONE_K_STREAMS]
    for sid in ordered:
        d = sc[sc["stream_id"] == sid].sort_values("size_ct")
        if d.empty:
            continue
        tier = d["tradable_tier"].iloc[0]
        crow = cap[cap["stream_id"] == sid] if not cap.empty else pd.DataFrame()
        real_curve = bool(crow["real_curve"].iloc[0]) if not crow.empty else (len(d) > 1)
        depth_state = (crow["depth_state"].iloc[0] if not crow.empty else
                       ("real_curve" if real_curve else "accruing"))
        book_source = (crow["book_source"].iloc[0] if (not crow.empty and "book_source" in crow.columns
                       and pd.notna(crow["book_source"].iloc[0]))
                       else (d["book_source"].iloc[0] if ("book_source" in d.columns and
                             pd.notna(d["book_source"].iloc[0])) else "accruing"))
        edge0 = float(d["model_edge_c_per_ct"].iloc[0])
        clr = _STREAM_COLOR.get(sid, GREEN)
        btext, bkind = _TIER_BADGE.get(tier, ("", "neut"))

        if real_curve and len(d) > 1 and book_source == "standing_book":
            # ---- LAX/CHI-high: REAL STANDING-BOOK per-size curve from the 9-day depth archive (audit 2026-06-21).
            # Honest: a periodic standing-book snapshot, NOT a lock-moment fill (badged STANDING). 99.9-100%
            # fillable to 250ct at sub-1c slip on a positive net edge -> deep, no degenerate ceiling. ----
            n_sig = int(d["n_signals"].iloc[-1]) if pd.notna(d["n_signals"].iloc[-1]) else 0
            arch_days = (int(crow["archive_days"].iloc[0]) if (not crow.empty and
                         "archive_days" in crow.columns and pd.notna(crow["archive_days"].iloc[0])) else None)
            fpct250 = (float(crow["fill_pct_at_250"].iloc[0]) if (not crow.empty and
                       "fill_pct_at_250" in crow.columns and pd.notna(crow["fill_pct_at_250"].iloc[0]))
                       else None)
            net250 = float(d[d["size_ct"] == 250]["net_edge_after_fills_c_per_ct"].iloc[0]) if (
                250 in set(d["size_ct"])) else None
            med250 = float(d[d["size_ct"] == 250]["slippage_vs_best_c"].iloc[0]) if (
                250 in set(d["size_ct"])) else None
            fig = go.Figure()
            fig.add_scatter(x=d["size_ct"], y=d["net_edge_after_fills_c_per_ct"], mode="lines+markers",
                            name="net edge after fills", line=dict(color=clr, width=2.8),
                            marker=dict(size=6),
                            hovertemplate="%{x:,} ct/market<br>net %{y:+.2f} c/ct<extra></extra>")
            fig.add_scatter(x=d["size_ct"], y=d["slippage_vs_best_c"], mode="lines+markers",
                            name="median standing-book slippage", line=dict(color=RED, width=1.7, dash="dot"),
                            marker=dict(size=4), yaxis="y2",
                            hovertemplate="%{x:,} ct/market<br>slippage %{y:.2f} c/ct<extra></extra>")
            if "p75_slip_c" in d.columns and d["p75_slip_c"].notna().any():
                fig.add_scatter(x=d["size_ct"], y=d["p75_slip_c"], mode="lines",
                                name="p75 slippage", line=dict(color=RED_DK, width=1.0, dash="dash"),
                                marker=dict(size=3), yaxis="y2",
                                hovertemplate="%{x:,} ct/market<br>p75 slippage %{y:.2f} c/ct<extra></extra>")
            fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dash"))
            if edge0 > 0:
                fig.add_hline(y=edge0, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                              annotation_text=f"model edge {edge0:+.1f}c (fixed)",
                              annotation_position="top left", annotation_font=dict(color=DIM, size=9.5))
            fig.update_xaxes(type="log", title="order size (contracts/market, log)")
            fig.update_yaxes(title="net edge (c/ct)", ticksuffix="c")
            fig.update_layout(yaxis2=dict(title="slippage (c/ct)", overlaying="y", side="right",
                                          showgrid=False, tickfont=dict(size=11, color=DIM),
                                          title_font=dict(size=11, color=DIM), ticksuffix="c"))
            badges = [badge(btext, bkind), badge(f"REAL CURVE · n={n_sig:,}", "good"),
                      badge("STANDING BOOK · not lock-moment", "warn")]
            fpct_s = f"{fpct250:.1f}%" if fpct250 is not None else "~100%"
            sub = (f"REAL per-size curve from the {arch_days or 9}-day periodic STANDING-book depth archive "
                   f"(n≈{n_sig:,} hourly snapshots). {fpct_s} fillable to 250ct/market at "
                   f"~{(med250 or 0):.2f}c median slippage — net stays "
                   f"{('+%.1fc' % net250) if net250 is not None else 'positive'} at 250ct. HONEST: these are "
                   f"STANDING-book reads (periodic snapshot), NOT decision/lock-moment fills; cent-floor & "
                   f"near-certain books were excluded so no tick-floor ceiling.")
            figs.append(html.Div([
                html.Div([html.B(_scal_stream_label(sid)),
                          html.Div(badges, className="badge-row")],
                         className="facet-head"),
                graph(_tpl(fig, h=250, legend=True)),
                html.Div(sub, className="sub", style={"fontSize": "10.5px", "marginTop": "2px"})],
                className="col-6"))
        elif real_curve and len(d) > 1:
            # ---- NY-high: the REAL median per-size fill curve (audit 1) ----
            n_sig = int(d["n_signals"].iloc[0]) if pd.notna(d["n_signals"].iloc[0]) else 0
            med_book = float(crow["median_book_ct"].iloc[0]) if not crow.empty and pd.notna(
                crow["median_book_ct"].iloc[0]) else None
            max_book = float(crow["max_book_ct"].iloc[0]) if not crow.empty and pd.notna(
                crow["max_book_ct"].iloc[0]) else None
            net1 = float(d["net_edge_after_fills_c_per_ct"].iloc[0])  # net @ smallest size
            fig = go.Figure()
            fig.add_scatter(x=d["size_ct"], y=d["net_edge_after_fills_c_per_ct"], mode="lines+markers",
                            name="net edge after fills", line=dict(color=clr, width=2.8),
                            marker=dict(size=6),
                            hovertemplate="%{x:,} ct/market<br>net %{y:+.1f} c/ct<extra></extra>")
            fig.add_scatter(x=d["size_ct"], y=d["slippage_vs_best_c"], mode="lines+markers",
                            name="median slippage vs best", line=dict(color=RED, width=1.7, dash="dot"),
                            marker=dict(size=4), yaxis="y2",
                            hovertemplate="%{x:,} ct/market<br>slippage %{y:.1f} c/ct<extra></extra>")
            fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dash"))
            if edge0 > 0:
                fig.add_hline(y=edge0, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                              annotation_text=f"model edge {edge0:+.1f}c (fixed)",
                              annotation_position="top left", annotation_font=dict(color=DIM, size=9.5))
                x0 = float(d["size_ct"].min())
                fig.add_annotation(x=math.log10(x0), y=(edge0 + net1) / 2.0, ax=math.log10(x0), ay=0,
                                   xref="x", yref="y", axref="x", ayref="y", showarrow=True,
                                   arrowhead=2, arrowsize=1, arrowwidth=1.4, arrowcolor=AMBER,
                                   text=f"−{edge0 - net1:.0f}c<br>fee+spread", font=dict(size=9, color=AMBER),
                                   xanchor="left", align="left", standoff=2)
            # median book as a "fillable depth" reference band (NOT a degenerate ceiling)
            if med_book:
                fig.add_vline(x=med_book, line=dict(color=GREEN, width=1.4, dash="dot"),
                              annotation_text=f"median book ~{med_book/1000:,.1f}k ct",
                              annotation_position="top right", annotation_font=dict(color=GREEN, size=9))
            x_top = max((max_book or 12000), 600)
            fig.update_xaxes(type="log", title="order size (contracts/market, log)",
                             range=[0, math.log10(x_top)])
            fig.update_yaxes(title="net edge (c/ct)", ticksuffix="c")
            fig.update_layout(yaxis2=dict(title="slippage (c/ct)", overlaying="y", side="right",
                                          showgrid=False, tickfont=dict(size=11, color=DIM),
                                          title_font=dict(size=11, color=DIM), ticksuffix="c"))
            badges = [badge(btext, bkind), badge(f"REAL CURVE · n={n_sig}", "good"),
                      badge("LOCK-MOMENT", "good")]
            net250 = float(d[d["size_ct"] == 250]["net_edge_after_fills_c_per_ct"].iloc[0]) if (
                250 in set(d["size_ct"])) else None
            sub = (f"REAL median curve across {n_sig} logged lock-moment signals. 250ct/market fills at ~2c "
                   f"slippage on a ~{edge0:.0f}c edge — net stays "
                   f"{('+%.0fc' % net250) if net250 is not None else 'positive'} at 250ct, so true capacity "
                   f"is HIGH (hundreds of ct/market). Median book ~{(med_book or 0):,.0f}ct "
                   f"(range {int(crow['min_book_ct'].iloc[0]):,}–{int(max_book):,}ct). The old '100ct' "
                   f"figure was a single near-close snapshot and understated this.")
            figs.append(html.Div([
                html.Div([html.B(_scal_stream_label(sid)),
                          html.Div(badges, className="badge-row")],
                         className="facet-head"),
                graph(_tpl(fig, h=250, legend=True)),
                html.Div(sub, className="sub", style={"fontSize": "10.5px", "marginTop": "2px"})],
                className="col-6"))
        else:
            # ---- The 6 others: 25ct confirmed, curve ACCRUING (audit 2) -> honest card, NO fabricated curve ----
            slip25 = float(d[d["size_ct"] == 25]["slippage_vs_best_c"].iloc[0]) if (
                25 in set(d["size_ct"])) else None
            net25 = float(d[d["size_ct"] == 25]["net_edge_after_fills_c_per_ct"].iloc[0]) if (
                25 in set(d["size_ct"])) else None
            cent_floor = bool(crow["cent_floor_artifact"].iloc[0]) if not crow.empty else False
            ci_text = crow["edge_ci_text"].iloc[0] if (not crow.empty and pd.notna(
                crow["edge_ci_text"].iloc[0])) else None
            badges = [badge(btext, bkind), badge("CURVE ACCRUING", "warn")]
            if crow.shape[0] and bool(crow["edge_estimate_only"].iloc[0]):
                badges.append(badge("EST. EDGE (n=0 signals)", "warn"))
            lines = [html.Div([html.B("✓ fills ≥25ct confirmed"),
                               html.Span(f"  at the decision moment (slippage ~{(slip25 or 0):.0f}c, "
                                         f"net ~{(net25 or 0):+.0f}c/ct on a {edge0:.0f}c edge)",
                                         className="sub")]),
                     html.Div(["Full per-size slippage curve is ", html.B("ACCRUING"),
                               " — the monitor logging fix (2026-06-21) now records the per-signal fill_curve; "
                               "it had only the 25ct scalar historically, so any ceiling beyond ~25ct here "
                               "would be an unbased single-snapshot assumption. We do not display one."],
                              className="sub", style={"marginTop": "4px"})]
            if cent_floor:
                lines.append(html.Div(["⚠ cent-floor / longshot snapshot — ", html.B("not meaningful "
                             "capacity"), " (VWAP can't rise until the whole book is swept; any 5-6 figure "
                             "'ceiling' is a tick-floor artifact, removed)."], className="sub",
                             style={"marginTop": "4px", "color": "var(--amber)"}))
            if ci_text:
                lines.append(html.Div(f"Edge = documented backtest estimate: {ci_text}.",
                                      className="sub", style={"marginTop": "4px"}))
            figs.append(html.Div([
                html.Div([html.B(_scal_stream_label(sid)),
                          html.Div(badges, className="badge-row")],
                         className="facet-head"),
                html.Div(lines, style={"padding": "14px 4px 6px"})],
                className="col-6 card",
                style={"borderColor": "color-mix(in srgb, var(--amber) 22%, transparent)"}))
    return panel("Scalability Curve — Net Edge vs Order Size",
                [html.Div(figs, className="grid12")],
                caption="Net edge after fills vs order size (per-market). All three deployed high cities have a "
                        "real fill curve; the rest show 'confirmed ≥25ct, curve accruing' with no fabricated "
                        "ceiling.",
                drawer=("Units: every contract count is PER-MARKET (per-strike, per-day). Each city lists ~2–3 "
                     "strikes/day, each its own market with its own book; MONTHLY throughput ≈ per-market "
                     "depth × ~2–3 strikes/day × ~21 trading days. All THREE deployed high cities now have a "
                     "real per-size fill curve: NY-high from the median LOCK-MOMENT signal book, and LAX-high "
                     "+ CHI-high from a 9-day periodic STANDING-book depth archive (99.9–100% fillable to "
                     "250ct/market at sub-1c slippage — badged STANDING because these are periodic snapshots, "
                     "NOT decision/lock-moment fills). The grey dotted line is the model edge (held fixed "
                     "across sizes), the coloured line is net edge AFTER fills, and red (right axis) is VWAP "
                     "slippage. The remaining streams (the daily-LOW books + MIA-high watch + the lock-in "
                     "stream) are NOT in this archive — they keep the honest 'confirmed ≥25ct, curve ACCRUING' "
                     "state with NO fabricated ceiling. Cent-floor & near-certain books are excluded, so no "
                     "tick-floor ceiling can appear. Model edge held fixed = the FILLS side of capacity only. "
                     "Paper / public-data read — never realized P&L."))


def panel_scalability_headroom():
    """ITEM 1 redesign: the old chart gave three cities the SAME colour (coloured by depth_binds, not by
    stream) so it was unreadable. Now FACETED by stream: one small grouped-bar panel per $1k-run stream,
    each in its OWN distinct colour, with bankroll-WANTED (light, what 5%-of-bankroll implies) next to
    DEPTH-CAPPED (solid, what the book actually fills) per tier. Where the solid bar is SHORTER than the
    light one, DEPTH binds (a red ▲ marks it); where they match, BANKROLL binds (room to scale). The
    depth-binds-vs-bankroll-binds story now reads at a glance, per stream."""
    hr = table("fill_headroom")
    if hr.empty:
        return card([html.H3("Bankroll Headroom — Where Depth Stops Linear Scaling"),
                     empty_state("Fills when the next pipeline run materializes the fill tables.")])
    present = list(dict.fromkeys(hr["stream_id"].tolist()))
    ordered = [s for s in ONE_K_STREAMS if s in present] + [s for s in present if s not in ONE_K_STREAMS]
    facets = []
    for sid in ordered:
        d = hr[hr["stream_id"] == sid].sort_values("bankroll_usd")
        if d.empty:
            continue
        clr = _STREAM_COLOR.get(sid, GREEN)
        xs = [f"${int(b/1000)}k" for b in d["bankroll_usd"]]
        want = list(d["bankroll_implied_ct"])
        state = d["depth_state"].iloc[0] if "depth_state" in d else "accruing"
        # AUDIT 2: only NY-high has a measured depth cap. The 6 others are ACCRUING -> we show what bankroll
        # WANTS but do NOT plot a fabricated depth-capped bar (depth_capped_ct is None for them).
        if state == "real_curve" and d["depth_capped_ct"].notna().any():
            got = [float(g) if pd.notna(g) else None for g in d["depth_capped_ct"]]
            binds = list(d["depth_binds"])
            fig = go.Figure()
            fig.add_bar(x=xs, y=want, name="bankroll wants",
                        marker=dict(color="rgba(174,184,192,0.22)", line=dict(width=0)),
                        hovertemplate="%{x}<br>bankroll wants %{y:,.0f} ct/market<extra></extra>")
            fig.add_bar(x=xs, y=got, name="depth-capped (fillable)",
                        marker=dict(color=clr, line=dict(width=0)),
                        hovertemplate="%{x}<br>fillable %{y:,.0f} ct/market<extra></extra>")
            bx = [x for x, b in zip(xs, binds) if b]
            by = [g for g, b in zip(got, binds) if b]
            if bx:
                fig.add_scatter(x=bx, y=by, mode="markers", name="depth binds",
                                marker=dict(symbol="triangle-up", size=10, color=RED,
                                            line=dict(width=1, color="#fff")),
                                hovertemplate="%{x}<br>DEPTH binds — more bankroll buys no extra size<extra></extra>")
            fig.update_layout(barmode="overlay", title=None, bargap=0.35)
            fig.update_yaxes(title="contracts / market", type="log")
            fig.update_xaxes(title="")
            n_bind = sum(1 for b in binds if b)
            story = (f"depth binds at {n_bind}/{len(binds)} tiers" if n_bind
                     else "bankroll binds at every tier — room to scale")
            facets.append(html.Div([
                html.Div([html.B(_scal_stream_label(sid)),
                          html.Span(story, className="sub", style={"fontSize": "10px",
                                    "color": RED if n_bind else GREEN})],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "baseline"}),
                graph(_tpl(fig, h=200, legend=False))], className="col-4"))
        else:
            fig = go.Figure()
            fig.add_bar(x=xs, y=want, name="bankroll wants",
                        marker=dict(color=clr, line=dict(width=0)), opacity=0.35,
                        hovertemplate="%{x}<br>bankroll wants %{y:,.0f} ct/market<extra></extra>")
            fig.update_layout(barmode="overlay", title=None, bargap=0.35)
            fig.update_yaxes(title="contracts / market", type="log")
            fig.update_xaxes(title="")
            facets.append(html.Div([
                html.Div([html.B(_scal_stream_label(sid)),
                          html.Span("depth-cap accruing", className="sub",
                                    style={"fontSize": "10px", "color": AMBER})],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "baseline"}),
                graph(_tpl(fig, h=200, legend=False)),
                html.Div("≥25ct confirmed; per-tier depth cap not yet measurable (curve accruing).",
                         className="sub", style={"fontSize": "9.5px"})], className="col-4"))
    return panel("Bankroll Headroom — Where Depth Stops Linear Scaling",
                 [html.Div(facets, className="grid12")],
                 caption="Per-market: ghost bar = what a flat 5% stake wants, solid bar = what the book fills. "
                         "Where the solid bar is shorter (red ▲), depth binds and profit plateaus.",
                 drawer=("Contracts here are PER-MARKET (per-strike, per-day). The light ghost bar = contracts "
                      "a flat 5%-of-bankroll stake would want at each tier; the solid bar = what the book "
                      "actually fills within the net-positive size. All three deployed high cities have a "
                      "measured depth cap now (NY-high from its lock-moment curve, LAX-high + CHI-high from the "
                      "9-day STANDING-book archive — periodic snapshot, not lock-moment): where the solid bar "
                      "is shorter (red ▲), DEPTH binds — more bankroll buys NO extra size and profit plateaus. "
                      "The remaining streams (daily-LOW books + MIA-high watch) show only 'bankroll wants' "
                      "(translucent) because their per-tier depth cap is still ACCRUING (≥25ct confirmed) — we "
                      "do not plot a fabricated cap. Illustrative flat 5% stake, NOT the live Kelly engine. "
                      "Paper / public-data read — never realized P&L."))


# ============================== PAGES ==============================
def render_overview():
    kpi = table("kpi"); br = table("bankroll_run"); cs1 = table("city_s1")
    kpi_strip = kpi_spark_row()
    # bankroll
    if br.empty:
        # $1k DOLLAR curve stays OFF until separate approval. Fill this prime real estate with the REAL
        # forward-evidence graphics. WP-01: reuse the canonical panel_funnel() (was a duplicated inline
        # go.Funnel build with an off-palette pink marker); it guards its own empty case.
        bank = panel_funnel()
    else:
        # $1,000 staged run is now surfaced (its own page). Overview shows the HONEST paper equity curve
        # (LIVE allocation $0; it moves only as activated PAPER signals settle) + a pointer to the gate tracker.
        y = [float(v) for v in br["bankroll"]]
        x = list(range(len(y)))
        if len(x) == 1:
            x = [0, 1]; y = [y[0], y[0]]
        # headline = latest rebased timeline equity (realized + unrealized MTM); step-curve below = realized only
        cur_val = _latest_equity()
        line_col = GREEN if cur_val >= 1000 else RED
        fig = go.Figure()
        fig.add_scatter(x=x, y=y, name="paper equity", mode="lines+markers",
                        line=dict(color=line_col, width=2.4), marker=dict(size=6, color=line_col),
                        customdata=[str(v) for v in br["date"]] if len(br) == len(y) else None,
                        hovertemplate="%{customdata}<br>$%{y:,.2f} (paper)<extra></extra>")
        fig.add_hline(y=1000, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                      annotation_text="$1,000 baseline", annotation_position="bottom right",
                      annotation_font=dict(color=NEUTRAL, size=10))
        span = (max(y) - min(y)) or 1.0
        fig.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.2f",
                         range=[min(min(y), 1000) - span * 0.6, max(max(y), 1000) + span * 0.6])
        fig.update_xaxes(title="settlement step", showticklabels=False)
        bank = card([html.H3(f"$1,000 Paper Run — ${cur_val:,.2f}"),
                     _cap("LIVE allocation $0 — every edge is STAGED until its forward gate PASSES. The paper "
                          "equity moves only as activated PAPER signals settle (no real money). See the "
                          "$1,000 Run page for the full per-edge gate board. Paper only, never realized P&L."),
                     graph(_tpl(fig, h=240, legend=False)),
                     _cap("X-axis = settlement step — each point is one paper signal SETTLING and booking its "
                          "P&L; the curve advances one step per settled contract, NOT by calendar time. Smooth "
                          "because settlements are evenly spaced here. (The $1,000 Run page plots the same "
                          "equity against real datetime, including the unrealized mark of open positions.)")])
    # confirmed-cities mini panel. WP-01: key on DEPLOY STATUS (tradable), not `revived` -- statistical
    # revival != deployed, exactly the conflation the Multi-City page warns about.
    if not cs1.empty:
        _cs = cs1.copy()
        _cs["status"] = _city_status(_cs)
        trad = _cs[_cs["status"] == "tradable"]
    else:
        trad = cs1.iloc[0:0]
    chips = [badge(f"{r['city']}  +{r['s1_net_c']:.1f}c", "good")
             for _, r in trad.iterrows()] or [badge("NY", "good")]
    status_card = card([html.H3("System status"),
                        html.Div([html.Span(html.Span(className="dot")), "Pipeline LIVE · paper-only · ",
                                  html.Span(id="ov-updated", className="mono")], className="sub"),
                        html.Div([html.Div("Confirmed multi-city S1 edges:", className="sub",
                                           style={"margin": "10px 0 6px"}),
                                  html.Div(chips, style={"display": "flex", "gap": "8px", "flexWrap": "wrap"})])])
    return html.Div([section("Command Center"),
                     kpi_strip,
                     html.Div([html.Div(bank, style={"flex": "2", "minWidth": "420px"}),
                               html.Div(status_card, style={"flex": "1", "minWidth": "260px"})],
                              className="grid"),
                     html.Div([html.Div(panel_city_network(), className="col-7"),
                               html.Div(panel_alerts(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_strategy_perf(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_brier_gauges(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_blotter(), className="col-12")], className="grid12")])


def render_markets():
    # WP-02: de-duplicated. city_network + alerts are canonical on Overview; open_positions is canonical on
    # the $1,000 Run page. Markets keeps the live quote surfaces + source health/drift.
    return html.Div([section("Markets / Live — Public Quote Scans (Paper)"),
                     html.Div("Live public market context the bot watches. PAPER scans of public Kalshi "
                              "quotes and public weather feeds — no orders, no account, no real money.",
                              className="sub", style={"marginBottom": "10px"}),
                     html.Div([html.Div(panel_quote_board(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_market_feed(), className="col-7"),
                               html.Div(panel_scan_stream(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_city_rank(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_source_health(), className="col-6"),
                               html.Div(panel_model_drift(), className="col-6")], className="grid12")])


# WP-02: render_quantlab was REMOVED. Its panels are redistributed to their canonical homes: model_compare,
# emos_skill, pit, fan -> Model & Accuracy; dailylow_edge -> Edges & Validation; equity_curve, drawdown,
# monthly_returns, scenario -> Lab.


def panel_city_source_attribution():
    """Per-city forecast-source ATTRIBUTION (deliverable #4): for each city, which sources are ACTIVE members
    of that city's deployed ensemble (green) vs REFERENCE-ONLY / excluded (muted grey). Source: city_pool
    (authoritative monitor pool configs). Honest: shows the real per-city membership, not a guess."""
    d = table("city_pool")
    if d.empty:
        return card([html.H3("Per-City Source Attribution"),
                     empty_state("Fills from the per-city ensemble pool config.")])
    d = d.copy()
    # nicer source labels; keep ordering deterministic (active first, then reference)
    def src_chip(src, role):
        active = role == "active"
        style = {"display": "inline-block", "padding": "3px 8px", "margin": "3px 4px 0 0",
                 "borderRadius": "5px", "fontSize": "11px", "fontFamily": "JetBrains Mono, monospace",
                 "border": "1px solid"}
        if active:
            style.update({"color": GREEN, "borderColor": "rgba(0,224,138,.45)",
                          "background": "rgba(0,224,138,.08)"})
        else:
            style.update({"color": DIM, "borderColor": GRIDCOL, "background": "rgba(138,150,158,.06)"})
        return html.Span(_titlecase(src), style=style)

    # city display order + market label
    city_order = ["NY", "CHI", "LAX", "MIA", "AUS", "DEN", "PHIL"]
    d["_co"] = d["city"].map(lambda c: city_order.index(c) if c in city_order else 99)
    blocks = []
    for (city, market), sub in sorted(d.groupby(["city", "market"]),
                                      key=lambda kv: (city_order.index(kv[0][0]) if kv[0][0] in city_order
                                                      else 99, kv[0][1])):
        sub = sub.sort_values("role", ascending=True)   # 'active' < 'reference' alphabetically
        actives = sub[sub["role"] == "active"]
        refs = sub[sub["role"] == "reference"]
        pool = sub["pool"].iloc[0]
        mkt_lbl = "daily-high" if market == "high" else "daily-low"
        chips = ([src_chip(s, "active") for s in actives["source"]] +
                 [src_chip(s, "reference") for s in refs["source"]])
        blocks.append(html.Div([
            html.Div([html.Span(f"{city} ", style={"fontWeight": "700", "color": "var(--ink)"}),
                      html.Span(mkt_lbl, className="sub"),
                      html.Span(f"  ·  {len(actives)} active "
                                f"({'expanded pool' if pool == 'full' else 'core 5 members'})",
                                className="sub", style={"fontSize": "10.5px"})],
                     style={"marginBottom": "2px"}),
            html.Div(chips)], className="col-6", style={"marginBottom": "10px"}))
    legend = html.Div([
        html.Span("green = ACTIVE member (factored into this city's ensemble)",
                  style={"color": GREEN, "fontWeight": "600"}),
        html.Span("   ·   ", className="sub"),
        html.Span("grey = reference-only / excluded", style={"color": DIM, "fontWeight": "600"})],
        className="sub", style={"marginBottom": "8px"})
    return card([html.H3("Per-City Source Attribution — Active Members vs Reference-Only"),
                 _cap("Which forecast sources are ACTUALLY factored into each city's deployed ensemble "
                      "(GREEN active members) versus carried for REFERENCE only / excluded (MUTED GREY). NYC "
                      "uses the 5-member EMOS (icon/aifs/gfs/nbm/ecmwf); high-S1 LAX/CHI/MIA and daily-low "
                      "MIA/AUS use the full ~15-source expanded pool; the rest use the core 5. nws_baseline "
                      "is reference-only everywhere. Membership mirrors the authoritative monitor pool "
                      "configs. Paper/backtest config."),
                 legend,
                 html.Div(blocks, className="grid12")], id="city-pool-card")


def _content_forecasts():
    """Forecasts-by-source content (WP-02: absorbed into the Model & Accuracy page). Returns a list."""
    sf = table("source_forecast")
    if sf.empty:
        return [card("No source-forecast snapshot yet "
                     "(snapshot_source_forecasts.py runs in the pipeline; panel fills shortly)."),
                html.Div([html.Div(panel_city_source_attribution(), className="col-12")],
                         className="grid12")]
    members = sf[sf["source"] != "ENSEMBLE_MEAN"].copy()
    ens = sf[sf["source"] == "ENSEMBLE_MEAN"].copy()
    # spread chart: each source a point per city, ensemble a diamond
    fig = go.Figure()
    for base, sub in members.groupby("is_base5"):
        fig.add_scatter(x=sub["city"], y=sub["forecast_f"], mode="markers",
                        name="core 5 members" if base else "expanded pool",
                        marker=dict(size=9, opacity=.8,
                                    color=MINT if base else CYAN,
                                    line=dict(width=1, color="rgba(255,255,255,.25)")))
    if not ens.empty:
        fig.add_scatter(x=ens["city"], y=ens["forecast_f"], mode="markers", name="ENSEMBLE mean",
                        marker=dict(symbol="diamond", size=15, color=AMBER,
                                    line=dict(width=1.5, color="#fff")))
    fig.update_yaxes(title="forecast high (°F)", ticksuffix="°F")
    fig.update_xaxes(title="")
    fig.update_traces(hovertemplate="<b>%{x}</b><br>%{y:.1f}°F<extra></extra>")
    # pivot table city x source
    piv = members.pivot_table(index="city", columns="source", values="forecast_f", aggfunc="first")
    piv = piv.reset_index()
    src_cols = [c for c in piv.columns if c != "city"]
    piv_fmt = piv.copy()
    for c in src_cols:
        piv_fmt[c] = piv_fmt[c].map(_degf)
    piv_fmt = piv_fmt.rename(columns={"city": "City", **{c: _titlecase(c) for c in src_cols}})
    tgt = sf["target_date"].iloc[0] if "target_date" in sf else "—"
    return [card([html.H3(f"Ensemble Members · Target {tgt}"),
                  html.Div("The deployed model is an EMOS ensemble; here are the individual member "
                           "forecasts and their spread per city. Wide spread = high model "
                           "disagreement (a no-trade signal).", className="sub"),
                  graph(_tpl(fig, h=340))]),
            html.Div([html.Div(panel_city_source_attribution(), className="col-12")],
                     className="grid12"),
            card([html.H3("Per-Source Detail (°F)"), dt(piv_fmt, present_df=False, page_size=8)])]


# Coherent edge classification — fixes the bug where a POSITIVE net bar keyed on beats_market
# (Brier) was painted RED "no edge". Now the color encodes BOTH criteria honestly.
_EDGE_CLASS = {
    "both": (MINT, "Beats market AND positive net"),          # the real, trustworthy edge
    "net_only": (AMBER, "Positive net, does NOT beat market"),  # ambiguous — not a clean edge
    "brier_only": (CYAN, "Beats market, net not positive"),    # skill without realized net
    "neither": (RED, "Negative net, does not beat market"),    # genuinely no edge
}


def _edge_class(net, beats):
    pos = net is not None and net == net and net > 0
    bm = beats == 1
    if pos and bm:
        return "both"
    if pos:
        return "net_only"
    if bm:
        return "brier_only"
    return "neither"


def _content_edges_core():
    """Edge panels (WP-02: the core Edges content; the merged Edges & Validation page adds multi-city +
    the daily-low edge + the forward gate board). Returns a list."""
    e = table("edge")
    if e.empty:
        return [card("No edge data yet.")]
    s1 = e[e["stream"] == "S1_S2X"].copy()
    fig = None
    if not s1.empty and s1["avg_net_c"].notna().any():
        s1 = s1.sort_values("avg_net_c", ascending=False)
        s1["klass"] = [_edge_class(n, b) for n, b in zip(s1["avg_net_c"], s1["beats_market"])]
        fig = go.Figure()
        # one trace per class so the legend reads as the explicit criterion
        for klass, (color, label) in _EDGE_CLASS.items():
            sub = s1[s1["klass"] == klass]
            if sub.empty:
                continue
            fig.add_bar(x=sub["city"], y=sub["avg_net_c"], name=label, marker_color=color, width=0.6,
                        customdata=[[b] for b in sub["beats_market"]],
                        hovertemplate="<b>%{x}</b><br>Avg net: %{y:+.2f}c/contract<br>"
                                      "Beats market: %{customdata[0]}<extra></extra>")
        fig.update_layout(barmode="overlay")
        fig.update_yaxes(title="S1 avg net (cents / contract)", ticksuffix="c", tickformat="+.1f")
    caption = ("Color encodes BOTH tests, so nothing above zero looks like flat 'no edge'. "
               "Green = beats the market on Brier AND has positive net (the trustworthy edge). "
               "Amber = positive net but does NOT beat market (ambiguous, not a clean edge). "
               "Cyan = beats market but net is not positive. Red = negative net and no Brier edge. "
               "Net is paper/backtest c/contract after modeled fills; never realized P&L.")
    return [card([html.H3("S1 Edge by City"),
                  html.Div(caption, className="sub", style={"marginBottom": "6px"}),
                  graph(_tpl(fig, h=340))] if fig is not None else
                 [html.H3("S1 Edge by City"), html.Div("S1 net pending more cities.", className="sub")]),
            card([html.H3("Per-City Edge Detail"),
                  dt(present(e, order=["city", "stream", "n", "avg_net_c", "win_rate",
                                       "brier_model", "brier_market", "beats_market", "status"]),
                     present_df=False)]),
            panel_fills_waterfall(),
            html.Div([html.Div(panel_divergence(), className="col-6"),
                      html.Div(panel_decay(), className="col-6")], className="grid12"),
            html.Div([html.Div(panel_edge_success(), className="col-12")], className="grid12"),
            html.Div([html.Div(panel_funnel(), className="col-12")], className="grid12")]


# DEPLOYED STATUS — source of truth for which day-ahead S1 HIGH city models are LIVE in the paper path.
# Canonical: STATUS in src/monitor_multicity_s1.py (LAX/CHI tradable, MIA watch) + NY tradable via the
# base-5 deployed path. AUS/DEN are validated-capable but NOT in the live deployment.
# NOTE: this is a DISPLAY-SIDE map shipped to close an investor-honesty gap (a city that is statistically
# "revived" is not necessarily DEPLOYED — e.g. AUS/MIA near-pass CIs must not read as tradable). It should
# later move to a `status` column on the city_s1 store table (owned by Conduit / build_dashboard_dataset.py).
CITY_S1_STATUS = {"NY": "tradable", "LAX": "tradable", "CHI": "tradable",
                  "MIA": "watch", "AUS": "not-deployed", "DEN": "not-deployed"}
# status -> (bar color, badge kind, display label)
_STATUS_STYLE = {"tradable": (MINT, "good", "TRADABLE"),
                 "watch": (AMBER, "warn", "WATCH"),
                 "not-deployed": (DIM, "neut", "NOT-DEPLOYED"),
                 "not-built": (DIM, "neut", "NOT-BUILT")}


def _city_status(cs1):
    """DEPLOYED status per city. READS the city_s1.status store column when Conduit has added it;
    falls back to the display-side CITY_S1_STATUS map only if the column is absent/blank."""
    if "status" in cs1.columns:
        def norm(c, v):
            v = (str(v).strip().lower() if v is not None else "")
            return v if v in _STATUS_STYLE else CITY_S1_STATUS.get(c, "not-built")
        return [norm(c, v) for c, v in zip(cs1["city"], cs1["status"])]
    return [CITY_S1_STATUS.get(c, "not-built") for c in cs1["city"]]


def _content_multicity():
    """Multi-city S1 validation content (WP-02: absorbed into Edges & Validation). Returns a list."""
    cs1 = table("city_s1")
    blocks = []
    if not cs1.empty:
        d = cs1.copy()
        d["status"] = _city_status(d)
        col_src = "store column" if "status" in cs1.columns else "deploy map (fallback)"
        d = d.sort_values("s1_net_c", ascending=False)
        fig = go.Figure()
        err_plus = (d["ci_hi"] - d["s1_net_c"]).clip(lower=0)
        err_minus = (d["s1_net_c"] - d["ci_lo"]).clip(lower=0)
        colors = [_STATUS_STYLE[s][0] for s in d["status"]]
        fig.add_bar(x=d["city"], y=d["s1_net_c"], marker_color=colors, width=0.62,
                    customdata=[_STATUS_STYLE[s][2] for s in d["status"]],
                    hovertemplate="<b>%{x}</b><br>S1 net: %{y:+.2f}c/contract<br>"
                                  "Status: %{customdata}<extra></extra>",
                    error_y=dict(type="data", array=err_plus, arrayminus=err_minus,
                                 color=DIM, thickness=1.4, width=4))
        fig.update_yaxes(title="S1 net (cents / contract)", ticksuffix="c", tickformat="+.0f")
        # per-city deployed-status badges (the honesty fix: near-pass CIs != tradable)
        order = ["tradable", "watch", "not-deployed", "not-built"]
        # WP-06: season scope is now DATA-DRIVEN from the city_s1.season_scope column (WP-05), replacing the
        # WP-01 _COLD_ONLY_CITIES hardcode.
        scope_by_city = (dict(zip(d["city"], d["season_scope"])) if "season_scope" in d.columns else {})
        status_chips = []
        for st in order:
            cities = [c for c in d["city"] if d.set_index("city").loc[c, "status"] == st]
            if cities:
                _, kind, lbl = _STATUS_STYLE[st]
                city_str = ", ".join((c + " (cold-only)" if (st == "tradable"
                                       and scope_by_city.get(c) == "cold") else c) for c in cities)
                status_chips.append(html.Div([badge(lbl, kind),
                                              html.Span("  " + city_str, className="sub")],
                                             style={"marginRight": "18px"}))
        blocks.append(card([html.H3("Day-Ahead S1 by City — Expanded Per-City Pool"),
                            html.Div(status_chips, style={"display": "flex", "flexWrap": "wrap",
                                                          "gap": "6px 0", "margin": "2px 0 8px"}),
                            html.Div(["Bar color = DEPLOYED status (paper path; source: ", html.B(col_src),
                                     "). TRADABLE = live S1 signal (NY base-5 anchor; LAX/CHI expanded pool). "
                                     "WATCH = logged but not trusted (CI touches zero — e.g. MIA). "
                                     "NOT-DEPLOYED = validated-capable but not in the live deployment "
                                     "(AUS/DEN). A tall bar alone does NOT mean tradable — only the status "
                                     "badge does. All figures paper/forward, never realized P&L."],
                                     className="sub"),
                            graph(_tpl(fig))]))
        tbl = d.copy()
        tbl["status"] = tbl["status"].map(lambda s: _STATUS_STYLE[s][2])
        blocks.append(card([html.H3("Per-City Validation Detail"),
                            dt(present(tbl, drop=["revived"],
                                       order=["city", "status", "s1_net_c", "ci_lo", "ci_hi", "p_gt0",
                                              "rmse_base", "rmse_exp", "trades_per_month"]),
                               present_df=False)]))
    else:
        blocks.append(card("Per-city S1 validation table fills from the latest revival-validate run."))
    # NEW-CITY EXPANSION watchlist. WP-06: now DATA-DRIVEN from the producer `expansion_watchlist` table
    # (WP-05), so the narrative lives in one place (src/build_dashboard_dataset.py) instead of hardcoded here.
    def _wl_row(name, tier, kind, note):
        return html.Div([badge(tier, kind),
                         html.Span(f"  {name}  ", className="sub", style={"fontWeight": "700"}),
                         html.Span(note, className="sub")], style={"marginBottom": "6px"})

    wl = table("expansion_watchlist")
    if not wl.empty:
        def _wl_name(r):
            e = r.get("edge_c")
            suffix = f"  ({r['market']}, {e:+.1f}c)" if not _isnull(e) else f"  ({r['market']})"
            return f"{r['city']}{suffix}"
        wl_rows = [_wl_row(_wl_name(r), r["tier"], r.get("kind") or "neut", r["note"])
                   for _, r in wl.iterrows()]
    else:
        wl_rows = [empty_state("Fills from the expansion_watchlist table (producer).")]
    blocks.append(card(
        [html.H3("New-City Expansion — Candidate Watchlist")]
        + wl_rows
        + [html.Div("Backtest probes + settlement audits; NONE carry real capital. LV/MIN are wired as $0 "
                    "A6 watch accruing forward; the rest are pre-gate. Paper/forward, never realized P&L.",
                    className="sub", style={"marginTop": "8px", "opacity": ".82"})]))
    # WP-01: the "Airport Lock-In Channel" card was removed here -- lock-in was RETIRED 2026-06-25 (latency
    # artifact). Its single retrospective lives on the Capacity & Risk page.
    return blocks


def _content_accuracy():
    """Forecast-accuracy content (WP-02: absorbed into Model & Accuracy). Returns a list."""
    r = table("forecast_rmse")
    if r.empty:
        return [card("No forecast RMSE yet.")]
    m = r.melt(id_vars="city", value_vars=["members_rmse", "s2x_rmse"], var_name="model", value_name="rmse")
    m["model"] = m["model"].map({"members_rmse": "Members-only", "s2x_rmse": "S2X (deployed)"})
    fig = px.bar(m, x="city", y="rmse", color="model", barmode="group",
                 color_discrete_map={"Members-only": DIM, "S2X (deployed)": MINT})
    s = r.melt(id_vars="city", value_vars=["warm", "cold"], var_name="season", value_name="rmse")
    s["season"] = s["season"].map({"warm": "Warm season", "cold": "Cold season"})
    fig2 = px.bar(s, x="city", y="rmse", color="season", barmode="group",
                  color_discrete_map={"Warm season": AMBER, "Cold season": CYAN})
    for f in (fig, fig2):
        f.update_yaxes(title="day-ahead RMSE (°F)", ticksuffix="°F")
        f.update_xaxes(title="")
        f.update_traces(hovertemplate="<b>%{x}</b> · %{fullData.name}<br>%{y:.2f}°F<extra></extra>")
    # WP-01: these cards had NO H3 -> the px title set above was wiped by _tpl(title=None), leaving the two
    # lead Accuracy charts title-less. Give them real card titles (H3 owns the title, never Plotly).
    return [html.Div([html.Div(card([html.H3("Day-Ahead RMSE by City — Members-only vs S2X"),
                                     graph(_tpl(fig))]), style={"flex": "1", "minWidth": "380px"}),
                      html.Div(card([html.H3("Seasonal RMSE — Warm vs Cold"),
                                     graph(_tpl(fig2))]), style={"flex": "1", "minWidth": "380px"})],
                     className="grid"),
            card([html.H3("RMSE Detail (°F)"),
                  dt(present(r, order=["city", "members_rmse", "s2x_rmse", "warm", "cold", "n"]),
                     present_df=False)]),
            html.Div([html.Div(panel_calibration_streams(), className="col-7"),
                      html.Div(panel_emos_skill(), className="col-5")], className="grid12"),
            html.Div([html.Div(panel_pit(), className="col-6"),
                      html.Div(panel_brier_decomp(), className="col-6")], className="grid12"),
            html.Div([html.Div(panel_fan(), className="col-12")], className="grid12"),
            html.Div([html.Div(panel_surprise(), className="col-12")], className="grid12"),
            # WP-02: the permanently-empty lead-decay panel (single-horizon archive) moves into a collapsed
            # drawer so it stops occupying prime space until the multi-lead archive exists.
            html.Details([html.Summary("Planned diagnostics (not yet buildable)",
                                       style={"cursor": "pointer", "color": DIM, "fontSize": "12px",
                                              "margin": "6px 0"}),
                          html.Div([html.Div(panel_lead_decay(), className="col-12")], className="grid12")])]


def _content_forward():
    """Forward-validation gate board (WP-02: absorbed into Edges & Validation). Reads the SAME run_gates
    table the $1,000 Run board uses so the pages agree by construction. Returns a list."""
    g = table("run_gates")
    if g.empty:
        return [card([html.H3("Pre-registered forward gates"),
                      empty_state("Fills from the $1,000 staged-harness ledger + monitor logs.")])]
    bars = []
    for _, r in g.iterrows():
        nset = int(r["n_settled"] or 0); nreq = int(r["n_required"] or 1)
        pct = min(100, int(100 * nset / max(nreq, 1)))
        kind, col, lbl = _GATE_STATUS_STYLE.get(r["status"], ("neut", NEUTRAL, str(r["status"]).upper()))
        bar_col = "var(--accent)" if kind == "good" else ("var(--amber)" if kind == "warn" else "var(--neutral)")
        cp = f" · changepoint {r['changepoint']}" if r.get("changepoint") else ""
        edge = "—" if _isnull(r["edge_c"]) else f"{r['edge_c']:+.2f}c"
        cil = "—" if _isnull(r["ci_lo"]) else f"{r['ci_lo']:+.1f}"
        cih = "—" if _isnull(r["ci_hi"]) else f"{r['ci_hi']:+.1f}"
        bars.append(html.Div([
            html.Div([html.B(r["edge_label"]), badge(lbl, kind)],
                     style={"display": "flex", "justifyContent": "space-between", "alignItems": "center"}),
            html.Div(r["gate_desc"], className="sub", style={"fontSize": "11.5px", "margin": "3px 0 2px"}),
            html.Div([html.Span(f"edge {edge}  CI [{cil}, {cih}]c", className="sub",
                                style={"fontSize": "11px"}),
                      html.Span(f"   {nset}/{nreq} settled{cp}", className="sub",
                                style={"fontSize": "11px", "color": DIM})]),
            html.Div(html.Div(className="bar-fill", style={"width": f"{pct}%", "background": bar_col}),
                     className="bar-track", style={"margin": "5px 0 14px"})]))
    return [card([html.H3("Pre-registered forward gates"),
                  # WP-01: the long enumerated gate list drifted (missing A6 LV/MIN, A8/FDR). The board below
                  # IS the live gate state (same run_gates table as the $1k page), so it speaks for itself.
                  html.Div(["Thresholds are fixed in advance in docs/FORWARD_PROTOCOL.md. The board "
                            "below is the live gate state — the SAME run_gates table as the $1,000 "
                            "Run board, so the two pages agree by construction. Every row is still "
                            "ACCUMULATING settled signals; none is a proven live edge yet, and REAL "
                            "capital moves only on a gate PASS. Paper/forward, never realized P&L."],
                           className="sub"),
                  html.Div(bars, style={"marginTop": "12px"})])]


def _content_scalability():
    """Scalability content (Mosaic -> Iris): per-stream fill-cost curve + bankroll headroom = the honest
    'more money != linearly more profit' story. WP-02: absorbed into Capacity & Risk. Returns a list."""
    cap = table("fill_capacity")
    # headline strip: real-curve count vs accruing count (audit-corrected; no more 'dead-book gaps')
    n_real = int((cap["real_curve"] == True).sum()) if (not cap.empty and "real_curve" in cap) else 0  # noqa: E712
    n_accr = int((cap["depth_state"] == "accruing").sum()) if (not cap.empty and "depth_state" in cap) else 0
    live_note = (html.Div(["⟳ Live from the curated fill tables — ", html.B(_scal_data_asof())],
                          className="sub", style={"fontSize": "11px", "opacity": .85})
                 if _scal_data_asof() else html.Div())
    intro = panel(
        "Scalability — a Fills Problem, and an Honesty Problem",
        [live_note],
        caption=(f"Contract counts are per-market (per-strike, per-day). All three deployed high cities now "
                 f"have real fill data; the rest are accruing forward. {n_real} real-curve, {n_accr} "
                 f"accruing."),
        drawer=html.Div(["Each edge sits on a finite order book, and contract counts are ", html.B("per-market "
                  "(per-strike, per-day)"), ". All ", html.B("three deployed high cities"), " now have real "
                  "fill data: ", html.B("NY-high"), " from its median LOCK-MOMENT signal book, and ",
                  html.B("LAX-high + CHI-high"), " from a 9-day periodic STANDING-book depth archive — "
                  "99.9–100% fillable to 250ct/market at sub-1c slippage (badged STANDING because these are "
                  "periodic snapshots, NOT decision/lock-moment fills). The remaining streams (the daily-LOW "
                  "books + MIA-high watch + the lock-in stream) are NOT in that archive; their full slippage "
                  "curve is ", html.B("accruing forward"), ", so we show '≥25ct confirmed' and NO fabricated "
                  "ceiling. Degenerate cent-floor longshot 'ceilings' (e.g. the old 174k/27k figures) stay "
                  "removed as tick-floor artifacts. ", html.B("Paper / public-data orderbook reads — no "
                  "auth, no orders, never realized P&L.")]))
    return [html.Div([html.Div(intro, className="col-12")], className="grid12"),
            html.Div([html.Div(panel_scalability_curve(), className="col-12")], className="grid12"),
            html.Div([html.Div(panel_scalability_headroom(), className="col-12")], className="grid12")]


def _content_sandbox():
    """Interactive risk/return sandbox content (WP-02: absorbed into the Lab page). Returns a list. All the
    sandbox input/output IDs are unchanged, so its callbacks are unaffected."""
    def field(id_, label, val, step="any", mn=None, mx=None):
        kw = {"id": id_, "type": "number", "value": val, "step": step}
        if mn is not None:
            kw["min"] = mn
        if mx is not None:
            kw["max"] = mx
        return html.Div([html.Label(label), dcc.Input(**kw)], className="sb-field")

    # COMPACT field + bucket (2026-06-25 UX): the season-classed inputs were ~16 stacked fields = a very tall
    # column. Group each stream class into a small "bucket" tile with its 2-3 numeric inputs on ONE row, and
    # lay the buckets out in a responsive auto-fit grid -> ~3x shorter, same IDs/behaviour.
    def cf(id_, label, val, step, mn, mx):
        return html.Div([
            html.Label(label, style={"fontSize": "9.5px", "color": DIM, "fontWeight": "600",
                                     "display": "block", "marginBottom": "2px", "whiteSpace": "nowrap"}),
            dcc.Input(id=id_, type="number", value=val, step=step, min=mn, max=mx,
                      style={"width": "100%", "padding": "5px 7px", "fontSize": "13px"})],
            style={"flex": "1", "minWidth": "58px"})

    def bucket(title, color, caption, *fields):
        return html.Div([
            html.Div(title, style={"color": color, "fontWeight": "700", "fontSize": "11.5px",
                                   "letterSpacing": ".2px"}),
            html.Div(caption, className="sub",
                     style={"fontSize": "9.5px", "opacity": .72, "lineHeight": "1.3", "margin": "1px 0 7px"}),
            html.Div(list(fields), style={"display": "flex", "gap": "7px", "flexWrap": "wrap"})],
            style={"border": "1px solid var(--line)", "borderRadius": "10px", "padding": "9px 11px",
                   "background": "color-mix(in srgb, var(--panel) 55%, transparent)"})

    # ---- column 1: edge & flow inputs ----
    # DEFAULTS reproduce the DEPLOYED $1,000 activated book (3 high-S1 NY/LAX/CHI + NY-low all-season + the 4
    # warm daily-low AUS/LAX/DEN/MIA). The stream count + staked $ + median are read LIVE from run_meta /
    # run_projection (DYNAMIC -- never goes stale when the book changes, e.g. NY-low added 2026-06-22 took it
    # 7->8 streams / $30.29->$35.95). Lock-in is NOT in the activated book (deprioritized speed race) -> 0
    # locks/mo default. SANDBOX_CT_CAL anchors the default scenario to the live deployed median (~+14.63%/m).
    _sb_nact = _run_meta("n_active_paper_streams", "8")
    _sb_stake = _run_meta("active_paper_allocation_dollars", "35.95")
    _sb_rp = table("run_projection")
    _sb_med = (float(_sb_rp["mc_median_mo"].dropna().iloc[0])
               if (not _sb_rp.empty and "mc_median_mo" in _sb_rp and _sb_rp["mc_median_mo"].notna().any())
               else None)
    _sb_med_str = f"~+{100 * _sb_med:.2f}%/m" if _sb_med is not None else "the live Kelly-MC median"
    edge_inputs = card([html.H3("Edges & Flow"),
        html.Div([f"Streams split by SEASON CLASS for both high & low. YEAR-ROUND defaults reproduce the live "
                  f"deployed book ({_sb_nact} streams, ${_sb_stake} @ 0.50x Kelly, anchored to the Kelly-MC "
                  f"median ", html.B(_sb_med_str), f"); COLD-ONLY adds the validated cold streams (LV/MIN high; "
                  f"PHIL/PHX low — $0 in warm today), so the full-book default sits above the warm-season "
                  f"number. Zero the cold cities for the warm-only book; every field moves the result."],
                 className="sub", style={"margin": "0 0 10px", "fontSize": "11px"}),
        html.Div([
            bucket("HIGH · year-round", MINT, "NY/LAX/CHI all-season (~4.9c gross; ~2.9c net)",
                   cf("sb-s1edge", "Edge ¢/ct", S1_HIGH_EDGE_DEFAULT, 0.1, 0, 20),
                   cf("sb-cities", "Cities", 3, 1, 0, 7),
                   cf("sb-s1trades", "Trades/mo·city", 84, 1, 0, 400)),
            bucket("HIGH · cold-only", CYAN, "LV +9.19c / MIN +9.89c · Nov–Apr · A6 watch ($0 warm)",
                   cf("sb-s1edge-cold", "Edge ¢/ct", S1_HIGH_COLD_EDGE_DEFAULT, 0.1, 0, 20),
                   cf("sb-cities-cold", "Cities", 2, 1, 0, 7),
                   cf("sb-s1trades-cold", "Trades/mo·city", 84, 1, 0, 400)),
            bucket("LOW · all-season / active", MINT, "NY-low + warm AUS/LAX/DEN/MIA (~7.6c) — trading now",
                   cf("sb-lowedge", "Edge ¢/ct", LOW_EDGE_DEFAULT, 0.1, 0, 20),
                   cf("sb-lowcities", "Cities", 5, 1, 0, 7),
                   cf("sb-lowtrades", "Trades/mo·city", 82, 1, 0, 400)),
            bucket("LOW · cold-only", CYAN, "PHIL +11.79c / PHX +17.65c · ~20/mo · Nov–Apr ($0 warm)",
                   cf("sb-lowedge-cold", "Edge ¢/ct", LOW_COLD_EDGE_DEFAULT, 0.1, 0, 20),
                   cf("sb-lowcities-cold", "Cities", 2, 1, 0, 7),
                   cf("sb-lowtrades-cold", "Trades/mo·city", 20, 1, 0, 400)),
            bucket("Lock-in (RETIRED 2026-06-25)", DIM, "latency artifact · 0 locks = not in deployed book",
                   cf("sb-lock", "Edge ¢/ct", 12, 0.5, 0, 30),
                   cf("sb-lockpm", "Locks/mo·city", 0, 1, 0, 120))],
            style={"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(192px, 1fr))",
                   "gap": "10px"})])

    # ---- column 2: capital, risk profile, frictions ----
    cap_inputs = card([html.H3("Capital & Risk Profile"),
        field("sb-bankroll", "Bankroll ($)", 1000, 100, 100, 1_000_000),
        html.Div("Kelly fraction (risk profile)", className="sb-field-lbl u-label",
                 style={"margin": "12px 0 6px"}),
        dcc.Slider(id="sb-kelly", min=0.25, max=1.00, step=0.05, value=0.50,
                   marks={0.25: "0.25", 0.50: "0.50x dep", 0.75: "0.75", 1.00: "1.0x full"},
                   tooltip={"placement": "bottom", "always_visible": False},
                   updatemode="mouseup"),
        html.Div("Full Kelly (1.0x) reaches the highest validated return (+29.7%/m) but the worst "
                 "stress drawdown (~41%). 0.50x is the recommended ceiling — beyond it, ruin risk "
                 "climbs steeply. No ROI cap; the warnings escalate honestly.", className="sub",
                 style={"margin": "8px 0 2px", "fontSize": "11px"}),
        html.Div(style={"height": "10px"}),
        field("sb-slip", "Base fill cost — slippage + fee (c/contract)", 2.0, 0.5, 0, 4),
        # ITEM 6: slippage-vs-bankroll mode. DEFAULT = 'off' (base behavior, no bankroll dependence).
        html.Div("Slippage model", className="sb-field-lbl u-label", style={"margin": "12px 0 6px"}),
        dcc.RadioItems(id="sb-slip-mode",
                       options=[{"label": " Off (base — fixed)", "value": "off"},
                                {"label": " Auto-scale with bankroll", "value": "auto"},
                                {"label": " Manual override", "value": "manual"}],
                       value="off", className="sb-radio",
                       labelStyle={"display": "block", "fontSize": "12px", "margin": "2px 0"}),
        field("sb-slip-manual", "Manual slippage (c/contract, manual mode)", 4.0, 0.5, 0, 40),
        html.Div(["The base fill cost (slippage + fee) is what the gross backtest edge loses to reach the "
                  "deployed net (~2c, matching the activated-book net_opt = mean_c − 2c). Off = fixed at that "
                  "value (depth still capped by each stream's measured book). Auto = it RISES with order size "
                  "as bankroll grows, along Mosaic's per-stream slippage(size) curve. Manual = force a single "
                  "flat value. Win rate is NOT a user lever — it is already embedded in each stream's edge."],
                 className="sub",
                 style={"marginTop": "8px", "fontSize": "11px"})],
        style={"flex": "1", "minWidth": "270px"})

    # ---- column 3: headline result ----
    out = card([html.H3("Projected Monthly Result"),
                html.Div("Estimated monthly profit (this scenario)", className="sub"),
                html.Div(id="sb-profit", className="sb-out", style={"color": MINT}),
                html.Div("Monthly ROI on bankroll", className="sub", style={"marginTop": "10px"}),
                html.Div(id="sb-roi", className="sb-out"),
                html.Div(id="sb-kelly-band", style={"marginTop": "14px"}),
                html.Div(id="sb-note", className="sub", style={"marginTop": "12px"})],
               style={"flex": "1.1", "minWidth": "300px"})

    risk_panel = card([html.H3("Risk Profile — Kelly Stake Sweep"),
        html.Div(["At the selected Kelly fraction, the real numbers from the $1,000 correlation-aware "
                  "Monte-Carlo stake sweep (interpolated between rows). Severity is color-coded: ",
                  html.Span("green = ok", style={"color": MINT, "fontWeight": "700"}), ", ",
                  html.Span("amber = elevated", style={"color": AMBER, "fontWeight": "700"}), ", ",
                  html.Span("red = ruin-risk", style={"color": RED, "fontWeight": "700"}),
                  ". 0.50x recommended ceiling; the slider goes to full Kelly (1.0x) with escalating "
                  "warnings — no ROI cap."], className="sub", style={"marginBottom": "10px"}),
        html.Div(id="sb-risk-metrics")],
        style={"flex": "1"})

    charts = card([html.H3("Projected Equity Fan — 12-Month Monte-Carlo"),
        html.Div("Median path with p5/p95 bands. Capacity-aware: the depth ceiling is an absolute $/mo, so as "
                 "equity compounds the % return tightens toward the plateau (paths can't grow unbounded through "
                 "the ceiling). A model, not a forecast.", className="sub"),
        dcc.Graph(id="sb-fan", config={"displayModeBar": False})])
    chart_rr = card([html.H3("Risk vs Return Across Kelly Fractions"),
        html.Div("Median monthly return vs p95 max-drawdown for each Kelly fraction; your current pick is "
                 "highlighted. The curve bends sharply right past 0.50x.", className="sub"),
        dcc.Graph(id="sb-rr", config={"displayModeBar": False})])
    chart_dist = card([html.H3("Monthly Return Distribution + Drawdown Gauge"),
        html.Div("Modeled monthly-return spread (p5 / median / p95) and the p95 max-drawdown dial for the "
                 "selected fraction.", className="sub"),
        dcc.Graph(id="sb-dist", config={"displayModeBar": False})])
    chart_break = card([html.H3("Profit Breakdown by Stream"),
        dcc.Graph(id="sb-chart", config={"displayModeBar": False})])
    # ITEM 7 (AUDIT-CORRECTED): Kelly fraction vs P(maxDD>=25%) and P(ruin=DD>=50%). The old P(month<0) line
    # was dropped -- it is invariant to the Kelly fraction (scaling all bets by f scales mean & std equally,
    # so the sign probability never moves). Drawdown thresholds DO respond to the lever.
    chart_ruin = card([html.H3(["Drawdown & Ruin Risk vs Kelly Fraction  ", info_dot(
            "Sweeps the Kelly fraction through the SAME Monte-Carlo engine that drives the equity fan. "
            "P(max drawdown ≥ 25%) = chance equity falls at least 25% peak-to-trough within 12 months; "
            "P(ruin) = the same at the ≥50% threshold. Both climb with the fraction — this is the risk "
            "behind picking 0.25x / 0.35x / 0.50x. (We dropped P(month<0): scaling every bet by the Kelly "
            "fraction scales the monthly mean AND std together, so the sign probability is invariant to the "
            "lever — it told you nothing about leverage. Drawdown depth does.) Paper model, never realized P&L.")]),
        html.Div(["Both drawdown probabilities climb as you lever up — P(ruin) is ~0 below 0.50x and rises "
                  "sharply through full Kelly, while P(max drawdown ≥ 25%) crosses meaningful odds earlier. "
                  "This replaces the old P(month<0) line, which was invariant to the Kelly fraction (scaling "
                  "every bet by f scales the monthly mean and std equally, so the sign probability never "
                  "moves). Your current Kelly pick is marked; the 0.50x ceiling and full-Kelly (1.0x) zone "
                  "are shaded. Paper model, never realized P&L."], className="sub"),
        dcc.Graph(id="sb-ruin", config={"displayModeBar": False})])
    asof_note = _scal_data_asof()
    chart_cap = card([html.H3(["Capacity Ceiling vs Bankroll  ", info_dot(
            "Real markets have finite depth. As order size grows, VWAP slippage climbs along each book's "
            "measured curve (Mosaic), so the net edge per contract degrades and crosses zero at that book's "
            "capacity ceiling. Each book saturates at its OWN size, so the plateau is a STAIRCASE: the shallow "
            "daily-low books cap first (faint marker), then the deep high books. The amber line marks where "
            "the LAST book saturates — beyond it, extra bankroll adds ~$0/mo.")]),
        # LIVE-DATA note (Task B): the scaling curves read from the materialized fill tables, so this moves as
        # the depth archive + forward fill_curve logging grow. NOT a hardcoded date.
        (html.Div(["⟳ Live from the curated fill tables — ", html.B(asof_note),
                   ". Curves update as the depth archive + forward fill logging grow."],
                  className="sub", style={"fontSize": "11px", "marginBottom": "6px", "opacity": .85})
         if asof_note else html.Div()),
        html.Div(id="sb-cap-flag", style={"marginBottom": "8px"}),
        html.Div(["Monthly paper profit as bankroll grows: it rises with capital UNTIL real fill-depth caps "
                  "it, then flattens — modeled on each book's non-linear slippage curve (see the ",
                  html.B("Scalability"), " page). Books drop out a GROUP at a time (shallow daily-low books "
                  "first, then the deep high books), so the green curve is a staircase. The ",
                  html.B("amber line", style={"color": AMBER}), " is where the last book saturates — beyond it "
                  "even a $100M bankroll returns the same capped dollars. Paper model; model edge held fixed "
                  "across sizes."],
                 className="sub"),
        dcc.Graph(id="sb-cap", config={"displayModeBar": False})])

    disclaimer = card([html.Div("How this is computed", className="u-label", style={"marginBottom": "6px"}),
        html.Div(["The profit breakdown is a transparent parametric model: per-stream net edge (c/contract) "
                  "— which DEGRADES with order size along each stream's measured VWAP-slippage curve (Mosaic), "
                  "binding at the real per-stream capacity ceiling — × trades/month × stake (Kelly-fraction × "
                  "bankroll). The risk band, equity fan, risk/return curve, and drawdown gauge are read from "
                  "(and interpolated within) the $1,000 correlation-aware Monte-Carlo stake sweep — every "
                  "number is sized at the edge CI lower bound. ",
                  html.B("Paper / backtest estimate — NOT a guarantee, never realized P&L."), " Staged math: "
                  "today LIVE capital = $0 until the pre-registered forward gates PASS; this lab shows the "
                  "what-if if/when they do."], className="sub")],
        style={"borderColor": "color-mix(in srgb, var(--amber) 40%, transparent)"})

    # NEW (2026-06-25): TIME-TO-TARGET simulator under the edge inputs. Set a profit goal -> the median time
    # (years/months/days) to reach it, with the depth/liquidity ceiling applied each month, + the equity path.
    ttt_card = card([html.H3(["Time to Target Profit  ", info_dot(
            "Set a profit goal; the lab simulates thousands of forward paths under THIS scenario and reports "
            "the MEDIAN time to reach it. Fill sizes / liquidity are taken into account: the depth ceiling is "
            "an absolute $/mo, so as equity compounds the dollars added per month plateau at capacity and the "
            "growth (and the countdown) slows. The chart is the median equity path with its p5–p95 band climbing "
            "to the target. Paper model, never realized P&L.")]),
        html.Div("Set a profit goal — get the median time to reach it, with order-book depth/liquidity applied "
                 "every month (the $/mo added plateaus at capacity, so growth slows as the bankroll scales).",
                 className="sub"),
        html.Div([
            html.Div([html.Label("Target profit ($)", className="u-label",
                                 style={"fontSize": "10px", "display": "block", "marginBottom": "3px"}),
                      dcc.Input(id="sb-target", type="number", value=10000, step=100, min=100, max=10_000_000,
                                style={"width": "150px", "padding": "6px 9px", "fontSize": "14px"})]),
            html.Div(id="sb-ttt-result", style={"flex": "1", "minWidth": "180px"})],
            style={"display": "flex", "alignItems": "flex-end", "gap": "18px", "margin": "10px 0 8px",
                   "flexWrap": "wrap"}),
        dcc.Graph(id="sb-ttt-chart", config={"displayModeBar": False})])

    return [
        html.Div("Tune the edges, capital, and risk profile. Every output is a transparent paper model "
                 "(see the note at the bottom) — research only, never realized P&L.", className="sub",
                 style={"marginBottom": "10px"}),
        html.Div([html.Div([html.Div([edge_inputs, ttt_card],
                                      style={"display": "flex", "flexDirection": "column", "gap": "14px"})],
                            className="col-8"),
                  html.Div([html.Div([out, cap_inputs],
                                     style={"display": "flex", "flexDirection": "column", "gap": "14px"})],
                           className="col-4")], className="grid12"),
        html.Div([html.Div(risk_panel, className="col-12")], className="grid12"),
        html.Div([html.Div(charts, className="col-6"), html.Div(chart_rr, className="col-6")],
                 className="grid12"),
        html.Div([html.Div(chart_dist, className="col-6"), html.Div(chart_break, className="col-6")],
                 className="grid12"),
        html.Div([html.Div(chart_ruin, className="col-12")], className="grid12"),
        html.Div([html.Div(chart_cap, className="col-12")], className="grid12"),
        html.Div([html.Div(disclaimer, className="col-12")], className="grid12")]


def _content_risk():
    """Risk & honesty content (WP-02: absorbed into Capacity & Risk). Returns a list."""
    items = [("Capacity ceiling", "Each edge is depth-capacity-bounded; absolute $ per city has a ceiling that "
              "does NOT grow with bankroll. Scale comes from MORE validated cities, not more capital per city.",
              "warn"),
             ("Fills are the gating unknown", "Edges modeled at ≤~1c slippage; worse real fills shrink them. "
              "Forward fill validation is in progress.", "warn"),
             ("Edges are thin", "Validated multi-city S1 nets are ~3–5c/contract with bootstrap CIs whose "
              "lower bound is small — real but fragile. Sized conservatively.", "warn"),
             ("Paper only", "No authentication, no orders, no account, no real money — anywhere. Every figure "
              "is a paper/backtest/forward estimate, never realized P&L.", "bad"),
             ("Lock-in reality", "NYC lock-in was RETIRED (2026-06-25) as a latency artifact at the ~128s METAR "
              "floor (no faster KNYC feed exists). Airport-city lock-in is a thin speed race, not a fat edge.",
              "neut")]
    return [html.Div([panel(t, [], badges=[badge(k.upper(), k)], caption=d, cls="stack")
                      for t, d, k in items]),
            html.Div([html.Div(panel_latency(), className="col-12")], className="grid12")]


# ============================== MERGED PAGES (WP-02: 13 -> 8) ==============================
def render_model():
    """Model & Accuracy = the old Forecasts + Forecast-Accuracy pages, one canonical home for every
    forecast/calibration panel (calibration deck, PIT, fan, surprise, EMOS skill, model compare)."""
    return html.Div([section("Model & Accuracy — Forecast Skill & Calibration")]
                    + _content_forecasts()
                    + _content_accuracy()
                    + [html.Div([html.Div(panel_model_compare(), className="col-12")], className="grid12")])


def render_edges():
    """Edges & Validation = the old Edges + Multi-City + Forward-Validation pages."""
    return html.Div(
        [section("Edges & Validation — Per-City Signal & Pre-Registered Gates")]
        + _content_edges_core()
        + [html.Div([html.Div(panel_dailylow_edge(), className="col-12")], className="grid12"),
           html.H3("Multi-City S1", style={"margin": "14px 0 4px"})]
        + _content_multicity()
        + [html.H3("Forward Validation", style={"margin": "14px 0 4px"})]
        + _content_forward())


def render_capacity():
    """Capacity & Risk = the old Scalability + Risk & Honesty pages."""
    return html.Div([section("Capacity & Risk — Fill-Size vs Net Edge, and the Honest Caveats")]
                    + _content_scalability()
                    + [html.H3("Risk & Honesty", style={"margin": "14px 0 4px"})]
                    + _content_risk())


# WP-06: per-stream forward-net visualization off the WP-05 equity_curve_stream / monthly_returns_stream
# tables, driven by the multi-city scope bar. Stream hues mirror the tokens.css --stream-* set.
_STREAM_HUES = {
    "NY_high": "#00e08a", "LAX_high": "#36c5f0", "CHI_high": "#d9a23a", "MIA_high": "#5fd0c0",
    "LV_high": "#b0e057", "MIN_high": "#57b0e0",
    "NY_low": "#9ad04a", "AUS_low": "#b07ff0", "LAX_low": "#ff8a5b", "DEN_low": "#7fd6a0",
    "MIA_low": "#e85f8a", "CHI_low": "#e0c04a", "PHIL_low": "#8a9ff0", "PHX_low": "#f0a04a",
}
# the deployed $1k book order (high cities, then low), so the scope bar reads deployed-first
_STREAM_ORDER = ["NY_high", "LAX_high", "CHI_high", "NY_low", "AUS_low", "LAX_low", "DEN_low", "MIA_low"]


def _stream_color(s):
    return _STREAM_HUES.get(s, NEUTRAL)


def _stream_label2(s):
    parts = str(s).rsplit("_", 1)
    return f"{parts[0]} · {parts[1]}" if len(parts) == 2 else str(s)


def _stream_scopes():
    es = table("equity_curve_stream")
    if es.empty or "stream" not in es.columns:
        return []
    present = list(dict.fromkeys(es["stream"]))
    return [s for s in _STREAM_ORDER if s in present] + [s for s in present if s not in _STREAM_ORDER]


def _stream_forward_view(scope):
    """The chart body for the per-stream forward-net section. scope='ALL' -> overlay of every stream's
    cumulative net + a summary table; a single stream id -> that stream's equity line + monthly bars."""
    es = table("equity_curve_stream")
    if es.empty:
        return empty_state("Fills from the per-stream forward-signal logs (equity_curve_stream).")
    es = es.copy()
    scopes = _stream_scopes()
    if scope == "ALL":
        fig = go.Figure()
        rowsum = []
        for s in scopes:
            d = es[es["stream"] == s].sort_values("date")
            if d.empty:
                continue
            fig.add_scatter(x=d["date"], y=d["equity_c"], mode="lines", name=_stream_label2(s),
                            line=dict(color=_stream_color(s), width=2, shape="linear"),
                            hovertemplate=_stream_label2(s) + "<br>%{x}<br>cum %{y:+,.0f}c<extra></extra>")
            rowsum.append({"stream": _stream_label2(s), "signals": int(d["trades"].sum()),
                           "net_c": round(float(d["equity_c"].iloc[-1]), 1)})
        fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
        fig.update_yaxes(title="cumulative forward net (c / contract)", ticksuffix="c", tickformat="+,.0f")
        fig.update_xaxes(title="", nticks=8)
        tbl = pd.DataFrame(rowsum).sort_values("net_c", ascending=False) if rowsum else pd.DataFrame()
        return html.Div([graph(_tpl(fig, h=340, legend=True)),
                         (pro_table(present(tbl, rename={"net_c": "Cum Net", "signals": "Signals"},
                                            fmt={"net_c": _cents1}),
                                    present_df=False, align_left=("Stream",)) if not tbl.empty else html.Div())])
    # single stream
    d = es[es["stream"] == scope].sort_values("date")
    if d.empty:
        return empty_state(f"No settled forward signals for {_stream_label2(scope)} yet.")
    col = _stream_color(scope)
    eqf = go.Figure()
    eqf.add_scatter(x=d["date"], y=d["equity_c"], mode="lines", name="cum net",
                    line=dict(color=col, width=2.4), fill="tozeroy",
                    fillcolor=f"rgba({_rgb(col)},.10)",
                    hovertemplate="%{x}<br>cum %{y:+,.0f}c<extra></extra>")
    eqf.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
    eqf.update_yaxes(title="cumulative forward net (c/ct)", ticksuffix="c", tickformat="+,.0f")
    eqf.update_xaxes(title="", nticks=8)
    mr = table("monthly_returns_stream")
    mfig_el = html.Div()
    if not mr.empty:
        md = mr[mr["stream"] == scope].sort_values("month")
        if not md.empty:
            colors = [GREEN if v >= 0 else RED for v in md["net_c"]]
            mfig = go.Figure()
            mfig.add_bar(x=md["month"], y=md["net_c"], marker_color=colors, width=0.7,
                         hovertemplate="%{x}<br>%{y:+.0f}c · %{customdata} signals<extra></extra>",
                         customdata=md["trades"])
            mfig.add_hline(y=0, line=dict(color=AXISCOL, width=1))
            mfig.update_yaxes(title="monthly net (c/ct)", ticksuffix="c", tickformat="+,.0f")
            mfig.update_xaxes(title="")
            mfig_el = html.Div([html.Div("Monthly forward net", className="sub",
                                         style={"margin": "8px 0 2px"}),
                                graph(_tpl(mfig, h=240, legend=False))])
    return html.Div([graph(_tpl(eqf, h=300, legend=False)), mfig_el])


def panel_stream_forward():
    """Per-stream FORWARD paper net (cents/contract), scoped by the multi-city scope bar. Source: the WP-05
    equity_curve_stream / monthly_returns_stream tables (settled forward signals). Distinct from the NY
    walk-forward BACKTEST curve below — this is the deployed streams' FORWARD track. Paper, never realized."""
    scopes = _stream_scopes()
    if not scopes:
        return card([html.H3("Per-Stream Forward Net"),
                     empty_state("Fills from the per-stream forward-signal logs (equity_curve_stream).")])
    return card([html.H3("Per-Stream Forward Net — Deployed Streams"),
                 _cap("Cumulative FORWARD paper net per deployed stream from settled forward signals "
                      "(distinct from the NY walk-forward backtest below). Pick ALL for the overlay + a "
                      "summary, or a single stream for its equity line and monthly bars. Paper/forward, "
                      "never realized P&L."),
                 scope_bar(scopes, active="ALL"),
                 html.Div(_stream_forward_view("ALL"), id="stream-fwd-charts")],
                id="stream-forward-card")


def render_lab():
    """Lab = the interactive Sandbox + per-stream forward net + the leak-free backtest research panels."""
    return html.Div([section("Lab — Interactive Sandbox & Backtest Research")]
                    + _content_sandbox()
                    + [html.H3("Per-Stream Forward Net", style={"margin": "16px 0 4px"}),
                       html.Div([html.Div(panel_stream_forward(), className="col-12")], className="grid12"),
                       html.H3("Backtest Research", style={"margin": "16px 0 4px"}),
                       html.Div("Leak-free walk-forward backtest diagnostics in cents/contract or °F — "
                                "NOT dollars, NOT live, never realized P&L.", className="sub",
                                style={"marginBottom": "10px"}),
                       html.Div([html.Div(panel_equity_curve(), className="col-8"),
                                 html.Div(panel_drawdown(), className="col-4")], className="grid12"),
                       html.Div([html.Div(panel_monthly_returns(), className="col-6"),
                                 html.Div(panel_scenario(), className="col-6")], className="grid12")])


def render_methodology():
    m = table("methodology")
    # WP-02: the per-stream calibration deck is canonical on Model & Accuracy now (dropped here).
    return html.Div([section("Methodology & Provenance"),
                     card(dt(m, page_size=20) if not m.empty else html.Div("—", className="sub"))])


RENDER = {"overview": render_overview, "bankroll": render_bankroll, "markets": render_markets,
          "model": render_model, "edges": render_edges, "capacity": render_capacity,
          "lab": render_lab, "methodology": render_methodology}

# ============================== APP ==============================
app = Dash(__name__, title="AeroAlpha — Investor View", update_title=None,
           suppress_callback_exceptions=True)
server = app.server

_users = {}
for pair in os.environ.get("DASH_USERS", "investor:7241").split(","):
    if ":" in pair:
        u, p = pair.split(":", 1)
        _users[u.strip()] = p.strip()
dash_auth.BasicAuth(app, _users)


def topbar():
    return html.Div(className="topbar", children=[
        html.Div([html.Span("Aero", className="a1"), html.Span("Alpha")], className="brand"),
        html.Span([html.Span(className="dot"), "LIVE DATA"], className="pill live",
                  title="Public-data pipeline is live. This is NOT live trading."),
        html.Span("PAPER ONLY — no orders, no real money", className="pill paper"),
        html.Div(style={"flex": "1"}),
        html.Div(id="tb-tickers", style={"display": "flex", "gap": "20px"}),
        html.Span(id="tb-stale", children=staleness_chip()),
        html.Span("Dark", id="theme-toggle", className="pill theme-toggle", n_clicks=0,
                  title="Toggle light / dark", style={"cursor": "pointer"}),
        html.Span(id="tb-clock", className="mono", style={"color": DIM, "fontSize": "13px"})])


def _system_status():
    """Footer verdict DERIVED from the integrity sentinel's alerts (WP-01: was a hardcoded 'ALL SYSTEMS
    NOMINAL' that lied when the sentinel was CRITICAL). CRITICAL -> degraded/red; HIGH -> attention/amber;
    else nominal/green. Always paper-honest."""
    a = table("alerts")
    sev = set(str(s).upper() for s in a["severity"]) if (not a.empty and "severity" in a) else set()
    if "CRITICAL" in sev:
        return html.Span("DEGRADED — see Alerts · paper / backtest / forward, never realized P&L",
                         className="sb-item sb-bad")
    if "HIGH" in sev:
        return html.Span("ATTENTION — see Alerts · paper / backtest / forward, never realized P&L",
                         className="sb-item sb-warn")
    return html.Span("ALL SYSTEMS NOMINAL — paper / backtest / forward, never realized P&L",
                     className="sb-item sb-ok")


def paper_banner():
    """WP-03: one persistent honesty banner under the topbar. Because it's always on screen, the repeated
    'never realized P&L' tail can come off individual one-line captions (full caveats stay in each card)."""
    return html.Div([html.Span("PAPER RESEARCH", className="pb-tag"),
                     html.Span(" — simulated results on public data. No orders, no account, no real money, "
                               "never realized P&L.", className="pb-text")],
                    className="paper-banner")


def statusbar():
    return html.Div(className="statusbar", children=[
        html.Span([html.Span(className="dot"), "DATA STREAM"], className="sb-item"),
        html.Span(id="status-updated", className="sb-item mono"),
        html.Span("MODE: PAPER", className="sb-item sb-paper"),
        html.Div(style={"flex": "1"}),
        html.Span(_system_status(), id="status-verdict")])


_NAV_ICON = {"overview": "overview", "bankroll": "run", "markets": "markets", "model": "model",
             "edges": "edges", "capacity": "capacity", "lab": "lab", "methodology": "methodology"}


def sidebar():
    # WP-02: real links (dcc.Link -> <a href>). Deep-linkable + browser back/forward work; active state is
    # driven by the URL in _nav_style. WP-03: unicode glyphs -> inline-SVG icon set (consistent across OS).
    items = [dcc.Link([html.Span(svg_icon(_NAV_ICON.get(k, "chevron"), size=17), className="ic"),
                       html.Span(lbl)], href=KEY_TO_PATH[k],
                      className="nav-item", id={"type": "nav", "key": k}) for k, ic, lbl in NAV]
    items.append(html.Div([html.Div("BOT ENGINE", className="lbl"),
                           html.Div([html.Span(className="dot"), html.Span("RUNNING", className="st")]),
                           html.Div(id="sb-uptime", className="lbl", style={"marginTop": "6px"})],
                          className="engine"))
    return html.Div(items, className="sidebar")


app.layout = html.Div([
    html.A("Skip to content", href="#main", className="skip-link"),   # WP-07: keyboard skip link
    dcc.Location(id="url", refresh=False),
    dcc.Store(id="theme-store", storage_type="local", data="dark"),
    dcc.Interval(id="tick", interval=60_000, n_intervals=0),
    topbar(),
    paper_banner(),
    html.Div(className="shell", children=[sidebar(),
             html.Div(id="main", className="main", tabIndex="-1")]),
    statusbar(),
])


# Light/dark toggle (clientside, persisted in localStorage; applies stored theme on load via the tick).
# CSS provides [data-theme="light"]; default :root is dark, so data-theme="dark" simply falls through.
app.clientside_callback(
    """
    function(nclicks, ntick, current) {
        var theme = current || 'dark';
        var trig = (dash_clientside.callback_context.triggered || []).map(function(t){return t.prop_id;});
        if (trig.some(function(p){return p.indexOf('theme-toggle') === 0;})) {
            theme = (theme === 'light') ? 'dark' : 'light';
        }
        document.documentElement.setAttribute('data-theme', theme);
        return [theme, theme === 'light' ? 'Light' : 'Dark'];
    }
    """,
    [Output("theme-store", "data"), Output("theme-toggle", "children")],
    [Input("theme-toggle", "n_clicks"), Input("tick", "n_intervals")],
    State("theme-store", "data"),
)


# WP-02: URL-driven routing. dcc.Link sets url.pathname; _route resolves it (normalizing legacy paths and
# emitting a redirect so the address bar shows the canonical route) and serves the cached page. Routing on
# the URL (not the 60s tick) preserves the BUG-FIX #1 property: the active page is not re-rendered on tick,
# so Sandbox inputs survive; live elements update via their own small callbacks.
def _resolve_key(pathname):
    path = pathname or "/"
    path = LEGACY_ROUTES.get(path, path)
    return PATH_TO_KEY.get(path, "overview")


@app.callback(Output("main", "children"), Output("url", "pathname"), Input("url", "pathname"))
def _route(pathname):
    _ensure_prewarm()
    path = pathname or "/"
    redirect = dash.no_update
    if path in LEGACY_ROUTES:                       # old deep link -> canonicalize the address bar
        redirect = LEGACY_ROUTES[path]
    key = _resolve_key(path)
    hit = _PAGE_CACHE.get(key)
    tree = hit[1] if (hit and time.time() - hit[0] < _PAGE_TTL) else _render_page(key)
    return tree, redirect


@app.callback(Output({"type": "nav", "key": ALL}, "className"), Input("url", "pathname"))
def _nav_style(pathname):
    key = _resolve_key(pathname)
    return ["nav-item active" if o["id"]["key"] == key else "nav-item" for o in ctx.outputs_list]


# $1k paper-equity time-window selector (USER ASK 2026-06-21): re-window the equity series + readout on each
# RadioItems pick (12hr / 1D / 3D / 1W / 1M / All). Keyed only on the selector -> does not re-render the page.
@app.callback(Output("run-equity-graph", "figure"), Output("run-equity-readout", "children"),
              Input("run-equity-window", "value"), Input("run-equity-kelly", "value"))
def _run_equity_window(window, kelly):
    w = window or "1W"
    overlay = bool(kelly) and "on" in kelly
    def build():
        fig, readout, _col = _equity_figure(w, overlay)
        return _as_dict_fig(fig), readout
    return _cb_memo(("eqwin", w, overlay), 60.0, build)


# (2026-06-22) The resolution-day current/next TOGGLE was replaced by full per-date SECTIONS rendered
# statically in panel_resolution_day_curve (_resday_section).
# (2026-07-01) SETTLED past resolution days moved behind a dropdown (they no longer render as live sections);
# this callback renders the picked past day's full value-vs-paid section on demand.
@app.callback(Output("resday-past-section", "children"), Input("resday-past-select", "value"))
def _resday_past_section(date):
    if not date:
        return html.Div()
    def build():
        return _resday_section(date)
    return _cb_memo(("resdaypast", str(date)), 120.0, build)


# Per-stream calibration deck (Bayes feed): the dropdown drives the chip + PIT + coverage + meta line.
# Keyed only on the dropdown -> does NOT re-render the page on the 60s tick.
@app.callback(Output("calib-chip", "children"), Output("calib-pit", "figure"),
              Output("calib-cov", "figure"), Output("calib-meta", "children"),
              Input("calib-stream", "value"))
def _calib_stream(stream):
    def build():
        d = table("calibration_streams")
        empty = _dfig([], h=300)
        if d.empty or stream is None:
            return "", empty, _dfig([], h=180), ""
        sel = d[d["stream"] == stream]
        if sel.empty:
            return "", empty, _dfig([], h=180), ""
        row = sel.iloc[0]
        chip = _conf_chip(row.get("direction"), row.get("s_star"))
        pit = _as_dict_fig(_calib_pit_figure(row))
        cov = _as_dict_fig(_calib_cov_figure(row))
        # honest meta line: folds, residual RMSE, mean sigma, coverage, window.
        def _n(v, f="{:.2f}"):
            return "—" if _isnull(v) else f.format(float(v))
        meta = (f"{_stream_label(stream)} ({str(row.get('market') or '').upper()}) · "
                f"n={int(row.get('n_folds') or 0)} WF folds · "
                f"residual RMSE {_n(row.get('rmse_resid_F'))}°F · mean σ {_n(row.get('mean_sd_F'))}°F · "
                f"cov80 {_n(row.get('cov80'), '{:.1%}')} (nom 80%) / cov90 {_n(row.get('cov90'), '{:.1%}')} "
                f"(nom 90%) · {row.get('date_start')} → {row.get('date_end')}. Leak-free walk-forward, "
                f"paper/backtest.")
        return chip, pit, cov, meta
    return _cb_memo(("calib", stream), 60.0, build)


# WP-06: the multi-city scope bar on the Lab per-stream section. A chip click re-scopes the chart body and
# repaints the active-chip state. prevent_initial_call -> the page already renders the ALL view by default.
@app.callback(Output("stream-fwd-charts", "children"),
              Output({"type": "scope-chip", "key": ALL}, "className"),
              Input({"type": "scope-chip", "key": ALL}, "n_clicks"),
              prevent_initial_call=True)
def _stream_fwd(_clicks):
    key = ctx.triggered_id.get("key") if isinstance(ctx.triggered_id, dict) else "ALL"
    classes = ["scope-chip active" if o["id"]["key"] == key else "scope-chip"
               for o in ctx.outputs_list[1]]
    return _stream_forward_view(key), classes


@app.callback(Output("tb-clock", "children"), Output("tb-tickers", "children"),
              Output("sb-uptime", "children"), Input("tick", "n_intervals"))
def _live(_n):
    now = datetime.now(timezone.utc)
    cn = table("city_network")
    tk = []
    # HONEST market-style ticker strip: per-city day-ahead forecast high + the validated paper edge as the
    # green/red "delta". Real public signals (forecast + our paper edge) -- NOT invented SPX/VIX, NOT P&L.
    if not cn.empty:
        # WP-01: show ALL wired cities (was head(6) -> silently dropped the 7th+ city from the strip).
        for _, r in cn.iterrows():
            fc = "—" if _isnull(r["forecast_f"]) else f"{r['forecast_f']:.0f}°F"
            e = r["edge_c"]
            if _isnull(e):
                dv, dcls = "", "v"
            else:
                dv, dcls = f"{e:+.1f}c", ("v up" if e >= 0 else "v down")
            tk.append(html.Div([html.Span(f"{r['city']} ", className="k"),
                                html.Span(fc, className="v"),
                                html.Span(dv, className=dcls)], className="ticker"))
    tk.append(html.Div([html.Span("INTEGRITY ", className="k"),
                        html.Span(meta_value("integrity_verdict"), className="v")], className="ticker"))
    # WP-01: topbar clock in ET (the market's clock; site-wide standard), not UTC.
    et_clock = _to_et_naive(now).strftime("%H:%M:%S ET")
    return et_clock, tk, f"data · {meta_value('generated_at_utc')}"


@app.callback(Output("ov-updated", "children"), Input("tick", "n_intervals"))
def _ov_updated(_n):
    return f"updated {meta_value('generated_at_utc')}"


@app.callback(Output("status-updated", "children"), Output("tb-stale", "children"),
              Output("status-verdict", "children"), Input("tick", "n_intervals"))
def _statusbar(_n):
    return f"LAST {meta_value('generated_at_utc')}", staleness_chip(), _system_status()


# ---- Sandbox profitability model (transparent; paper estimate only) ----
DEPTH_CAP = 250        # contracts fillable within slippage (measured median; flat fallback only)
# HIGH-S1 and DAILY-LOW edges are DIRECT c/contract inputs (user ask: c/ct is more conventional than the old
# RMSE->edge derivation). The edge field is the GROSS RAW backtest model-edge MEAN across the FULL activated
# book (kelly_activated_book per_stream mean_c), NOT NY alone (2026-06-24 fix: the old 3.9/6.7 were NY-anchored
# net_opt+1c -> understated). High = mean(NY 4.02, LAX 5.24, CHI 5.42) = 4.9c; daily-low = mean over ALL 5
# activated low streams INCLUDING NY-low = mean(NY-low 6.96, AUS 8.38, LAX 9.77, DEN 7.03, MIA 5.75) = 7.6c.
# The activated-book MC's net_opt = mean_c - 2.0c (slippage + fee) -> base FILL-COST default 2.0c lands the
# net at net_opt (~2.9c high / ~5.6c low).
# EDGE DEFAULTS split by SEASON CLASS (2026-06-24, user ask). The sandbox distinguishes the ALL-SEASON /
# currently-active streams from the COLD-ONLY (validated cold edge; warm $0) ADD-ON streams, for BOTH high and
# low. Defaults = the real per-bucket means we actually have, and the all-season buckets reproduce the live
# deployed book so cold-cities-zero == the deployed ~21%/m projection, with cold adding the cold-season upside:
#   HIGH all-season  = NY/LAX/CHI all-season mean ~4.9c (deployed high book, 3 cities)
#   HIGH cold-only   = LV/MIN cold mean ~9.5c (A6 watch; +9.19/+9.89, 2 cities)
#   LOW  all-season  = the deployed low book NY-low + AUS/LAX/DEN/MIA(warm) ~7.6c (5 cities). The 4 warm
#                      daily-low are warm-activated NOW; folding them here keeps the warm baseline = deployed.
#   LOW  cold-only   = PHIL/PHX cold ADD ~14.7c (+11.79/+17.65, 2 cities) -- $0 in warm, auto-stage at cold turn.
# Cold buckets default ON to their validated city counts (full-book default, user-chosen) so the scenario
# shows year-round + cold upside; zero the cold cities for the warm-season-only (deployed) view.
S1_HIGH_EDGE_DEFAULT = 4.9          # gross raw-backtest c/ct, HIGH all-season (NY/LAX/CHI mean)
S1_HIGH_COLD_EDGE_DEFAULT = 9.5     # gross c/ct, HIGH cold-only add (LV/MIN cold mean +9.19/+9.89)
LOW_EDGE_DEFAULT = 7.6              # gross c/ct, LOW all-season / active (NY-low + AUS/LAX/DEN/MIA warm)
LOW_COLD_EDGE_DEFAULT = 14.7        # gross c/ct, LOW cold-only add (PHIL/PHX cold mean +11.79/+17.65)
# Per-trade contract SIZING that anchors the linear what-if to the LIVE deployed book's Kelly-MC median (the
# kelly_activated_book joint-MC that run_projection reads). The 8-stream activated book (incl NY-low, MC
# regenerated 2026-06-24) medians 21.33%/m, so CT_CAL=3.52 makes the deployed-config default MATCH that
# projection. It is anchored to the CURRENT deployed reality, then profit SCALES with every input (add a
# stream -> >21.33%, drop one -> less). It is NOT pinned to HOLD a default when the scenario changes -- the
# bug a user caught 2026-06-24 was the OPPOSITE: I lowered it to keep the default at the STALE 7-stream 14.63%
# WHILE adding NY-low, which HID NY-low's real +6.7%/m lift. Re-solve ONLY when the deployed book itself
# changes AND the MC is regenerated (here: 7->8 streams, median 14.63->21.33), so the default tracks reality.
SANDBOX_CT_CAL = 3.5233

# ---- NON-LINEAR per-stream slippage(size) curves (AUDIT-CORRECTED 2026-06-21) ----
# The ONLY stream with a real logged per-size fill curve is NY-high (median across n logged lock-moment
# signals): 0c@25 / 1c@50,100 / 2c@250 -> net stays positive to 250ct/market on its ~12c edge. The 6 other
# streams have only a 25ct-confirmed scalar (full curve ACCRUING since 2026-06-21) -> NO real curve to read.
# So we derive ONE real archetype ('high' = NY's measured curve) and apply it to all binding books as a
# best-available proxy; the daily-low archetype reuses the same shape (its own curve is accruing). This keeps
# the depth model GROUNDED in the only real data and produces a HIGHER, honest capacity than the old tiny/
# degenerate ceilings -> profit-vs-bankroll now plateaus as a visible staircase. CAVEAT (carried into the
# note): model_edge is held FIXED across sizes -> this is the FILLS side of capacity only.
# ---- REAL per-stream fill curves (AUDIT-CORRECTED 2026-06-21, user ask: per-stream so the capacity chart
# drops streams one GROUP at a time instead of one shared cliff). The deployed high cities each carry a REAL
# measured slippage(size) curve in fill_scalability: NY = lock-moment (deep), LAX/CHI = 9-day standing-book
# (deep, ~0 slip to 250ct). The daily-low books are SHALLOWER (1-day standing read 2026-06-21: slip ~1c to
# ~100ct, then climbs to ~9-18c by 250ct) -> they saturate at a SMALLER fill size and so bind at a LOWER
# bankroll than the highs. That difference is what makes capacity-vs-bankroll a STAIRCASE instead of a cliff.
_HIGH_CITY_ORDER = ["NY_high_S1", "LAX_high_S1", "CHI_high_S1"]
# Conservative daily-low curve, PRELIMINARY from the 1-day low standing-book read (the low depth archive is
# still maturing ~1-2wk before promotion to a producer real_curve like the highs). Slippage-vs-best (0 at the
# top of book, like the real high curves; the base-slip/fee floor is applied separately so this is INCREMENTAL
# size slippage only) climbs to ~9c by 250ct -> SHALLOWER than the highs, so low books saturate ~100-150ct.
_LOW_FALLBACK_CURVE = [(10, 0.0), (25, 0.0), (50, 0.5), (100, 1.5), (250, 9.0)]


_CURVE_MEMO: dict = {}     # PERF: cache the pandas-derived curves; the source table() already has a 120s TTL,
_CURVE_TTL = 120.0         # but the groupby->dict reshape (~34 ms) re-ran on every sandbox keystroke.


def _real_stream_curves():
    """{stream_id: [(size_ct, slip_c), ...]} for every stream with depth_state=='real_curve' in the curated
    fill_scalability table (NY/LAX/CHI high today). {} if none materialized. MEMOIZED (TTL) -- the reshape is
    pure recompute on a TTL-cached table, so it was wasted work on every interactive callback."""
    _now = time.time()
    _hit = _CURVE_MEMO.get("real")
    if _hit and _now - _hit[0] < _CURVE_TTL:
        return _hit[1]
    sc = table("fill_scalability")
    out: dict = {}
    if (not sc.empty) and "depth_state" in sc:
        sc = sc[sc["depth_state"] == "real_curve"]
        for sid, g in sc.groupby("stream_id"):
            gg = g.groupby("size_ct")["slippage_vs_best_c"].mean().sort_index()
            out[str(sid)] = [(float(s), float(v)) for s, v in gg.items()]
    _CURVE_MEMO["real"] = (_now, out)
    return out


def _scal_curves():
    """Representative high/low curves for the HEADLINE net-at-size math (auto/manual slip modes). high = the
    deepest real high curve (NY); low = the real warm-low curve if promoted, else the conservative fallback.
    MEMOIZED (TTL) off _real_stream_curves (itself memoized)."""
    _now = time.time()
    _hit = _CURVE_MEMO.get("scal")
    if _hit and _now - _hit[0] < _CURVE_TTL:
        return _hit[1]
    real = _real_stream_curves()
    high = next((real[s] for s in _HIGH_CITY_ORDER if s in real), None)
    low = real.get("AUS_low_S1") or _LOW_FALLBACK_CURVE
    out = {}
    if high:
        out["high"] = high
    if low:
        out["low"] = low
    _CURVE_MEMO["scal"] = (_now, out)
    return out


def _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm, s1_edge_c, low_edge_c, lock_edge_c,
                        cold_cities=0, s1tr_cold=0.0, s1_cold_edge_c=0.0,
                        lowcold_cities=0, lowcold_trades=0.0, low_cold_edge_c=0.0):
    """Each ACTIVE per-market book as (key, net_edge_c, slip_curve, trades_per_mo). Real per-city HIGH curves
    + a conservative DAILY-LOW curve -> shallower low books saturate at a smaller size (earlier bankroll) than
    deep high books, so the capacity-vs-bankroll curve is a STAIRCASE that drops streams a group at a time.
    Cold-only high/low books (2026-06-24 season split) use the SAME archetype depth curves as their year-round
    counterparts (high cold = HIGH curves cycled; low cold = the daily-low curve) at their own edge/cadence."""
    real = _real_stream_curves()
    hi = [real[s] for s in _HIGH_CITY_ORDER if s in real] or [None]
    low_curve = real.get("AUS_low_S1") or _LOW_FALLBACK_CURVE
    books = []
    for i in range(max(0, int(cities))):
        books.append((f"high{i+1}", s1_edge_c, hi[i] if i < len(hi) else hi[-1], s1tr))
    for i in range(max(0, int(cold_cities))):                     # high cold-only (LV/MIN) -> HIGH depth curves
        books.append((f"highcold{i+1}", s1_cold_edge_c, hi[(int(cities) + i) % len(hi)], s1tr_cold))
    for j in range(max(0, int(low_cities))):
        books.append((f"low{j+1}", low_edge_c, low_curve, low_trades))
    for j in range(max(0, int(lowcold_cities))):                  # low cold-only -> daily-low depth curve
        books.append((f"lowcold{j+1}", low_cold_edge_c, low_curve, lowcold_trades))
    if lockpm > 0:
        books.append(("lock", lock_edge_c, real.get("NY_high_S1"), lockpm))
    return books


def _scal_data_asof():
    """LIVE freshness signal for the sandbox scaling curves (USER ASK 2026-06-21, Task B): a short
    'data as of <day> · N snapshots' string derived FROM the curated fill tables (NOT a hardcoded date), so
    as the depth archive + forward fill_curve logging grow, the note moves. Pulls the materialize timestamp
    from meta.generated_at_utc, the total logged-signal/snapshot count from fill_scalability.n_signals (the
    real-curve streams), and how many streams have a real curve vs are still accruing. () if no data yet."""
    sc = table("fill_scalability")
    if sc.empty:
        return ""
    asof = meta_value("generated_at_utc", "")[:16].replace("T", " ")
    n_snaps = 0
    if "n_signals" in sc.columns:
        try:
            n_snaps = int(sc["n_signals"].fillna(0).astype(float).max())
        except (ValueError, TypeError):
            n_snaps = 0
    n_real = int((sc.get("depth_state") == "real_curve").sum()) if "depth_state" in sc.columns else 0
    n_real_streams = sc[sc.get("depth_state") == "real_curve"]["stream_id"].nunique() if (
        "depth_state" in sc.columns and "stream_id" in sc.columns) else 0
    n_accr_streams = sc[sc.get("depth_state") == "accruing"]["stream_id"].nunique() if (
        "depth_state" in sc.columns and "stream_id" in sc.columns) else 0
    bits = []
    if asof:
        bits.append(f"data as of {asof} UTC")
    if n_snaps:
        bits.append(f"{n_snaps:,} fill snapshots")
    bits.append(f"{n_real_streams} stream(s) with a real curve · {n_accr_streams} accruing")
    return " · ".join(bits)


def _slip_at_size(curve, size):
    """Linear-interpolate VWAP slippage (c/ct) at an arbitrary per-market size from a [(size,slip)] curve.
    Below the first tested size -> the first slippage (typically 0); above the last -> the last (capacity
    is enforced separately by the net-edge sign, so we do NOT extrapolate slippage upward past tested data)."""
    if not curve:
        return 0.0
    if size <= curve[0][0]:
        return curve[0][1]
    if size >= curve[-1][0]:
        return curve[-1][1]
    for (s0, v0), (s1, v1) in zip(curve, curve[1:]):
        if s0 <= size <= s1:
            t = (size - s0) / (s1 - s0) if s1 > s0 else 0.0
            return v0 + t * (v1 - v0)
    return curve[-1][1]


def _net_edge_at_size(base_edge_c, curve, size):
    """Net edge per contract (c/ct) for filling `size` contracts in one market: the fixed model edge minus
    the VWAP slippage at that size (Mosaic's net_edge_after_fills, minus fee already folded into base_edge).
    DEGRADES with size and goes negative past the capacity ceiling -> the honest non-linear response."""
    return base_edge_c - _slip_at_size(curve, size)

# Kelly stake-sweep — TRANSPARENT EMBEDDED CONSTANT, sourced verbatim from
# data/processed/kelly_1k_stake_sweep_20260619_000854.json ($1,000 correlation-aware MC, every edge
# sized at its CI lower bound). Rows: fraction -> the real risk/return numbers. Interpolated linearly
# between rows when the user picks an in-between fraction. 0.50x = the HARD CEILING.
# Keys: med = median %/m, p5 = p5 %/m (1-in-20 bad month), dd = p95 max-DD % (staged),
#       sdd = p95 max-DD % under STRESS (all edges at CI lower bound), stress = stress median %/m.
# ALL values verified against data/processed/kelly_1k_stake_sweep_20260619_000854.json. The earlier
# P(month<0)/P(DD>25%) probabilities were DROPPED: inconsistent between Kelly's .md and .json AND
# internally impossible (P(DD>25%) cannot exceed P(DD>p95)=5%). Drawdown PERCENTILES are well-defined.
# EXTENDED to 1.0x (BUG FIX #3): the full-Kelly row is REAL (median +29.69%/m, p95 maxDD 33.1%, STRESS
# maxDD 40.7%) -- no artificial ROI cap. Higher profit is reachable; the ruin/drawdown warnings escalate
# honestly with the fraction. All values verified against kelly_1k_stake_sweep_20260619_000854.json.
KELLY_SWEEP = [
    {"f": 0.25, "med":  7.08, "p5":  -4.51, "dd":  9.4, "sdd": 12.0, "stress": 1.94},
    {"f": 0.35, "med":  9.98, "p5":  -6.32, "dd": 12.9, "sdd": 16.5, "stress": 2.66},
    {"f": 0.50, "med": 14.40, "p5":  -9.03, "dd": 18.0, "sdd": 22.7, "stress": 3.68},
    {"f": 0.65, "med": 18.91, "p5": -11.73, "dd": 22.8, "sdd": 28.6, "stress": 4.63},
    {"f": 0.75, "med": 21.97, "p5": -13.53, "dd": 25.9, "sdd": 32.3, "stress": 5.22},
    {"f": 1.00, "med": 29.69, "p5": -17.98, "dd": 33.1, "sdd": 40.7, "stress": 6.55},
]
KELLY_MAX = 1.00       # slider ceiling = full Kelly (highest validated profit; highest ruin risk)
KELLY_CEILING = 0.50   # RECOMMENDED ceiling — beyond it the STRESS max-drawdown climbs steeply (NOT a cap)


def kelly_interp(frac):
    """Linear interpolation of the embedded Kelly sweep at an arbitrary fraction (clamped to row range)."""
    rows = KELLY_SWEEP
    if frac <= rows[0]["f"]:
        return dict(rows[0])
    if frac >= rows[-1]["f"]:
        return dict(rows[-1])
    for a, b in zip(rows, rows[1:]):
        if a["f"] <= frac <= b["f"]:
            t = (frac - a["f"]) / (b["f"] - a["f"])
            return {k: (a[k] + t * (b[k] - a[k])) if k != "f" else frac for k in a}
    return dict(rows[-1])


# severity classing for the risk metrics (green ok / amber elevated / red ruin)
def _sev_dd(dd):       # p95 max-drawdown %
    return "good" if dd < 12 else ("warn" if dd < 20 else "bad")


def _sev_sdd(dd):      # p95 max-drawdown % under the STRESS scenario (stricter band than staged)
    return "good" if dd < 16 else ("warn" if dd < 25 else "bad")


# Risk-vs-return frontier + ruin/drawdown probability curves depend ONLY on the embedded KELLY_SWEEP (the
# user's edges/cities never enter them) -> compute the (formerly per-keystroke) 16-fraction x 3000-path MC
# ONCE and reuse. Only the "your pick" markers/lines move with the slider. Lazy + cached at module scope.
_RISK_STATIC: dict | None = None


def _risk_static():
    global _RISK_STATIC
    if _RISK_STATIC is not None:
        return _RISK_STATIC
    import numpy as _np
    fr = [r["f"] for r in KELLY_SWEEP]; med = [r["med"] for r in KELLY_SWEEP]; dd = [r["dd"] for r in KELLY_SWEEP]
    DD25, RUIN_DD = 0.25, 0.50
    rng2 = _np.random.default_rng(98765)
    fr_grid = _np.round(_np.arange(0.25, 1.001, 0.05), 2)
    p_dd25, p_ruin = [], []
    for f in fr_grid:
        kf = kelly_interp(float(f))
        mu = kf["med"] / 100.0
        sig = max(1e-4, (kf["med"] - kf["p5"]) / 100.0 / 1.645)
        dr = rng2.normal(mu, sig, size=(3000, 12))
        eqp = _np.cumprod(1.0 + _np.clip(dr, -0.95, None), axis=1)
        peak = _np.maximum.accumulate(eqp, axis=1)
        ddp = (peak - eqp) / peak
        maxdd = ddp.max(axis=1)
        p_dd25.append(float((maxdd >= DD25).mean()))
        p_ruin.append(float((maxdd >= RUIN_DD).mean()))
    _RISK_STATIC = {"fr": fr, "med": med, "dd": dd, "fr_grid": list(fr_grid),
                    "p_dd25": p_dd25, "p_ruin": p_ruin, "DD25": DD25, "RUIN_DD": RUIN_DD}
    return _RISK_STATIC


def _risk_metric(label, value, sev, meaning):
    color = {"good": MINT, "warn": AMBER, "bad": RED}[sev]
    return html.Div([
        html.Div(label, className="u-label"),
        html.Div(value, className="mono", style={"fontSize": "24px", "fontWeight": "800",
                                                  "color": color, "margin": "2px 0 2px"}),
        html.Div(meaning, className="sub", style={"fontSize": "11px", "lineHeight": "1.45"})],
        className="card", style={"flex": "1", "minWidth": "150px", "padding": "12px 14px"})


def _optimal_fill_dollars(base_edge_c, curve, trades, markets):
    """Profit-maximizing per-market $ along a NON-LINEAR slippage(size) curve (Mosaic). Since net edge per
    contract DEGRADES with size and goes negative past the capacity ceiling, total $ = net_edge(size)*size is
    a HUMP: it rises, peaks, then falls. The absolute ceiling = the peak (best size to fill). Returns
    (max_$_per_market_per_trade, best_size). Falls back to the flat 250ct cap if no curve is available."""
    if trades <= 0 or markets <= 0 or base_edge_c <= 0:
        return 0.0, 0.0
    if not curve:
        return DEPTH_CAP * (base_edge_c / 100.0), float(DEPTH_CAP)
    best_d, best_sz = 0.0, 0.0
    # sweep tested sizes + a fine grid up to the deepest tested size (no extrapolation past it)
    smax = curve[-1][0]
    sizes = sorted({s for s, _ in curve} | {smax * f for f in (0.25, 0.5, 0.75, 1.0)})
    for sz in sizes:
        net_c = _net_edge_at_size(base_edge_c, curve, sz)
        d = (net_c / 100.0) * sz
        if d > best_d:
            best_d, best_sz = d, sz
    return max(0.0, best_d), best_sz


def _capacity_ceiling_dollars(cities, s1tr, low_cities, low_trades, lockpm,
                              s1_edge_c, low_edge_c, lock_edge_c,
                              cold_cities=0, s1tr_cold=0.0, s1_cold_edge_c=0.0,
                              lowcold_cities=0, lowcold_trades=0.0, low_cold_edge_c=0.0):
    """Absolute-$ monthly profit CEILING from REAL non-linear market depth, summed PER BOOK (each book fills
    its profit-MAXIMIZING size along its OWN archetype slippage(size) curve; net edge degrades with size, $
    peaks then falls). Bankroll-independent -> the hard plateau. Because shallow daily-low books peak at a
    smaller size than the deep high books, the books saturate at DIFFERENT bankrolls -> the staircase. Falls
    back to the flat 250ct cap per book if a curve is absent. Includes the cold-only high/low books."""
    books = _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm,
                                s1_edge_c, low_edge_c, lock_edge_c,
                                cold_cities, s1tr_cold, s1_cold_edge_c,
                                lowcold_cities, lowcold_trades, low_cold_edge_c)
    tot = 0.0
    for _key, edge, curve, trades in books:
        peak, _ = _optimal_fill_dollars(edge, curve, trades, 1)
        tot += peak * trades
    return max(0.0, tot)


def _fmt_dur(months):
    """Float months -> 'X years, Y months, Z days' (years dropped when 0)."""
    months = max(0.0, float(months))
    yrs = int(months // 12); rem = months - yrs * 12; mo = int(rem); days = int(round((rem - mo) * 30.44))
    if days >= 30:
        mo += 1; days -= 30
    if mo >= 12:
        yrs += 1; mo -= 12
    seg = []
    if yrs:
        seg.append(f"{yrs} year" + ("s" if yrs != 1 else ""))
    seg.append(f"{mo} month" + ("s" if mo != 1 else ""))
    seg.append(f"{days} day" + ("s" if days != 1 else ""))
    return ", ".join(seg)


def _ttt_text(big, detail, color=MINT):
    return html.Div([
        html.Div("Median time to reach the target", className="sub", style={"fontSize": "10.5px", "opacity": .8}),
        html.Div(big, className="mono", style={"fontSize": "25px", "fontWeight": "800", "color": color,
                                               "lineHeight": "1.12", "margin": "1px 0 2px"}),
        html.Div(detail, className="sub", style={"fontSize": "10.5px", "lineHeight": "1.4"})])


def _time_to_target(base, unc_rate, sigma_m, cap_ceiling, target_profit):
    """Monte-Carlo: months to grow `base` equity to base+target_profit, with the depth/liquidity ceiling
    applied EACH month (monthly return = min(drawn rate, cap_ceiling/equity)), so as equity compounds the $
    added/mo plateaus at the capacity ceiling and the time-to-target stretches. Returns (result_children,
    figure). Median + p5/p95 across the paths; the chart shows the median equity path + band to the target."""
    import numpy as np
    blank = _dfig([], h=240)
    if base <= 0 or target_profit <= 0:
        return (_ttt_text("—", "Enter a positive bankroll and a target profit.", DIM), blank)
    target_eq = base + target_profit
    if unc_rate <= 0:
        return (_ttt_text("not reachable", "These inputs have no positive net edge — equity does not grow "
                          "toward the target.", RED), blank)
    n_paths, MAXM = 3000, 600
    # CAPACITY-AWARE drift AND volatility: mu_eff = min(unc_rate, cap_ceiling/E) tightens as equity grows, and
    # volatility shrinks WITH it (sd = cv x mu_eff, cv = the validated sweep's coefficient of variation) because
    # a capacity-bound book fills near-deterministically. Scaling sd to the UNCAPPED rate (the old bug) made the
    # capped-upside returns wildly noisy -> a fat tail where slow paths never crossed (spurious '>50 years' at
    # high bankroll). Times are read off the PERCENTILE PATHS (below), so the star lands exactly on the line.
    cv = sigma_m / max(unc_rate, 1e-9)
    rng = np.random.default_rng(4242)
    E = np.full(n_paths, float(base))
    hist = [E.copy()]
    m = 0
    while m < MAXM:
        m += 1
        mu = np.minimum(unc_rate, cap_ceiling / np.maximum(E, 1e-9)) if cap_ceiling > 0 else np.full(n_paths, unc_rate)
        sd = np.maximum(cv * mu, 1e-4)
        r = np.clip(rng.normal(mu, sd), -0.95, None)
        E = E * (1.0 + r)
        hist.append(E.copy())
        if float((E >= target_eq).mean()) >= 0.95:   # cheap break (no partition): lower band has reached target
            break
    H = np.vstack(hist)                              # (months+1, paths); ONE vectorized percentile for the bands
    p5a, p50a, p95a = np.percentile(H, [5, 50, 95], axis=1)

    def _cross(arr):                                 # first interpolated month where a percentile path hits target
        for i in range(1, len(arr)):
            if arr[i - 1] < target_eq <= arr[i]:
                return (i - 1) + (target_eq - arr[i - 1]) / max(arr[i] - arr[i - 1], 1e-9)
        return None

    med_t = _cross(p50a)                             # median path reaching the target == the headline + the star
    if med_t is None:
        return (_ttt_text("> 50 years", f"the ${target_profit:,.0f} profit target is not reached within 50 "
                          "years at this scenario's capacity-capped growth.", AMBER), blank)
    fast_t = _cross(p95a)                            # luckiest band (p95 equity) reaches first
    slow_t = _cross(p5a)                             # unluckiest band (p5 equity) reaches last (may exceed the run)
    p5l, p50l, p95l = p5a.tolist(), p50a.tolist(), p95a.tolist()
    xs = list(range(len(p50l)))
    fig = _dfig(
        [{"type": "scatter", "x": xs + xs[::-1], "y": p95l + p5l[::-1], "fill": "toself",
          "fillcolor": "rgba(22,199,132,.10)", "line": {"width": 0}, "mode": "lines", "name": "p5–p95",
          "hoverinfo": "skip"},
         {"type": "scatter", "x": xs, "y": p50l, "mode": "lines", "name": "median equity",
          "line": {"color": MINT, "width": 2.4},   # linear (no spline) so the star sits exactly on the line
          "hovertemplate": "month %{x}<br>%{y:$,.0f}<extra></extra>"},
         {"type": "scatter", "x": [med_t], "y": [target_eq], "mode": "markers", "name": "target reached",
          "marker": {"size": 14, "color": AMBER, "symbol": "star", "line": {"width": 1.3, "color": "#fff"}},
          "hovertemplate": f"target ${target_eq:,.0f}<br>~{_fmt_dur(med_t)}<extra></extra>"}],
        h=240, legend=False,
        xaxis={"title": "months from now", "nticks": 8},
        yaxis={"title": "paper equity ($)", "tickprefix": "$", "tickformat": "~s"},
        shapes=[_hline(target_eq, AMBER, 1.4, "dash"), _hline(base, AXISCOL, 1, "dot")],
        annotations=[_ann(0.0, target_eq, f"target ${target_eq:,.0f}", AMBER, 10,
                          xref="paper", yref="y", xanchor="left", yanchor="bottom")])
    _slow_s = _fmt_dur(slow_t) if slow_t is not None else "> 50 years"
    _fast_s = _fmt_dur(fast_t) if fast_t is not None else _fmt_dur(med_t)
    detail = (f"to ${target_profit:,.0f} profit (${target_eq:,.0f} equity) · median path of {n_paths:,} "
              f"capacity-capped paths · p5–p95 band reaches it in {_fast_s} — {_slow_s}")
    return (_ttt_text(_fmt_dur(med_t), detail), fig)


@app.callback(
    Output("sb-profit", "children"), Output("sb-roi", "children"), Output("sb-roi", "style"),
    Output("sb-kelly-band", "children"), Output("sb-note", "children"), Output("sb-risk-metrics", "children"),
    Output("sb-chart", "figure"), Output("sb-fan", "figure"), Output("sb-cap", "figure"),
    Output("sb-cap-flag", "children"),
    Output("sb-ttt-result", "children"), Output("sb-ttt-chart", "figure"),
    Input("sb-s1edge", "value"), Input("sb-cities", "value"), Input("sb-s1trades", "value"),
    Input("sb-s1edge-cold", "value"), Input("sb-cities-cold", "value"), Input("sb-s1trades-cold", "value"),
    Input("sb-lowedge", "value"), Input("sb-lowcities", "value"), Input("sb-lowtrades", "value"),
    Input("sb-lowedge-cold", "value"), Input("sb-lowcities-cold", "value"), Input("sb-lowtrades-cold", "value"),
    Input("sb-lock", "value"), Input("sb-lockpm", "value"),
    Input("sb-bankroll", "value"), Input("sb-kelly", "value"),
    Input("sb-slip", "value"), Input("sb-slip-mode", "value"), Input("sb-slip-manual", "value"),
    Input("sb-target", "value"))
def _sandbox(s1_c, cities, s1tr, s1c_cold, cities_cold, s1tr_cold,
             low_c, low_cities, low_trades, lowc_cold, lowcities_cold, lowtr_cold,
             lock_c, lockpm, bankroll, kelly, slip, slip_mode, slip_manual, target):
    import numpy as _np
    blank = _dfig([], h=300)
    try:
        s1_c = max(0.0, float(s1_c)); cities = max(0, int(cities)); s1tr = max(0.0, float(s1tr))
        s1c_cold = max(0.0, float(s1c_cold)); cities_cold = max(0, int(cities_cold))
        s1tr_cold = max(0.0, float(s1tr_cold))
        low_c = float(low_c); low_cities = max(0, int(low_cities)); low_trades = max(0.0, float(low_trades))
        lowc_cold = max(0.0, float(lowc_cold)); lowcities_cold = max(0, int(lowcities_cold))
        lowtr_cold = max(0.0, float(lowtr_cold))
        lock_c = float(lock_c); lockpm = max(0.0, float(lockpm))
        bankroll = max(0.0, float(bankroll)); kelly = float(kelly)
        slip = max(0.0, float(slip))
        slip_mode = slip_mode if slip_mode in ("off", "auto", "manual") else "off"
        slip_manual = max(0.0, float(slip_manual)) if slip_manual is not None else 0.0
        target = max(0.0, float(target)) if target not in (None, "") else 0.0
    except (TypeError, ValueError):
        return ("—", "—", {"color": DIM}, "", "Enter valid numbers in every field.", "",
                blank, blank, blank, "", "", blank)
    kelly = min(KELLY_MAX, max(0.25, kelly))

    # ---- TRANSPARENT PER-STREAM PROFIT MODEL (FIX 1, 2026-06-19). Profit is now a DIRECT function of the
    # ACTIVE streams -- every input (edges, city counts, trades/mo, locks/mo, slippage, Kelly, bankroll)
    # MOVES the output. The old code anchored the dollar TOTAL to the Kelly-sweep median and only re-split
    # it, so lock-in edge / city counts never changed profit -> that was the bug.
    #   profit_per_stream($/mo) = (net_edge_c/100) * trades/mo * active_markets * contracts_per_trade
    # contracts_per_trade = Kelly-fraction-scaled stake at the stream's reference price, capped by DEPTH_CAP
    # (250ct/market within ~1c slip). The Kelly sweep is STILL the source of the per-trade risk fraction so
    # the numbers stay grounded; it just no longer overrides the per-stream economics. Win rate is NOT a
    # lever (FIX 2): each stream's net c/contract ALREADY embeds win/loss from the backtest -> dropped.
    k = kelly_interp(kelly)
    # GROSS per-stream model edge (before any slippage). The win rate is already embedded in the backtest
    # net c/contract; slippage is applied per-mode below (item 6).
    s1_gross_c = max(0.0, s1_c)            # direct gross c/contract input (was RMSE->edge derivation)
    s1cold_gross_c = max(0.0, s1c_cold)    # HIGH cold-only (LV/MIN) gross edge
    low_gross_c = max(0.0, low_c)
    lowcold_gross_c = max(0.0, lowc_cold)  # LOW cold-only (PHIL/AUS/MIA/PHX) gross edge
    lock_gross_c = max(0.0, lock_c)
    # per-stream NET edge after the BASE slippage (used by the depth-ceiling/optimal-fill math, which is
    # mode-agnostic and conservative -- the base slip floor)
    s1_edge_c = max(0.0, s1_gross_c - slip)
    s1cold_edge_c = max(0.0, s1cold_gross_c - slip)
    low_edge_c = max(0.0, low_gross_c - slip)
    lowcold_edge_c = max(0.0, lowcold_gross_c - slip)
    lock_edge_c = max(0.0, lock_gross_c - slip)
    # contracts per trade: CALIBRATED so the DEFAULT scenario (deployed 7-stream book) at 0.50x Kelly on
    # $1,000 reproduces the activated-book median (~14.63%/m) -- the headline stays GROUNDED to the real number,
    # it does NOT run away. From that anchor it scales LINEARLY with Kelly fraction and bankroll, and is
    # CAPPED at the measured fillable depth (DEPTH_CAP/market). Every per-stream input (edge, trades, cities)
    # still multiplies through, so all inputs MOVE the output (FIX 1) while the magnitude stays honest.
    active_books = max(1, cities + cities_cold + low_cities + lowcities_cold)   # note/labeling only
    # contracts per trade scales with Kelly fraction + bankroll (NO flat 250ct cliff anymore -- the cap is now
    # the NON-LINEAR per-stream slippage curve below).
    def contracts_per_trade():
        return max(0.0, SANDBOX_CT_CAL * (kelly / 0.25) * (bankroll / 1000.0))
    cpt = contracts_per_trade()
    s1_ct = low_ct = lock_ct = cpt                    # same calibrated stake unit per market
    # ---- NON-LINEAR DEPTH (Mosaic -> Iris 2026-06-20): the net edge per contract DEGRADES as the per-market
    # size grows along the stream's measured slippage(size) curve, instead of paying the full edge up to a
    # flat 250ct then a cliff. Effective net edge = model edge - VWAP slippage at THIS size. Past the real
    # capacity ceiling the net goes <=0 and adding contracts stops adding $ (the optimal-fill cap handles the
    # plateau). This is the honest 'more size != linearly more profit' mechanism. Falls back to the flat edge
    # if the curated curves are absent.
    _curves = _scal_curves()

    # ---- ITEM 6: slippage model selector ----
    # off (base): per-contract slippage = the fixed base value above; the per-stream Mosaic curve still
    #             governs depth (net goes <=0 past the real ceiling), but slippage does NOT scale with size.
    # auto:       slippage RISES with order size (which grows with bankroll) along Mosaic's slippage(size)
    #             curve -> the honest bankroll-dependent friction. base slip is added as a floor.
    # manual:     a single flat slippage the user sets, applied to every size (curve disabled).
    def _eff_slip(curve, size):
        if slip_mode == "manual":
            return slip_manual
        if slip_mode == "auto":
            return slip + _slip_at_size(curve, size)        # base + measured size-dependent VWAP slippage
        return slip                                          # off (base): fixed, no size/bankroll dependence

    def _net_at(gross_edge_c, curve, size):
        """Net c/ct at this size under the selected slippage model = gross model edge - effective slippage."""
        return gross_edge_c - _eff_slip(curve, size)

    s1_net_at = _net_at(s1_gross_c, _curves.get("high"), s1_ct)
    s1cold_net_at = _net_at(s1cold_gross_c, _curves.get("high"), s1_ct)
    low_net_at = _net_at(low_gross_c, _curves.get("low"), low_ct)
    lowcold_net_at = _net_at(lowcold_gross_c, _curves.get("low"), low_ct)
    lock_net_at = _net_at(lock_gross_c, _curves.get("high"), lock_ct)
    s1_net_at = max(0.0, s1_net_at); low_net_at = max(0.0, low_net_at); lock_net_at = max(0.0, lock_net_at)
    s1cold_net_at = max(0.0, s1cold_net_at); lowcold_net_at = max(0.0, lowcold_net_at)
    # monthly $ per stream = net_edge_at_size($) * trades/mo * markets * contracts/trade (size-degraded edge)
    s1_monthly = (s1_net_at / 100.0) * s1tr * cities * s1_ct
    s1cold_monthly = (s1cold_net_at / 100.0) * s1tr_cold * cities_cold * s1_ct
    low_monthly = (low_net_at / 100.0) * low_trades * low_cities * low_ct
    lowcold_monthly = (lowcold_net_at / 100.0) * lowtr_cold * lowcities_cold * low_ct
    lock_monthly = (lock_net_at / 100.0) * lockpm * cities * lock_ct
    total_uncapped = s1_monthly + s1cold_monthly + low_monthly + lowcold_monthly + lock_monthly
    # ---- DEPTH-CAPACITY CAP (deliverable #3): absolute-$ profit cannot exceed what real market depth
    # fills. ceiling = DEPTH_CAP ct * edge * trades/mo * markets -> bankroll-INDEPENDENT plateau. Past the
    # ceiling, more capital earns the SAME dollars (lower %). This is the "$100M -> same as ~$5k" reality.
    cap_ceiling = _capacity_ceiling_dollars(cities, s1tr, low_cities, low_trades, lockpm,
                                            s1_edge_c, low_edge_c, lock_edge_c,
                                            cities_cold, s1tr_cold, s1cold_edge_c,
                                            lowcities_cold, lowtr_cold, lowcold_edge_c)
    capacity_bound = bankroll > 0 and cap_ceiling > 0 and total_uncapped > cap_ceiling
    total = min(total_uncapped, cap_ceiling) if cap_ceiling > 0 else total_uncapped
    # if the cap binds, shrink each stream proportionally so the breakdown still sums to the capped total
    if capacity_bound and total_uncapped > 0:
        _scale = total / total_uncapped
        s1_monthly *= _scale; s1cold_monthly *= _scale
        low_monthly *= _scale; lowcold_monthly *= _scale; lock_monthly *= _scale
    # realized ROI on bankroll (falls once capacity binds)
    roi = (total / bankroll * 100.0) if bankroll > 0 else 0.0
    roi_color = MINT if total >= 0 else RED
    # escalating ruin warning by fraction (honest -- no ROI cap, just sharper warnings as risk climbs)
    if kelly <= KELLY_CEILING + 1e-9:
        ceil_flag = ""
        kelly_badge_cls = "badge good"
    elif kelly <= 0.75 + 1e-9:
        ceil_flag = html.Span("  ABOVE 0.50x RECOMMENDED CEILING", className="badge warn",
                              style={"marginLeft": "8px"})
        kelly_badge_cls = "badge warn"
    else:
        ceil_flag = html.Span("  FULL-KELLY ZONE · RUIN RISK", className="badge bad",
                              style={"marginLeft": "8px"})
        kelly_badge_cls = "badge bad"
    # SCENARIO vs VALIDATED-REFERENCE (FIX 2026-06-24): the headline median MUST be the COMPUTED scenario roi
    # (responds to every input), NOT the fixed Kelly-sweep median -- showing k['med'] made the risk panel
    # pigeonhole to 7.1%@0.25 / 14.4%@0.5 regardless of the edges/cities entered. The Kelly SWEEP
    # (kelly_1k_stake_sweep_20260619, re-validated 2026-06-24: re-running the MC reproduces 7.06%@0.25) is the
    # VALIDATED deployed-book reference at CI-lower-bound; we keep it as an explicit reference + use its risk
    # SHAPE (p5/stress as a fraction of its median) scaled to the scenario median, so downside tracks inputs.
    ratio = (roi / k["med"]) if abs(k["med"]) > 1e-6 else 1.0
    sc_p5 = k["p5"] * ratio
    sc_stress = k["stress"] * ratio
    kelly_band = html.Div([
        html.Span(f"Kelly {kelly:.2f}x", className=kelly_badge_cls),
        ceil_flag,
        html.Div(["Scenario median (your inputs): ", html.B(f"{roi:+.1f}%/m"),
                  "  ·  validated-sweep reference (deployed 4-stream book, CI-lower-bound) at this fraction: ",
                  html.B(f"{k['med']:+.1f}%/m"), f" median · {k['p5']:+.1f}%/m p5 · {k['stress']:+.1f}%/m stress"],
                 className="sub", style={"marginTop": "6px", "fontSize": "11.5px"})])

    metrics = html.Div([
        _risk_metric("Median return (scenario)", f"{roi:+.1f}%/m", "good" if roi > 0 else "bad",
                     "YOUR inputs: net edge x trades x cities x Kelly stake, capacity-capped. Moves with "
                     "every field (the validated deployed-book default is ~+14.6%/m)."),
        _risk_metric("Downside p5 (scenario)", f"{sc_p5:+.1f}%/m", _sev_dd(abs(sc_p5)),
                     "1-in-20 bad month — the validated risk shape scaled to your scenario median."),
        _risk_metric("p95 max drawdown (ref)", f"{k['dd']:.1f}%", _sev_dd(k["dd"]),
                     "Validated deployed-book drawdown at this Kelly fraction (reference, not rescaled)."),
        _risk_metric("Stress max drawdown (ref)", f"{k['sdd']:.1f}%", _sev_sdd(k["sdd"]),
                     "Validated deployed-book stress drawdown at this Kelly fraction (reference)."),
        _risk_metric("Stress return (scenario)", f"{sc_stress:+.1f}%/m", "good" if sc_stress > 0 else "bad",
                     "All edges at CI lower bound at once, scaled to your scenario.")],
        style={"display": "flex", "flexWrap": "wrap", "gap": "10px"})

    # ---- (a) 12-month Monte-Carlo equity fan ----
    # drift = the UNCAPPED scenario rate (so the fan reflects the user's edge/city/trade inputs); the monthly
    # sigma is scaled from the Kelly sweep's median/p5 spread (the validated risk shape) about it.
    # CAPACITY-AWARE (2026-06-25): the depth ceiling is an ABSOLUTE $/mo (cap_ceiling), so as a path's equity
    # COMPOUNDS the same % return earns more $ until it hits the ceiling, after which the % return MECHANICALLY
    # falls (return = min(uncapped_rate, cap_ceiling / equity)). The old fan compounded a CONSTANT return and so
    # overstated the upper paths (they grew unbounded through the ceiling). We now draw uncapped monthly returns
    # and apply the DOLLAR cap per path per month, so the median/p95 bend toward the plateau as they scale up.
    base = bankroll if bankroll > 0 else 1.0
    unc_rate = (total_uncapped / bankroll) if bankroll > 0 else (roi / 100.0)   # fractional, scale-invariant
    swp_sigma = max(1e-4, (k["med"] - k["p5"]) / 100.0 / 1.645)      # sweep's monthly sigma at this fraction
    sigma_m = max(1e-4, swp_sigma * (abs(unc_rate) / max(abs(k["med"]) / 100.0, 1e-6)) if k["med"] else swp_sigma)
    months = _np.arange(0, 13)
    rng = _np.random.default_rng(12345)
    n_paths = 4000
    # CAPACITY-AWARE drift AND volatility (2026-06-25 fix): the depth ceiling is an absolute $/mo, so as equity
    # compounds the effective monthly return mu_eff = min(unc_rate, cap_ceiling/E) TIGHTENS -- and because a
    # capacity-bound book fills the same size near-deterministically, its volatility shrinks WITH the return
    # (sd = cv x mu_eff, cv = the validated sweep's coefficient of variation). Scaling sigma to the UNCAPPED
    # rate produced wild noise with a capped upside (a fat tail) once the cap bound -- the bug behind the fan
    # going haywire / the time-to-target stalling at high bankroll.
    cv = sigma_m / max(unc_rate, 1e-9)
    eq = _np.empty((n_paths, 13)); eq[:, 0] = base
    for _m in range(12):
        _E = eq[:, _m]
        _mu = _np.minimum(unc_rate, cap_ceiling / _np.maximum(_E, 1e-9)) if cap_ceiling > 0 else unc_rate
        _sd = _np.maximum(cv * _mu, 1e-4)
        _r = _np.clip(rng.normal(_mu, _sd), -0.95, None)
        eq[:, _m + 1] = _E * (1.0 + _r)
    med_path = _np.median(eq, axis=0)
    p5_path = _np.percentile(eq, 5, axis=0)
    p95_path = _np.percentile(eq, 95, axis=0)
    _mo = list(months)
    fan = _dfig(
        [{"type": "scatter", "x": _mo + _mo[::-1], "y": list(p95_path) + list(p5_path)[::-1],
          "fill": "toself", "fillcolor": "rgba(22,199,132,.10)", "line": {"width": 0}, "mode": "lines",
          "name": "p5–p95", "hoverinfo": "skip"},
         {"type": "scatter", "x": _mo, "y": list(med_path), "mode": "lines", "name": "median",
          "line": {"color": MINT, "width": 2.4, "shape": "spline", "smoothing": 0.4},
          "hovertemplate": "month %{x}<br>%{y:$,.0f}<extra></extra>"},
         {"type": "scatter", "x": _mo, "y": list(p5_path), "mode": "lines", "name": "p5",
          "line": {"color": RED, "width": 1.3, "dash": "dot"},
          "hovertemplate": "month %{x}<br>p5 %{y:$,.0f}<extra></extra>"},
         {"type": "scatter", "x": _mo, "y": list(p95_path), "mode": "lines", "name": "p95",
          "line": {"color": CYAN, "width": 1.3, "dash": "dot"},
          "hovertemplate": "month %{x}<br>p95 %{y:$,.0f}<extra></extra>"}],
        h=300, legend=True,
        xaxis={"title": "month", "nticks": 13},
        yaxis={"title": "paper equity ($)", "tickprefix": "$", "tickformat": ",.0f"},
        shapes=[_hline(base, AXISCOL, 1, "dash")])

    # ---- profit breakdown by stream ----
    labels = ["High year-round", "High cold-only", "Low year-round", "Low cold-only", "Lock-in", "TOTAL"]
    vals = [s1_monthly, s1cold_monthly, low_monthly, lowcold_monthly, lock_monthly, total]
    _vmax = max(list(vals) + [1.0]); _vmin = min(list(vals) + [0.0])
    fig = _dfig(
        [{"type": "bar", "x": labels, "y": vals,
          "marker": {"color": [MINT, "#3aa6c2", "#7fb0a0", "#4f8fa6", CYAN, AMBER],
                     "cornerradius": 6, "line": {"width": 0}}, "width": 0.62,
          "text": [f"${v:,.0f}" for v in vals], "textposition": "outside", "cliponaxis": False,
          "textfont": {"family": "JetBrains Mono, monospace", "size": 11.5},
          "hovertemplate": "%{x}<br>%{y:$,.0f} / month<extra></extra>"}],
        h=300, legend=False, xaxis={"title": ""},
        yaxis={"title": "paper profit ($ / month)", "tickprefix": "$", "tickformat": ",.0f",
               "range": [_vmin * 1.18 if _vmin < 0 else 0, _vmax * 1.18]})

    # ---- (d) capacity-ceiling-vs-bankroll line chart (deliverable #3 + Mosaic non-linear 2026-06-20) ----
    # Re-evaluate the SAME per-stream profit model across a $100 -> $100M bankroll sweep. UNCAPPED = flat full
    # model edge, contracts grow with bankroll, NO depth limit (the naive linear extrapolation). CAPPED = the
    # REAL Mosaic curve: net edge per contract degrades with per-market size along slippage(size), and a stream
    # never fills past the size that maximizes its $ -> a SMOOTH plateau at the per-stream capacity ceiling
    # (not a hard 250ct cliff). LOG x (bankroll spans decades) + LINEAR y so the capped green line + its fill
    # reach $0 at low bankroll and the plateau at the ceiling is legible (the uncapped line clips off the top).
    cap_data = []; cap_shapes = []; cap_anns = []; cap_xaxis = None; cap_yaxis = None
    if cap_ceiling > 0:
        bxs = _np.geomspace(100.0, 1e8, 90)                          # $100 -> $100M log axis (full detail)
        # PER-BOOK staircase (2026-06-21): each active book fills along its OWN curve; shallow daily-low books
        # cap at a smaller size (lower bankroll) than the deep high books, so streams drop a group at a time.
        _books = _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm,
                                     s1_edge_c, low_edge_c, lock_edge_c,
                                     cities_cold, s1tr_cold, s1cold_edge_c,
                                     lowcities_cold, lowtr_cold, lowcold_edge_c)
        _peaks = [_optimal_fill_dollars(e, c, 1, 1) for _k, e, c, tr in _books]  # (peak_$/mkt, best_size), bk-indep
        def _profit_at(bk, capped):
            ct = max(0.0, SANDBOX_CT_CAL * (kelly / 0.25) * (bk / 1000.0))
            tot = 0.0
            for (_k, e, c, tr), (peak, bs) in zip(_books, _peaks):
                if not capped or not c:                              # naive flat-edge linear growth
                    tot += (e / 100.0) * tr * ct
                else:                                                # fill only up to the profit-MAX size, then
                    fill = min(ct, bs) if bs > 0 else ct             # hold (never fill into negative-edge depth)
                    net = max(0.0, _net_edge_at_size(e, c, fill))
                    tot += (net / 100.0) * fill * tr
            return tot
        prof_uncapped = _np.array([_profit_at(b, capped=False) for b in bxs])
        prof_capped = _np.array([_profit_at(b, capped=True) for b in bxs])
        # FINAL saturation = smallest bankroll where the capped curve reaches 99.5% of its ceiling -> the TRUE
        # plateau where extra bankroll stops buying profit (the labeled yellow line). 99.5% (not 99%) so the
        # mark lands ON the flat, matching "where bankroll stops returning more profit/mo".
        _reach = prof_capped >= 0.995 * cap_ceiling
        bind_b = float(bxs[_np.argmax(_reach)]) if _reach.any() else None
        # FIRST staircase step = bankroll where the SHALLOWEST book caps (its best_size reached) -> faint marker
        _bsz = [bs for (_pk, bs) in _peaks if bs and bs > 0]
        _denom = SANDBOX_CT_CAL * (kelly / 0.25)
        step1_b = (min(_bsz) / _denom * 1000.0) if (_bsz and _denom > 0) else None
        # mark the user's current bankroll on the capped (actual) curve
        bnow = max(100.0, min(1e8, bankroll if bankroll > 0 else 1000.0))
        pnow = _profit_at(bnow, capped=True)
        cap_data = [
            {"type": "scatter", "x": list(bxs), "y": list(prof_uncapped), "mode": "lines",
             "name": "uncapped (no depth limit)", "line": {"color": NEUTRAL, "width": 1.6, "dash": "dash"},
             "hovertemplate": "bankroll $%{x:,.0f}<br>uncapped $%{y:,.0f}/mo<extra></extra>"},
            {"type": "scatter", "x": list(bxs), "y": list(prof_capped), "mode": "lines",
             "name": "depth-capped (your actual)", "line": {"color": GREEN, "width": 2.8},
             "fill": "tozeroy", "fillcolor": "rgba(0,224,138,.08)",
             "hovertemplate": "bankroll $%{x:,.0f}<br>capped $%{y:,.0f}/mo<extra></extra>"},
            {"type": "scatter", "x": [bnow], "y": [max(pnow, 0.0)], "mode": "markers", "name": "your bankroll",
             "marker": {"size": 15, "color": AMBER, "symbol": "star", "line": {"width": 1.4, "color": "#fff"}},
             "hovertemplate": f"your bankroll ${bnow:,.0f}<br>$%{{y:,.0f}}/mo<extra></extra>"}]
        # Shapes/annotations take RAW data coords on a log axis (Plotly converts internally, exactly as
        # add_hline/add_vline do) -- y is LINEAR here anyway. The red ceiling line + the saturation vlines:
        cap_shapes.append(_hline(cap_ceiling, RED, 1.4, "dot"))
        cap_anns.append(_ann(0.0, cap_ceiling, f"absolute depth ceiling ${cap_ceiling:,.0f}/mo", RED, 10,
                             xref="paper", yref="y", xanchor="left", yanchor="bottom"))
        if step1_b and bind_b and 100 <= step1_b <= 1e8 and step1_b < bind_b * 0.9:
            cap_shapes.append(_vline(step1_b, NEUTRAL, 1.1, "dot"))
            cap_anns.append(_ann(step1_b, 0.04, f"shallow daily-low books saturate ~${step1_b:,.0f}", DIM, 9,
                                 xref="x", yref="paper", xanchor="left", yanchor="bottom"))
        if bind_b and 100 <= bind_b <= 1e8:
            cap_shapes.append(_vline(bind_b, AMBER, 1.9, "dash"))
            cap_anns.append(_ann(bind_b, 0.98,
                                 f"depth fully saturates ~${bind_b:,.0f} — beyond here extra bankroll adds ~$0/mo",
                                 AMBER, 10, xref="x", yref="paper", xanchor="right", yanchor="top"))
        cap_xaxis = {"type": "log", "title": "bankroll ($, log scale)", "tickprefix": "$", "tickformat": "~s"}
        # LINEAR y, clamped a little above the ceiling so the capped plateau + its fill-to-$0 are legible and
        # the uncapped line simply exits the top (its full magnitude is the hover, the message is the plateau).
        cap_yaxis = {"type": "linear", "title": "paper profit ($ / month)", "tickprefix": "$",
                     "tickformat": "~s", "range": [0, cap_ceiling * 1.35], "rangemode": "tozero"}
    cap = _dfig(cap_data, h=320, legend=len(cap_data) > 1, xaxis=cap_xaxis, yaxis=cap_yaxis,
                shapes=cap_shapes or None, annotations=cap_anns or None)

    if capacity_bound:
        cap_flag = html.Div([badge("CAPACITY-LIMITED", "warn"),
                             html.Span(f"  At ${bankroll:,.0f} the order-book DEPTH binds: slippage on extra "
                                       f"size eats the edge, so profit plateaus at ~${cap_ceiling:,.0f}/mo. "
                                       f"More capital here earns the SAME dollars at a lower %.", className="sub",
                                       style={"marginLeft": "8px"})],
                            style={"display": "flex", "alignItems": "baseline", "flexWrap": "wrap"})
    elif cap_ceiling > 0:
        cap_flag = html.Div([badge("WITHIN CAPACITY", "good"),
                             html.Span(f"  Profit still scales with bankroll; the depth-driven ceiling is "
                                       f"~${cap_ceiling:,.0f}/mo (the green plateau on the log chart below).",
                                       className="sub", style={"marginLeft": "8px"})],
                            style={"display": "flex", "alignItems": "baseline", "flexWrap": "wrap"})
    else:
        cap_flag = html.Div("Set non-zero edges to see the capacity ceiling.", className="sub")

    _nonlin = " (depth model: live Mosaic slippage curves)" if _curves else " (depth model: flat 250ct fallback)"
    note = (f"Profit is a TRANSPARENT per-stream sum over FOUR season-classed books (high/low × year-round/"
            f"cold-only), driven by every input: for each active stream, (net c/contract at this size ÷ 100) "
            f"× trades/mo × active markets × contracts/trade. Contracts/trade ({s1_ct:.1f} here) is anchored "
            f"to the deployed book's per-trade unit so the ALL-SEASON default (cold cities = 0) at 0.50x on "
            f"$1,000 reproduces the live Kelly-MC projection (~21.3%/m, the Projection panel); it scales "
            f"linearly with Kelly ({kelly:.2f}x) and bankroll (${bankroll:,.0f}). The cold-only books default "
            f"ON (full validated book), so the default sits above the warm-season deployed number; zero their "
            f"cities for the warm-season-only (deployed) view. "
            f"NON-LINEAR DEPTH{_nonlin}: the net c/contract DEGRADES as per-market size grows along each book's "
            f"measured VWAP-slippage curve and crosses zero at its real capacity ceiling (deep high books fill "
            f"to ~250ct; the shallower daily-low books cap sooner — the capacity chart is a staircase). "
            f"High year-round net {s1_net_at:.1f}c (model {s1_edge_c:.1f}c) × {s1tr:.0f}/mo × {cities} = "
            f"${s1_monthly:,.0f}/mo; high cold-only {s1cold_net_at:.1f}c × {s1tr_cold:.0f}/mo × {cities_cold} = "
            f"${s1cold_monthly:,.0f}/mo; low year-round {low_net_at:.1f}c × {low_trades:.0f}/mo × {low_cities} = "
            f"${low_monthly:,.0f}/mo; low cold-only {lowcold_net_at:.1f}c × {lowtr_cold:.0f}/mo × "
            f"{lowcities_cold} = ${lowcold_monthly:,.0f}/mo; lock-in {lock_net_at:.1f}c × {lockpm:.0f}/mo × "
            f"{cities} = ${lock_monthly:,.0f}/mo. Win rate is NOT a lever — each net "
            f"c/contract already embeds win/loss from the backtest. The total is bounded by real market depth "
            f"-> a ~${cap_ceiling:,.0f}/mo absolute ceiling "
            f"{'(BINDING now)' if capacity_bound else '(not binding yet)'}; past it, bankroll buys no extra "
            f"dollars. Model edge is held FIXED across sizes "
            f"(this is the FILLS side of capacity only). Paper/backtest — NOT a guarantee, never realized P&L; "
            f"LIVE capital today = $0 until the forward gates PASS.")
    if slip_mode != "off":
        _sm = ("AUTO" if slip_mode == "auto" else "MANUAL")
        note += (f"  Slippage model = {_sm}: " + ("per-contract slippage scales up with order size as "
                 "bankroll grows along Mosaic's measured slippage(size) curve (base slip is the floor)."
                 if slip_mode == "auto" else
                 f"a flat {slip_manual:.1f}c/contract override on every fill (the curve is disabled)."))

    # ---- (f) TIME TO TARGET PROFIT: how long THIS scenario needs to reach the user's profit goal, with the
    # depth/liquidity ceiling applied EACH month (so growth decelerates as equity scales into capacity). Reuses
    # the capacity-aware drift (unc_rate / sigma_m / cap_ceiling) already computed for the equity fan.
    ttt_result, ttt_fig = _time_to_target(base, unc_rate, sigma_m, cap_ceiling, target)

    # fig/fan/cap are already styled PLAIN-DICT figures (no go.Figure -> ~0 build cost). The rr/dist/ruin
    # figures depend ONLY on the Kelly slider, so they moved to their own callback (_sandbox_risk) and no
    # longer rebuild on every edge/city keystroke.
    return (f"${total:,.0f}", f"{roi:+.1f}%", {"color": roi_color}, kelly_band, note, metrics,
            fig, fan, cap, cap_flag, ttt_result, ttt_fig)


@app.callback(
    Output("sb-rr", "figure"), Output("sb-dist", "figure"), Output("sb-ruin", "figure"),
    Input("sb-kelly", "value"))
def _sandbox_risk(kelly):
    """RISK figures that depend ONLY on the Kelly slider (the validated sweep + the 'your pick' markers), split
    out of _sandbox so they do NOT rebuild on every edge/city/trade keystroke. The static MC curves (frontier
    + drawdown/ruin probabilities) are precomputed once in _risk_static(); only the markers/lines move."""
    try:
        kelly = min(KELLY_MAX, max(0.25, float(kelly)))
    except (TypeError, ValueError):
        kelly = KELLY_CEILING
    k = kelly_interp(kelly)
    rs = _risk_static()
    fr, med, dd = rs["fr"], rs["med"], rs["dd"]

    # ---- (b) risk vs return curve across fractions (static frontier + moving 'your pick') ----
    rr = _dfig(
        [{"type": "scatter", "x": dd, "y": med, "mode": "lines+markers+text", "name": "Kelly frontier",
          "text": [f"{f:.2f}x" for f in fr], "textposition": "top center",
          "textfont": {"size": 10, "color": DIM},
          "line": {"color": CYAN, "width": 2, "shape": "spline", "smoothing": 0.3},
          "marker": {"size": 9, "color": [MINT if f <= KELLY_CEILING else RED for f in fr],
                     "line": {"width": 1, "color": "rgba(255,255,255,.25)"}},
          "hovertemplate": "%{text}<br>median %{y:+.1f}%/m<br>p95 maxDD %{x:.1f}%<extra></extra>"},
         {"type": "scatter", "x": [k["dd"]], "y": [k["med"]], "mode": "markers", "name": "your pick",
          "marker": {"size": 16, "color": AMBER, "symbol": "star", "line": {"width": 1.4, "color": "#fff"}},
          "hovertemplate": f"your pick {kelly:.2f}x<br>median %{{y:+.1f}}%/m"
                           f"<br>p95 maxDD %{{x:.1f}}%<extra></extra>"}],
        h=300, legend=False,
        xaxis={"title": "p95 max drawdown (%)", "ticksuffix": "%"},
        yaxis={"title": "median return (%/month)", "ticksuffix": "%"},
        shapes=[_vline(18.0, NEUTRAL, 1.4, "dash")],
        annotations=[_ann(18.0, 1.0, "0.50x recommended", NEUTRAL, 10)])

    # ---- (c) return distribution + drawdown gauge ----
    _dsev = _sev_color(_sev_dd(k["dd"]))
    spread_x = [k["p5"], k["med"], k["med"] + (k["med"] - k["p5"])]   # p95 ~ symmetric proxy of the band
    dist = {"data": [
        {"type": "indicator", "mode": "gauge+number", "value": k["dd"],
         "number": {"suffix": "%", "font": {"size": 22, "color": _dsev}},
         "title": {"text": "p95 max drawdown", "font": {"size": 12, "color": INK}},
         "gauge": {"axis": {"range": [0, 40], "tickwidth": 1, "tickcolor": AXISCOL,
                            "tickfont": {"size": 9, "color": DIM}},
                   "bar": {"color": _dsev, "thickness": 0.72}, "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
                   "steps": [{"range": [0, 12], "color": "rgba(22,199,132,.12)"},
                             {"range": [12, 20], "color": "rgba(217,162,58,.12)"},
                             {"range": [20, 40], "color": "rgba(234,57,67,.12)"}],
                   "threshold": {"line": {"color": AMBER, "width": 2}, "thickness": 0.85, "value": 18}},
         "domain": {"x": [0.0, 0.42], "y": [0.0, 1.0]}},
        {"type": "bar", "x": ["p5", "median", "p95"], "y": spread_x, "marker": {"color": [RED, MINT, CYAN]},
         "width": 0.6, "text": [f"{v:+.1f}%" for v in spread_x], "textposition": "outside",
         "cliponaxis": False, "xaxis": "x2", "yaxis": "y2",
         "hovertemplate": "%{x}: %{y:+.1f}%/m<extra></extra>"}],
        "layout": {
            "title": None, "template": "plotly_dark", "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)", "font": {"color": INK, "family": "Inter, system-ui", "size": 12},
            "height": 300, "showlegend": False, "margin": {"l": 10, "r": 20, "t": 20, "b": 40},
            "xaxis2": {"domain": [0.56, 1.0], "anchor": "y2", "tickfont": {"size": 11, "color": DIM},
                       "showgrid": False, "linecolor": GRIDCOL},
            "yaxis2": {"anchor": "x2", "title": "%/month", "ticksuffix": "%",
                       "tickfont": {"size": 11, "color": DIM}, "gridcolor": GRIDCOL, "griddash": "dot",
                       "zerolinecolor": AXISCOL, "title_font": {"size": 11, "color": DIM}},
            "annotations": [{"x": 0.78, "y": 1.08, "xref": "paper", "yref": "paper", "showarrow": False,
                             "text": "Monthly return spread", "font": {"size": 12, "color": INK}}]}}

    # ---- (e) P(maxDD>=25%) and P(ruin=DD>=50%) vs Kelly fraction (static curves + moving 'your pick') ----
    fr_grid, p_dd25, p_ruin = rs["fr_grid"], rs["p_dd25"], rs["p_ruin"]
    DD25, RUIN_DD = rs["DD25"], rs["RUIN_DD"]
    ruin = _dfig(
        [{"type": "scatter", "x": fr_grid, "y": [p * 100 for p in p_dd25], "mode": "lines+markers",
          "name": f"P(max drawdown ≥ {int(DD25*100)}% in 12mo)",
          "line": {"color": AMBER, "width": 2.4}, "marker": {"size": 6},
          "hovertemplate": "Kelly %{x:.2f}x<br>P(maxDD≥25%) %{y:.0f}%<extra></extra>"},
         {"type": "scatter", "x": fr_grid, "y": [p * 100 for p in p_ruin], "mode": "lines+markers",
          "name": f"P(ruin, ≥{int(RUIN_DD*100)}% DD in 12mo)",
          "line": {"color": RED, "width": 2.6}, "marker": {"size": 6},
          "hovertemplate": "Kelly %{x:.2f}x<br>P(ruin) %{y:.1f}%<extra></extra>"}],
        h=300, legend=True,
        xaxis={"title": "Kelly fraction", "dtick": 0.25},
        yaxis={"title": "probability (%)", "ticksuffix": "%", "rangemode": "tozero"},
        shapes=[_vrect(0.50, 0.75, "rgba(217,162,58,.08)"), _vrect(0.75, 1.00, "rgba(234,57,67,.08)"),
                _vline(kelly, GREEN, 1.6, "dash"), _vline(0.50, NEUTRAL, 1.2, "dot")],
        annotations=[_ann(kelly, 1.0, f"your pick {kelly:.2f}x", GREEN, 10),
                     _ann(0.50, 0.0, "0.50x ceiling", DIM, 9, xanchor="right")])
    return rr, dist, ruin


def _sev_color(sev):
    return {"good": MINT, "warn": AMBER, "bad": RED}[sev]


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
