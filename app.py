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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dash
import dash_auth
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, dash_table, Input, Output, State, ALL, ctx

from data import table, meta_value

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

NAV = [("overview", "◉", "Overview"), ("markets", "❖", "Markets / Live"),
       ("bankroll", "$", "$1,000 Run"),
       ("forecasts", "☉", "Forecasts"), ("edges", "↑", "Edges"),
       ("multicity", "▦", "Multi-City"), ("accuracy", "◎", "Forecast Accuracy"),
       ("quantlab", "⊞", "Quant Lab"), ("forward", "✓", "Forward Validation"),
       ("scalability", "⤢", "Scalability"),
       ("sandbox", "⚙", "Sandbox"), ("risk", "⚠", "Risk & Honesty"),
       ("methodology", "≡", "Methodology")]


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
    return card([html.H3("Fills-Realism Waterfall — Quoted Edge to Realized Net"),
                 _cap("Per paper stream: the gross model edge at top-of-book, minus modeled fee and minus "
                      "VWAP slippage, lands at the settled realized net. Net is paper/backtest c/contract "
                      "on a thin settled sample (n shown) — never realized P&L. Negative streams (S3/S3early) "
                      "show the fills reality: a quoted edge does not survive frictions."),
                 html.Div(figs, className="grid12")])


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
    return card([html.H3("Market-Divergence — Where We Fired vs the Market, by Outcome"),
                 _cap("Each point is a scanned contract: our model P(yes) vs the market mid, colored by "
                      "settled outcome (green = won, red = lost, dim = not yet settled). The strategy "
                      "(S1/S3/S3early) is in the hover. On the dashed agreement line we have no view; the "
                      "green band (model above market) is our buy-YES edge zone, red the opposite. "
                      "Paper/forward scans only."),
                 graph(_tpl(fig, h=360))])


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
    return card([html.H3("Edge vs Outcome — Does a Bigger Edge Pay?"),
                 _cap("Each settled paper signal: model edge magnitude (x) vs realized paper NET per contract "
                      "(y), colored by win/loss; the grey line is the binned mean. Success is paper RETURN "
                      "(EV), NOT hit-rate — a big-edge longshot can lose often yet pay, so we measure dollars, "
                      f"not wins. n={len(d)} settled, paper/forward, thin — directional only.{rtxt}"),
                 graph(_tpl(fig, h=340))])


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
    return card([html.H3("Brier Decomposition (Murphy) — Why the Model Scores Well"),
                 _cap(f"{verdict}Brier = reliability − resolution + uncertainty over n={n} settled paper "
                      f"signals. RED reliability is a penalty (low = calibrated); GREEN resolution is a "
                      f"credit (high = discriminates winners from losers); NEUTRAL uncertainty is the "
                      f"irreducible base-rate term, shared by both. Small sample — paper/backtest, the same "
                      f"settled set the forward gates accumulate."),
                 graph(_tpl(fig, h=340))])


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
                    fill="toself", fillcolor="rgba(74,144,184,.08)", line=dict(width=0), mode="lines",
                    name="±2σ", hoverinfo="skip")
    fig.add_scatter(x=list(d["date"]) + list(d["date"])[::-1], y=list(d["hi1"]) + list(d["lo1"])[::-1],
                    fill="toself", fillcolor="rgba(74,144,184,.16)", line=dict(width=0), mode="lines",
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
    return card([html.H3("Forecast Fan — Predictive Band vs Realized High"),
                 _cap(f"Deployed day-ahead forecast (σ=1.66°F one-day-ahead) with ±1σ/±2σ predictive bands; "
                      f"dots are the realized settlement high. Over these {len(d)} days {100*cov:.0f}% of "
                      f"realized highs land inside ±1σ (well-calibrated band ≈ 68%). Backtest replay."),
                 graph(_tpl(fig, h=340))])


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
    return card([html.H3("Settlement-Surprise Calendar — Forecast Closeness"),
                 _cap(f"GitHub-style date grid colored by how CLOSE the forecast was on each of ~{len(d)} "
                      f"settled days. The color scale is HIGH-RESOLUTION in the 0–3°F band (green → teal → "
                      f"yellow-green → amber) so small day-to-day deviations are distinguishable, with RED "
                      f"reserved for larger |error| (>3°F) surprise days; slate-gray = no settled data. Mean "
                      f"absolute error {mae:.2f}°F. Red clusters reveal regime surprises. Backtest."),
                 graph(_tpl(fig, h=240, legend=False))])


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
    return card([html.H3(["Trade Blotter — Recent Settled Paper Signals  ", info_dot()]),
                 _cap("The last settled paper signals across streams/cities: model edge at entry, effective "
                      "entry price, realized paper net, and win/loss. Individual outcomes are noisy (small "
                      "stakes, thin sample); the edge lives in the average, not any one row. Paper/forward."),
                 pro_table(show, present_df=False, align_left=("Win",))])


def panel_funnel():
    """Signal funnel: candidate scans -> disagreement -> filters -> fillable -> net-positive."""
    d = table("funnel")
    if d.empty:
        return card([html.H3("Signal Funnel"), empty_state("Fills when edge scans log.")])
    top = max(d["count"].max(), 1)
    fig = go.Figure(go.Funnel(y=d["stage"], x=d["count"], textposition="inside",
                              textinfo="value+percent initial",
                              marker=dict(color=[MINT, CYAN, VIOLET, AMBER, "#7fb0a0"]),
                              connector=dict(line=dict(color=GRIDCOL, width=1)),
                              hovertemplate="%{y}<br>%{x} signals<extra></extra>"))
    fig.update_layout(title=None, margin=dict(l=160, r=20, t=10, b=20))
    return card([html.H3("Signal Funnel — Candidate to Net-Positive"),
                 _cap("How many scanned contracts survive each gate: a model disagreement, the spread+size "
                      "filters, depth/fillability, and finally a settled net-positive outcome. Most "
                      "candidates are filtered out by design — selectivity is the point. Paper counts."),
                 graph(_tpl(fig, h=300, legend=False))])


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
                        line=dict(color=color, width=2, shape="spline", smoothing=0.4),
                        marker=dict(size=4),
                        hovertemplate=strat + " · signal %{x}<br>running net %{y:+.2f} c/ct<extra></extra>")
    fig.add_hline(y=0, line=dict(color=AXISCOL, width=1, dash="dot"))
    fig.update_layout(title=None)
    fig.update_yaxes(title="running mean net (c / contract)", ticksuffix="c", tickformat="+.0f")
    fig.update_xaxes(title="settled signal # (chronological)")
    return card([html.H3("Edge Decay — Running Mean Net per Stream"),
                 _cap("Cumulative-mean realized net as each settled paper signal lands, per stream. A line "
                      "drifting toward or below zero is an edge decaying or never-real (the early-warning we "
                      "want before committing). Thin samples — directional, not conclusive. Paper/backtest."),
                 graph(_tpl(fig, h=300))])


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
    return card([html.H3("Lock-In Latency — Distribution vs the 128s Floor"),
                 _cap(f"Seconds between the :51 KNYC observation and when the priced orderbook updates "
                      f"(n={len(v)} paper scans). Median {med:.0f}s and {100*frac:.0f}% sit at/above the ~128s "
                      f"METAR floor — confirming NYC lock-in is a latency artifact of KNYC's slow feed, not a "
                      f"fat edge. No faster free KNYC source exists."),
                 graph(_tpl(fig, h=300, legend=False))])


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
    """Per-city Brier skill-score radial gauges: model vs market (skill = 1 - model/market)."""
    d = table("brier_gauge")
    if d.empty:
        return card([html.H3("Brier Skill Gauges"), empty_state("Fills from the multi-city edge run.")])
    d = d.sort_values("skill", ascending=False).reset_index(drop=True)
    n = len(d)
    cols = min(n, 6)
    fig = go.Figure()
    for i, (_, r) in enumerate(d.iterrows()):
        sk = float(r["skill"]) * 100.0
        col = MINT if sk > 0 else RED
        fig.add_trace(go.Indicator(
            mode="gauge+number", value=sk,
            number={"suffix": "%", "font": {"size": 20, "color": col}},
            title={"text": f"{r['city']}", "font": {"size": 12, "color": INK}},
            gauge={"axis": {"range": [-8, 8], "tickwidth": 1, "tickcolor": AXISCOL,
                            "tickfont": {"size": 8, "color": DIM}},
                   "bar": {"color": col, "thickness": 0.7},
                   "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
                   "steps": [{"range": [-8, 0], "color": "rgba(234,57,67,.10)"},
                             {"range": [0, 8], "color": "rgba(22,199,132,.10)"}],
                   "threshold": {"line": {"color": DIM, "width": 1.5}, "thickness": 0.8, "value": 0}},
            domain={"row": 0, "column": i}))
    fig.update_layout(grid={"rows": 1, "columns": cols, "pattern": "independent"},
                      template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", margin=dict(l=10, r=10, t=30, b=10), height=180,
                      font=dict(color=INK, family="Inter, system-ui"))
    return card([html.H3("Brier Skill vs Market — Per City"),
                 _cap("Brier skill score = 1 − (model Brier ÷ market Brier) on ALL settled day-ahead "
                      "contracts; positive (green) = our probabilities beat the market on aggregate accuracy. "
                      "Only NY clears on this raw-skill aggregate — the validated multi-city S1 edge comes "
                      "from the market's overconfidence on selected contracts, not raw skill everywhere. "
                      "Honest framing, paper/backtest."),
                 graph(fig)])


# ============================================================================================
# QUANT-TERMINAL PANELS (Iris 2026-06-19): dense live/terminal + Quant Lab graphics. Green/red/neutral
# ONLY. Each reads ONE curated table, guards its own empty case, and carries an honest paper caption.
# ============================================================================================
def _spark(values, color=None, height=34, fill=True):
    """Tiny inline sparkline (no axes/grid/hover-bar). Green if last>=first else red unless color given."""
    if not values or len(values) < 2:
        return html.Div(className="spark-empty", style={"height": f"{height}px"})
    color = color or (GREEN if values[-1] >= values[0] else RED)
    fig = go.Figure()
    fig.add_scatter(y=values, mode="lines", line=dict(color=color, width=1.6, shape="spline", smoothing=0.5),
                    fill="tozeroy" if fill else None,
                    fillcolor=f"rgba({_rgb(color)},.10)" if fill else None, hoverinfo="skip")
    fig.update_layout(margin=dict(l=0, r=0, t=2, b=2), height=height, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
    fig.update_xaxes(visible=False, fixedrange=True)
    fig.update_yaxes(visible=False, fixedrange=True)
    return dcc.Graph(figure=fig, config={"displayModeBar": False, "staticPlot": True},
                     style={"height": f"{height}px"})


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
    # cumulative paper backtest net (cents) -> the "total return (paper)" headline, clearly backtest
    cum = float(eq["equity_c"].iloc[-1]) if not eq.empty else None
    cards = [
        kpi_spark_card("NY DAY-AHEAD RMSE", ny_rmse, "F", "WALK-FORWARD", "latency_s",
                       spark_color=NEUTRAL),
        kpi_spark_card("NY S1 EDGE (S2X)", ny_edge, "c/contract", "BACKTEST", "ny_edge_c"),
        kpi_spark_card("BEST STREAM EDGE", best_edge, "c/contract", "BACKTEST", "monthly_net_c"),
        kpi_spark_card("CITIES BEAT MARKET", cities, "cities", "BACKTEST", None),
        kpi_spark_card("PAPER NET (BACKTEST)", cum, "c/contract", "BACKTEST", "equity_c",
                       tip=PAPER_NET_TIP),
        kpi_spark_card("LIVE CAPITAL", 0, "$", "PAPER", None),
    ]
    return html.Div(cards, className="grid kpi-strip")


# ---------- Markets / Live page panels ----------
def panel_market_feed(compact=False):
    """Live-ish Kalshi market feed: recent scanned quotes per city/market (time/city/market/quote/edge/side).
    Honest: these are PAPER scans of public quotes, NOT orders. Source: forward monitor logs."""
    d = table("market_feed")
    if d.empty:
        return card([html.H3("Live Market Feed"), empty_state("Fills as the paper monitors scan quotes.")])
    show = present(d, drop=["status"],
                   rename={"scan_utc": "Time", "quote_c": "Quote", "model_p": "Model P", "edge_c": "Edge"},
                   fmt={"quote_c": lambda v: "—" if _isnull(v) else f"{v:.0f}c",
                        "model_p": lambda v: "—" if _isnull(v) else f"{v:.0%}",
                        "edge_c": _cents1},
                   order=["scan_utc", "city", "market", "ticker", "side", "quote_c", "model_p", "edge_c"])
    return card([html.H3("Live Market Feed — Paper Quote Scans"),
                 _cap("Most-recent public Kalshi quotes the paper monitors scanned, per city/market: the "
                      "quoted entry, our model probability, and the signed edge. PAPER scans of public "
                      "data — never orders, never real money. Time is UTC."),
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
            html.Span(str(r["scan_utc"]), className="ss-t mono"),
            html.Span(str(r["city"]), className="ss-c"),
            html.Span(str(r["market"]), className="ss-m"),
            html.Span(str(r["side"]) if not _isnull(r["side"]) else "—", className="ss-s"),
            html.Span(str(r["ticker"]) if not _isnull(r["ticker"]) else "—", className="ss-tk mono"),
            html.Span(ev, className=f"ss-e mono {ecls}")], className="ss-row"))
    return card([html.H3(["Scan Stream — Last 14 Paper Scans  ", html.Span(className="stream-pulse")]),
                 _cap("Rolling tape of the most recent public-quote scans across the forward monitors (UTC). "
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
    return card([html.H3("City Network — Day-Ahead Forecast & Paper Edge"),
                 _cap("The seven Kalshi daily-high cities at their settlement stations. Node size = "
                      "validated paper S1 edge magnitude; color = deployed status (green tradable / amber "
                      "watch / slate not-deployed). Arcs are illustrative. Paper/forward, never live P&L."),
                 graph(fig)])


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
            badge(str(r["status"]).upper().replace("-", " "),
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
    return card([html.H3(["Strategy Performance — Paper Streams  ", info_dot()]),
                 _cap("Every paper stream: validated edge (c/contract), win-rate, profit factor, deploy "
                      "status, and the honest one-line note. TRADABLE = live paper signal; WATCH = logged "
                      "not trusted; DEPRIORITIZED = real but not bankable. Paper/backtest, never live P&L."),
                 pro_table(show, present_df=False, align_left=("Strategy", "Status", "Note"))],
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
                    line=dict(color=eqcol, width=2.2, shape="spline", smoothing=0.4),
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
    return card([html.H3("Backtest Equity Curve — Leak-Free Walk-Forward S1"),
                 _cap(f"CUMULATIVE paper net from trading ONE contract of every settled S1 signal, summed in "
                      f"order over {len(d)} settled days ({d0} to {d1}). It ends at {last:+,.0f}c "
                      f"(about {last/100:+.2f} dollars total) — a running SUM across all those contracts, so "
                      f"each new day's settled contracts ADD to it; it is NOT a per-contract average and NOT "
                      f"annualised. The dashed zero line is the no-edge baseline (take every quote at the mark). "
                      f"This is BACKTEST research in cents/contract; the DEPLOYED $1,000 paper run with real "
                      f"Kelly sizing lives on the \"$1k Run\" page (different measure — don't conflate). "
                      f"Cents/contract, never realized P&L."),
                 graph(_tpl(fig, h=360))])


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
    return card([html.H3("Drawdown — Underwater Curve"),
                 _cap(f"Peak-to-trough underwater of the backtest equity, in cents/contract. Max backtest "
                      f"drawdown {maxdd:+.0f} c/contract. Drawdowns are part of any real edge — the curve "
                      f"recovers, but losing stretches happen. Backtest, never live."),
                 graph(_tpl(fig, h=240, legend=False))])


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
    return card([html.H3("Monthly Returns Distribution"),
                 _cap(f"Paper net per contract summed by calendar month from the walk-forward backtest. "
                      f"{pos} of {tot} months positive (green). The edge lives in the average — individual "
                      f"months swing, including losers. Backtest, never realized P&L."),
                 graph(_tpl(fig, h=300, legend=False))])


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
    return card([html.H3("Model Comparison — EMOS Variants"),
                 _cap("Leak-free out-of-sample scores by model variant (lower RMSE/CRPS/log-score is better; "
                      "90% coverage should be ≈90%). The deployed EMOS-full wins — which is why it ships. "
                      "Backtest, strictly out-of-sample."),
                 pro_table(show, present_df=False, align_left=("Model",))], id="model-compare-card")


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
    return card([html.H3("Daily-Low S1 Edge — the Orthogonal Overnight Book"),
                 _cap("Validated daily-LOW S1 net per city with 95% bootstrap CIs (green = TRADABLE, amber = "
                      "WATCH). The overnight-low market is roughly orthogonal to the daily high — a real "
                      "diversifier. The edge concentrates in the cold season; recent-quarter is the forward "
                      "decay watch. Paper/backtest, never realized P&L."),
                 graph(_tpl(fig, h=320, legend=False))])


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
    return html.Span("ⓘ", className="info-dot", title=tip)


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
            t["__dt"] = pd.to_datetime(t["ts"], utc=True, errors="coerce")
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
        t["__dt"] = pd.to_datetime(t["ts"], utc=True, errors="coerce")
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
        t["dt"] = pd.to_datetime(t["ts"], utc=True, errors="coerce")
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
        m["dt"] = pd.to_datetime(m["ts"], utc=True, errors="coerce")
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
        dd["dt"] = pd.to_datetime(dd["date"], utc=True, errors="coerce")
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
                mdt = pd.to_datetime(m0["ts"], utc=True, errors="coerce")
                if pd.notna(mdt):
                    out.append({"dt": mdt, "equity": float(m0["equity"]),
                                "drawdown": (None if pd.isna(m0.get("drawdown")) else float(m0["drawdown"]))})
            out.sort(key=lambda r: r["dt"])
            return out, "curve"
    # last resort: a single flat marks row (or nothing) -> one point so the chart still renders
    if not marks.empty and "ts" in marks.columns:
        m0 = marks.iloc[0]
        mdt = pd.to_datetime(m0["ts"], utc=True, errors="coerce")
        if pd.notna(mdt):
            return [{"dt": mdt, "equity": float(m0["equity"]),
                     "drawdown": (None if pd.isna(m0.get("drawdown")) else float(m0["drawdown"]))}], "marks"
    return [], "none"


def _equity_figure(window_label):
    """Build the windowed $1k paper-equity figure + the (raw $ change, % change) readout for `window_label`.
    Returns (figure, readout_children, readout_color). Time-aware x (HH:MM intraday, dates for long windows).
    Graceful with 1-2 points (renders a marker/short line, never crashes). PAPER equity, hypothetical."""
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
    win = _downsample_list(win, 600)   # PERF: cap plotted points (keeps first/last -> raw/% change exact)
    xs = [_to_et_naive(p["dt"]) for p in win]   # ET wall-clock display (was UTC)
    ys = [p["equity"] for p in win]
    dds = [p["drawdown"] for p in win]
    first_eq, last_eq = ys[0], ys[-1]
    raw = last_eq - first_eq
    pct = (100.0 * raw / first_eq) if first_eq else 0.0
    col = GREEN if raw >= 0 else RED
    # SMOOTH spline for the live realized+unrealized timeline; hv-step for realized-only marks; linear else.
    if source == "timeline":
        line_kw = dict(color=col, width=2.4, shape="spline", smoothing=0.4)
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
    mode = "lines+markers" if len(win) > 1 else "markers"
    # for the dense intraday timeline drop the per-point markers so the spline reads cleanly
    if source == "timeline" and len(win) > 40:
        mode = "lines"
    fig.add_scatter(x=xs, y=ys, mode=mode, name="paper equity",
                    line=line_kw, marker=dict(size=6, color=col), customdata=cd,
                    hovertemplate=hovertmpl)
    fig.add_hline(y=1000, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                  annotation_text="$1,000 baseline", annotation_position="bottom right",
                  annotation_font=dict(color=NEUTRAL, size=10))
    span = (max(ys) - min(ys)) or 1.0
    lo = min(min(ys), 1000) - span * 0.6
    hi = max(max(ys), 1000) + span * 0.6
    fig.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.2f", range=[lo, hi])
    # time-aware x: HH:MM for intraday windows, dates otherwise (proper datetime axis -> Plotly auto-formats)
    if window_label in _EQ_INTRADAY:
        fig.update_xaxes(title=None, tickformat="%H:%M", type="date")
    else:
        fig.update_xaxes(title=None, tickformat="%b %d", type="date")
    readout = html.Span([
        html.Span(f"{'+' if raw >= 0 else '−'}${abs(raw):,.2f}", className="mono",
                  style={"fontWeight": "800"}),
        html.Span(f"  ·  {pct:+.2f}%", className="mono"),
        html.Span(f"  ({window_label})", className="sub", style={"marginLeft": "4px"})],
        style={"color": col, "fontSize": "15px"})
    return _tpl(fig, h=240, legend=False), readout, col


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
    return card([html.H3(f"Paper Equity — {cur_str}"),
                 _cap(f"The $1,000 PAPER bankroll, RESET to $1,000 on {reset_date} (the algorithm changed; the "
                      f"prior track is archived). PAPER equity = $1,000 reset baseline − invested + the current "
                      f"value of open positions (unrealized public-quote mark-to-market) — so the curve moves "
                      f"CONTINUOUSLY with quotes from a $1,000 start, not just at settlements. Current paper "
                      f"equity {cur_str} ({'+' if delta >= 0 else '−'}${abs(delta):,.2f} vs the $1,000 baseline; "
                      f"max paper drawdown {dd}%). Pick a time window below; the readout shows the $ and % change "
                      f"over it. Source: {src_note}. LIVE real-deploy capital = $0 — promotion to REAL still "
                      f"requires a forward-gate PASS. Paper / hypothetical, never realized P&L."),
                 selector,
                 html.Div(readout, id="run-equity-readout", style={"marginBottom": "4px"}),
                 dcc.Graph(id="run-equity-graph", figure=fig, config={"displayModeBar": False})])


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
    return card([html.H3("Cash vs Positions"),
                 _cap("How the $1,000 paper net equity splits. CASH = uninvested bankroll ($1,000 + realized − "
                      "cost of open positions). IN POSITIONS = current market value of what we hold (cost + "
                      "unrealized). The two always sum to net equity. REALIZED P&L is locked in once a market "
                      "SETTLES; UNREALIZED is the still-moving paper mark on positions we still hold (not real "
                      "until they settle). Paper only — never real money."),
                 body])


# ---- TASK B (2026-06-21): value-vs-paid intraday curve per RESOLUTION DATE, with a current/next toggle ----
def _resolution_dates():
    """The DISTINCT resolution_date values from resolution_day_curve, sorted ASCENDING. first = current day,
    second = next day. Data-derived (never hardcoded). Returns a list of YYYY-MM-DD strings (possibly empty)."""
    d = table("resolution_day_curve")
    if d.empty or "resolution_date" not in d.columns:
        return []
    vals = sorted(str(v) for v in d["resolution_date"].dropna().unique())
    return vals


def _resday_summary(resolution_date):
    """Latest-ts totals for one resolution date from resolution_day_curve: dict(paid$, value$, net$, pct,
    n_pos, n_ct) or None. The last ts row holds the current cumulative paid / value / contracts."""
    d = table("resolution_day_curve")
    if d.empty or "resolution_date" not in d.columns or "ts" not in d.columns:
        return None
    sel = d[d["resolution_date"].astype(str) == str(resolution_date)]
    if sel.empty:
        return None
    last = sel.sort_values("ts").iloc[-1]
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
    sel["dt"] = pd.to_datetime(sel["ts"], utc=True, errors="coerce")
    sel = sel.dropna(subset=["dt"]).sort_values("dt")
    if sel.empty:
        return _tpl(fig, h=300, legend=False)
    sel["dt"] = sel["dt"].dt.tz_convert(_DISPLAY_TZ).dt.tz_localize(None)   # ET wall-clock display (was UTC)
    sel = _downsample_df(sel, 500)     # PERF: cap dense grids (today's curve was ~12.5k pts x 5 traces)
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


def _resday_section(resolution_date, idx):
    """One FULL section per resolution date: header + day tag, a summary metric row (paid / current value /
    net / positions / contracts) and the full cumulative value-vs-paid chart."""
    s = _resday_summary(resolution_date)
    tag = "TODAY" if idx == 0 else ("NEXT DAY" if idx == 1 else "")
    metrics = _resday_metric_row(s) if s else html.Div()
    hdr = html.Div([html.H3(f"Resolving {resolution_date}",
                            style={"display": "inline-block", "margin": "0 8px 0 0"}),
                    (badge(tag, "good" if idx == 0 else "neut") if tag else html.Span())],
                   style={"display": "flex", "alignItems": "baseline", "marginBottom": "2px"})
    return card([hdr, metrics, graph(_resolution_day_figure(resolution_date))],
                style={"marginBottom": "10px"})


def panel_resolution_day_curve():
    """Per-resolution-day FULL SECTIONS (USER ASK 2026-06-22; was a single current/next toggle): a BOOK-TOTAL
    header (all open resolution days) + for EACH open resolution date a header + summary (paid / current value /
    net / positions / contracts) + the full cumulative value-vs-paid chart over [day-before 00:01 ->
    resolution-day 23:59]. PAPER: value = public-quote mark (or settled payout once resolved), not realized P&L."""
    dates = _resolution_dates()
    if not dates:
        return card([html.H3("Positions Resolving — Cumulative Value vs Paid"),
                     empty_state("Fills from the resolution_day_curve table once positions are entered.")])
    intro = _cap("A BOOK TOTAL across all open resolution days, then one section PER resolution date. The "
                 "prominent line = cumulative VALUE of the positions resolving that day (fluctuates with public "
                 "quotes, or the settled payout once resolved); the dotted line = cumulative PRICE PAID (cost "
                 "basis, steps up as positions are entered). Band is GREEN when value > paid, RED when under. "
                 "Each spans 12:01 AM the day before through 11:59 PM the resolution day (ET). PAPER / UNREALIZED — "
                 "$0 real, no orders.")
    bs = _resday_book_summary(dates)
    book = []
    if bs:
        book = [card([html.Div([html.H3("All open resolution days — book total",
                                        style={"display": "inline-block", "margin": "0 8px 0 0"}),
                                badge(f"{len(dates)} days", "neut")],
                               style={"display": "flex", "alignItems": "baseline", "marginBottom": "2px"}),
                      _resday_metric_row(bs, big=True)],
                     style={"marginBottom": "12px",
                            "borderColor": "color-mix(in srgb, var(--mint) 35%, transparent)"})]
    sections = [_resday_section(dt, i) for i, dt in enumerate(dates)]
    return html.Div([section("Positions Resolving — Value vs Paid (per day)"), intro] + book + sections)


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
    return card([html.H3("Projected Paper-Equity Fan — 12-Month Monte-Carlo"),
                 _cap("Paper projection (model estimate, NOT realized). Monte-Carlo of the activated 7-edge "
                      "book at 0.50x Kelly; the P5 / median / P95 BANDS propagate MONTHLY-RETURN VARIANCE ONLY "
                      "(they assume the edges hold, and widen with time). The dashed amber STRESS line is "
                      "different: it propagates EDGE UNCERTAINTY by compounding the stress %/mo. The run RESET "
                      f"to $1,000 on {_run_meta('reset_date', '2026-06-21')} (algorithm changed; prior track "
                      "archived) — month 0 = $1,000 (YOU ARE HERE). Inputs: median "
                      f"{100*med_mo:+.1f}%/mo, P5 {100*p5_mo:+.1f}%/mo, P95 {100*p95_mo:+.1f}%/mo -> after 12 "
                      f"paper months the median path reaches ${end_med:,.0f} (P5 ${end_p5:,.0f} / P95 "
                      f"${end_p95:,.0f}).{stress_txt} Activated AHEAD of the forward gate; paper $1,000 only, "
                      "$0 REAL, never realized P&L."),
                 graph(_tpl(fig, h=320))], id="run-projection-card")


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
    return card([html.H3("Deploy-Gate Board — Active vs Staged, and What Unlocks REAL Capital"),
                 _cap(f"Each paper stream shows its PAPER state ({n_active} green ACTIVE · PAPER vs the rest "
                      "STAGED), the SPECIFIC pre-registered gate it must pass (docs/FORWARD_PROTOCOL A2/A3/A4), "
                      "forward progress, and its stake. ACTIVE = user-activated in the $1,000 PAPER run AHEAD "
                      "of the gate (paper money only). Activation is ORTHOGONAL to the gate — every row is "
                      "still ACCUMULATING, shown beside its stake. REAL deployment ($0 live) still requires a "
                      "gate PASS. WATCH · NO PATH = validated-but-not-promotable. Paper/forward only, never "
                      "realized P&L."),
                 html.Div(rows, className="gb-grid")], id="gate-board-card")


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
        if len(seq) < 2:
            # one point (or none) -> a single dot so the cell is non-empty + honest about thin history
            pts = seq or ([row.get("entry_price")] if not _isnull(row.get("entry_price")) else [])
            lbl = f"{pts[0]:.0f}c" if pts else "—"
            return html.Span(["• ", html.Span(lbl, className="mono sub")],
                             title="accumulating price snapshots (grows each run)")
        return _spark(seq, height=22, fill=False)
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
    return card([html.H3(["Pending Paper Trades — Open, Unsettled Signals  ", info_dot(
                    "Paper signals the monitors have LOGGED but that have NOT yet settled. The Entry → Current "
                    "column is a PAPER MARK, UNREALIZED — not real money, not realized P&L, not a real "
                    "position. No orders, no account. Current = live public YES-mid for the side held.")]),
                 _cap(f"{n} paper signals logged across the forward monitors but NOT yet settled, plotted by "
                      f"target settle date and model edge (circle = YES side, diamond = NO). "
                      f"Contracts = the staged $1,000-harness size and Paid ($) = the cost basis per position "
                      f"(contracts × entry price; paper stake, no real orders). The "
                      f"Entry → Current column marks each paper signal to the live public quote with a green "
                      f"up / red down chip. The Net-Per-Day Swing below AGGREGATES across ALL open paper "
                      f"contracts per calendar day (sum of entry cost vs sum of current marks) — a PAPER, "
                      f"UNREALIZED mark, NOT real money or realized P&L. Once signals settle they feed the "
                      f"gate-progress counters above."),
                 graph(_tpl(fig, h=300)) if not scat.empty else html.Div(),
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


def empty_state(msg, icon="◴"):
    """Standard empty panel: icon + 'fills when X runs' message."""
    return html.Div([html.Div(icon, className="es-ic"), html.Div(msg, className="es-msg")],
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
        return f"${value:,.0f}", ""
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
                          html.Div(badges, style={"display": "flex", "gap": "5px", "flexWrap": "wrap"})],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                                "flexWrap": "wrap", "gap": "4px"}),
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
                          html.Div(badges, style={"display": "flex", "gap": "5px", "flexWrap": "wrap"})],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                                "flexWrap": "wrap", "gap": "4px"}),
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
                          html.Div(badges, style={"display": "flex", "gap": "5px", "flexWrap": "wrap"})],
                         style={"display": "flex", "justifyContent": "space-between", "alignItems": "center",
                                "flexWrap": "wrap", "gap": "4px"}),
                html.Div(lines, style={"padding": "14px 4px 6px"})],
                className="col-6 card",
                style={"borderColor": "color-mix(in srgb, var(--amber) 22%, transparent)"}))
    children = [html.H3("Scalability Curve — Net Edge vs Order Size"),
                _cap("Units: every contract count is PER-MARKET (per-strike, per-day). Each city lists ~2–3 "
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
                     "Paper / public-data read — never realized P&L."),
                html.Div(figs, className="grid12")]
    return card(children)


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
    return card([html.H3("Bankroll Headroom — Where Depth Stops Linear Scaling"),
                 _cap("Contracts here are PER-MARKET (per-strike, per-day). The light ghost bar = contracts a "
                      "flat 5%-of-bankroll stake would want at each tier; the solid bar = what the book actually "
                      "fills within the net-positive size. All three deployed high cities have a measured depth "
                      "cap now (NY-high from its lock-moment curve, LAX-high + CHI-high from the 9-day STANDING-"
                      "book archive — periodic snapshot, not lock-moment): where the solid bar is shorter "
                      "(red ▲), DEPTH binds — more bankroll buys NO extra size and profit plateaus. The "
                      "remaining streams (daily-LOW books + MIA-high watch) show only 'bankroll wants' "
                      "(translucent) because their per-tier depth cap is still ACCRUING (≥25ct confirmed) — we "
                      "do not plot a fabricated cap. Illustrative flat 5% stake, NOT the live Kelly engine. "
                      "Paper / public-data read — never realized P&L."),
                 html.Div(facets, className="grid12")])


# ============================== PAGES ==============================
def render_overview():
    kpi = table("kpi"); br = table("bankroll_run"); cs1 = table("city_s1")
    kpi_strip = kpi_spark_row()
    # bankroll
    if br.empty:
        # $1k DOLLAR curve stays OFF until separate approval. Fill this prime real estate with the REAL
        # forward-evidence graphics instead of a blank placeholder: the signal funnel + the decay/skill.
        _fn = table("funnel")
        if not _fn.empty:
            funnel_content = graph(_tpl(go.Figure(go.Funnel(
                y=_fn["stage"], x=_fn["count"], textposition="inside",
                textinfo="value+percent initial",
                marker=dict(color=[MINT, CYAN, VIOLET, AMBER, "#c07fb0"]),
                connector=dict(line=dict(color=GRIDCOL, width=1)),
                hovertemplate="%{y}<br>%{x} signals<extra></extra>")).update_layout(
                title=None, margin=dict(l=160, r=20, t=10, b=20)), h=260, legend=False))
        else:
            funnel_content = empty_state("Wired and ready. Forward evidence fills as paper signals settle.")
        bank = card([html.H3("Forward Evidence — Signal Funnel"),
                     _cap("The $1,000 paper-run equity curve stays off until separately approved; until then "
                          "this space shows the live forward evidence. Below: how scanned contracts narrow to "
                          "net-positive settled paper signals."),
                     funnel_content])
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
                        hovertemplate="$%{y:,.2f} (paper)<extra></extra>")
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
    # confirmed-cities mini panel
    rev = cs1[cs1["revived"] == 1] if not cs1.empty and "revived" in cs1 else cs1.iloc[0:0]
    chips = [badge(f"{r['city']}  +{r['s1_net_c']:.1f}c", "good") for _, r in rev.iterrows()] or [badge("NY", "good")]
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
    return html.Div([section("Markets / Live — Public Quote Scans (Paper)"),
                     html.Div("Live public market context the bot watches. PAPER scans of public Kalshi "
                              "quotes and public weather feeds — no orders, no account, no real money.",
                              className="sub", style={"marginBottom": "10px"}),
                     html.Div([html.Div(panel_city_network(), className="col-7"),
                               html.Div(panel_city_rank(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_quote_board(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_market_feed(), className="col-7"),
                               html.Div(panel_scan_stream(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_open_positions(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_source_health(), className="col-6"),
                               html.Div(panel_model_drift(), className="col-6")], className="grid12"),
                     html.Div([html.Div(panel_alerts(), className="col-12")], className="grid12")])


def render_quantlab():
    return html.Div([section("Quant Lab — Backtest Research"),
                     html.Div("Leak-free walk-forward backtest diagnostics. Every figure is paper/backtest "
                              "research in cents/contract or °F — NOT dollars, NOT live, never realized P&L.",
                              className="sub", style={"marginBottom": "10px"}),
                     html.Div([html.Div(panel_equity_curve(), className="col-8"),
                               html.Div(panel_model_compare(), className="col-4")], className="grid12"),
                     html.Div([html.Div(panel_drawdown(), className="col-6"),
                               html.Div(panel_monthly_returns(), className="col-6")], className="grid12"),
                     html.Div([html.Div(panel_dailylow_edge(), className="col-7"),
                               html.Div(panel_emos_skill(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_pit(), className="col-6"),
                               html.Div(panel_fan(), className="col-6")], className="grid12"),
                     html.Div([html.Div(panel_scenario(), className="col-12")], className="grid12")])


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


def render_forecasts():
    sf = table("source_forecast")
    if sf.empty:
        return html.Div([section("Forecasts by source"),
                         card("No source-forecast snapshot yet "
                              "(snapshot_source_forecasts.py runs in the pipeline; panel fills shortly)."),
                         html.Div([html.Div(panel_city_source_attribution(), className="col-12")],
                                  className="grid12")])
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
    fig.update_layout(title="Day-Ahead High Forecast by Source (tomorrow, per city)")
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
    return html.Div([section("Forecasts by source"),
                     card([html.H3(f"Ensemble Members · Target {tgt}"),
                           html.Div("The deployed model is an EMOS ensemble; here are the individual member "
                                    "forecasts and their spread per city. Wide spread = high model "
                                    "disagreement (a no-trade signal).", className="sub"),
                           graph(_tpl(fig, h=340))]),
                     html.Div([html.Div(panel_city_source_attribution(), className="col-12")],
                              className="grid12"),
                     card([html.H3("Per-Source Detail (°F)"), dt(piv_fmt, present_df=False, page_size=8)])])


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


def render_edges():
    e = table("edge")
    if e.empty:
        return html.Div([section("Edges"), card("No edge data yet.")])
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
        fig.update_layout(title="S1 Edge by City (S2X model) — color = coherent edge criterion",
                          barmode="overlay")
        fig.update_yaxes(title="S1 avg net (cents / contract)", ticksuffix="c", tickformat="+.1f")
    caption = ("Color encodes BOTH tests, so nothing above zero looks like flat 'no edge'. "
               "Green = beats the market on Brier AND has positive net (the trustworthy edge). "
               "Amber = positive net but does NOT beat market (ambiguous, not a clean edge). "
               "Cyan = beats market but net is not positive. Red = negative net and no Brier edge. "
               "Net is paper/backtest c/contract after modeled fills; never realized P&L.")
    body = [section("Edges"),
            card([html.H3("S1 Edge by City"),
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
    return html.Div(body)


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


def render_multicity():
    cs1 = table("city_s1"); ll = table("lockin_lead")
    blocks = [section("Multi-City Scalability")]
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
        fig.update_layout(title="Per-City S1 Net — 95% Bootstrap CI (color = deployed status)")
        fig.update_yaxes(title="S1 net (cents / contract)", ticksuffix="c", tickformat="+.0f")
        # per-city deployed-status badges (the honesty fix: near-pass CIs != tradable)
        order = ["tradable", "watch", "not-deployed", "not-built"]
        status_chips = []
        for st in order:
            cities = [c for c in d["city"] if d.set_index("city").loc[c, "status"] == st]
            if cities:
                _, kind, lbl = _STATUS_STYLE[st]
                status_chips.append(html.Div([badge(lbl, kind),
                                              html.Span("  " + ", ".join(cities), className="sub")],
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
    # NEW-CITY EXPANSION watchlist (Magellan -> Kelvin -> Falcon -> Verity, Jun 2026). Reflects SEA-high
    # WATCH + the ruled-out cities. BACKTEST probes only; none deployed; SEA is the sole live WATCH candidate.
    blocks.append(card([
        html.H3("New-City Expansion — Candidate Watchlist (Jun 2026 backtest probe)"),
        html.Div([badge("WATCH", "warn"),
                  html.Span("  SEA-high  ", className="sub", style={"fontWeight": "700"}),
                  html.Span("+4.25c dedup, but P(net>0)=.979 fails the k=6 99.17% Bonferroni bar (needs "
                            ".9958); fill-fragile (+1c slippage re-touches 0) and not lambda-robust on the "
                            "deduped pool. Pre-registered promotion gate; not yet logging forward.",
                            className="sub")], style={"marginBottom": "6px"}),
        html.Div([badge("DEAD", "bad"),
                  html.Span("  SFO-high  ", className="sub", style={"fontWeight": "700"}),
                  html.Span("S1 edge vanished on dedup (+2.88c -> +0.46c) — a collinear-pool artifact.",
                            className="sub")], style={"marginBottom": "6px"}),
        html.Div([badge("PARKED", "neut"),
                  html.Span("  PHX / DAL / BOS-high  ", className="sub", style={"fontWeight": "700"}),
                  html.Span("PHX is a strong forecast (RMSE 1.555) but the desert market is too sharp for an "
                            "S1 edge -> lock-in candidate, not S1; DAL/BOS show no market-beating Brier.",
                            className="sub")]),
        html.Div("Backtest probes only (Magellan -> Kelvin -> Falcon -> Verity); none deployed. SEA is the "
                 "sole live WATCH candidate, on a pre-registered forward gate. Paper/forward, never realized "
                 "P&L.", className="sub", style={"marginTop": "8px", "opacity": ".82"}),
    ]))
    if not ll.empty:
        d = ll.copy()
        fig2 = go.Figure()
        fig2.add_bar(x=d["city"], y=d["lead_min"], name="Detection lead", marker_color=CYAN, width=0.62,
                     hovertemplate="<b>%{x}</b><br>Median lead: %{y:.0f} min<extra></extra>")
        fig2.update_layout(title="Airport 5-Min HF Feed — Lock Detection Lead vs Hourly METAR")
        fig2.update_yaxes(title="median lead (minutes)", ticksuffix=" min")
        blocks.append(card([html.H3("Airport Lock-In Channel (the cities KNYC can't match)"),
                            html.Div("Non-NYC cities settle on airport ASOS with a free 5-min HF feed; it "
                                     "detects the locked daily high minutes before the hourly METAR. The gap is "
                                     "thin (markets watch it too) — a capacity story, not a fat edge.",
                                     className="sub"), graph(_tpl(fig2, legend=False)),
                            dt(d, page_size=7)]))
    return html.Div(blocks)


def render_accuracy():
    r = table("forecast_rmse")
    if r.empty:
        return html.Div([section("Forecast Accuracy"), card("No forecast RMSE yet.")])
    m = r.melt(id_vars="city", value_vars=["members_rmse", "s2x_rmse"], var_name="model", value_name="rmse")
    m["model"] = m["model"].map({"members_rmse": "Members-only", "s2x_rmse": "S2X (deployed)"})
    fig = px.bar(m, x="city", y="rmse", color="model", barmode="group",
                 color_discrete_map={"Members-only": DIM, "S2X (deployed)": MINT},
                 title="Day-Ahead RMSE by City — Members-only vs S2X")
    s = r.melt(id_vars="city", value_vars=["warm", "cold"], var_name="season", value_name="rmse")
    s["season"] = s["season"].map({"warm": "Warm season", "cold": "Cold season"})
    fig2 = px.bar(s, x="city", y="rmse", color="season", barmode="group",
                  color_discrete_map={"Warm season": AMBER, "Cold season": CYAN},
                  title="Seasonal RMSE — Warm vs Cold")
    for f in (fig, fig2):
        f.update_yaxes(title="day-ahead RMSE (°F)", ticksuffix="°F")
        f.update_xaxes(title="")
        f.update_traces(hovertemplate="<b>%{x}</b> · %{fullData.name}<br>%{y:.2f}°F<extra></extra>")
    return html.Div([section("Forecast Accuracy"),
                     html.Div([html.Div(card(graph(_tpl(fig))), style={"flex": "1", "minWidth": "380px"}),
                               html.Div(card(graph(_tpl(fig2))), style={"flex": "1", "minWidth": "380px"})],
                              className="grid"),
                     card([html.H3("RMSE Detail (°F)"),
                           dt(present(r, order=["city", "members_rmse", "s2x_rmse", "warm", "cold", "n"]),
                              present_df=False)]),
                     html.Div([html.Div(panel_calibration_streams(), className="col-7"),
                               html.Div(panel_emos_skill(), className="col-5")], className="grid12"),
                     html.Div([html.Div(panel_pit(), className="col-6"),
                               html.Div(panel_brier_decomp(), className="col-6")], className="grid12"),
                     html.Div([html.Div(panel_lead_decay(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_fan(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_surprise(), className="col-12")], className="grid12")])


def render_forward():
    # FIX 3 (2026-06-19): read the SAME run_gates table the $1,000 Run gate board uses, so the two pages
    # AGREE by construction and the NEW pre-registered gate set (A4 daily-low multi-city) is reflected here.
    g = table("run_gates")
    if g.empty:
        return html.Div([section("Forward Validation"),
                         card([html.H3("Pre-registered forward gates"),
                               empty_state("Fills from the $1,000 staged-harness ledger + monitor logs.")])])
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
    return html.Div([section("Forward Validation"),
                     card([html.H3("Pre-registered forward gates"),
                           html.Div(["Thresholds fixed in advance (docs/FORWARD_PROTOCOL.md, gates A2/A3/A4/"
                                     "A4.1). Current pre-registered set: LOCK-IN (latency, deprioritized), "
                                     "S1-high (NY deployed), multi-city S1-high (LAX/CHI), S1_LOW_NYC (A3, "
                                     "MIN_N=150), and the A4 daily-low multi-city cold sub-gates — PHIL/AUS/MIA "
                                     "(cold n≥110 + non-degradation + fresh forward CI-excludes-0 + fills "
                                     "clause) with DEN/LAX WATCH (no tradable path), plus PHX-low (A4.2 — the "
                                     "STRONGEST cold candidate, +17.65c, at the stricter 99.44% k=9 bar). A4.1 "
                                     "(2026-06-20) adds a WARM-season TRACKING gate for the user-activated "
                                     "LAX/DEN/MIA warm streams (break-even floor, n_warm≥90, WATCH-only — "
                                     "promotion needs a Verity k=9 re-rule + Aegis). New-city S1-HIGH expansion: "
                                     "SEA-high is a WATCH candidate (fails the k=6 Bonferroni bar); SFO/DAL/BOS "
                                     "and PHX-HIGH ruled out for S1-high (PHX-LOW above is a separate, strong "
                                     "cold edge — not ruled out). "
                                     "Same gate table as the $1,000 Run board, so the two pages agree. All "
                                     "ACCUMULATING — not yet a proven live edge."],
                                    className="sub"),
                           html.Div(bars, style={"marginTop": "12px"})])])


def render_scalability():
    """Scalability page (Mosaic -> Iris): the per-stream fill-cost curve + bankroll headroom = the honest
    'more money != linearly more profit' story that backs the sandbox's non-linear depth model."""
    cap = table("fill_capacity")
    # headline strip: real-curve count vs accruing count (audit-corrected; no more 'dead-book gaps')
    n_real = int((cap["real_curve"] == True).sum()) if (not cap.empty and "real_curve" in cap) else 0  # noqa: E712
    n_accr = int((cap["depth_state"] == "accruing").sum()) if (not cap.empty and "depth_state" in cap) else 0
    intro = card([
        html.Div("Scalability is a FILLS problem — and an HONESTY problem.", className="u-label",
                 style={"marginBottom": "6px"}),
        html.Div(["Each edge sits on a finite order book, and contract counts are ", html.B("per-market "
                  "(per-strike, per-day)"), ". All ", html.B("three deployed high cities"), " now have real "
                  "fill data: ", html.B("NY-high"), " from its median LOCK-MOMENT signal book, and ",
                  html.B("LAX-high + CHI-high"), " from a 9-day periodic STANDING-book depth archive — "
                  "99.9–100% fillable to 250ct/market at sub-1c slippage (badged STANDING because these are "
                  "periodic snapshots, NOT decision/lock-moment fills). The remaining streams (the daily-LOW "
                  "books + MIA-high watch + the lock-in stream) are NOT in that archive; their full slippage "
                  "curve is ", html.B("accruing forward"), ", so we show '≥25ct confirmed' and NO fabricated "
                  "ceiling. Degenerate cent-floor longshot 'ceilings' (e.g. the old 174k/27k figures) stay "
                  "removed as tick-floor artifacts. ", html.B("Paper / public-data orderbook reads — no "
                  "auth, no orders, never realized P&L."),
                  f" {n_real} streams with a real curve; {n_accr} with accruing depth data."],
                 className="sub"),
        (html.Div(["⟳ Live from the curated fill tables — ", html.B(_scal_data_asof())],
                  className="sub", style={"fontSize": "11px", "marginTop": "6px", "opacity": .85})
         if _scal_data_asof() else html.Div())],
        style={"borderColor": "color-mix(in srgb, var(--amber) 30%, transparent)"})
    return html.Div([section("Scalability — Fill-Size vs Net Edge"),
                     html.Div([html.Div(intro, className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_scalability_curve(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_scalability_headroom(), className="col-12")], className="grid12")])


def render_sandbox():
    def field(id_, label, val, step="any", mn=None, mx=None):
        kw = {"id": id_, "type": "number", "value": val, "step": step}
        if mn is not None:
            kw["min"] = mn
        if mx is not None:
            kw["max"] = mx
        return html.Div([html.Label(label), dcc.Input(**kw)], className="sb-field")

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
    _sb_med_str = f"~+{100 * _sb_med:.2f}%/m" if _sb_med is not None else "the older Kelly-MC median"
    edge_inputs = card([html.H3("Edges & Flow"),
        html.Div([f"Defaults = the live deployed $1,000 book: {_sb_nact} activated streams, ${_sb_stake} "
                  f"staked at 0.50x Kelly. The per-trade sizing is anchored to the original 7-stream sub-book "
                  f"and HELD FIXED, so profit SCALES with every field — adding NY-low (the 8th stream) lifts "
                  f"the modeled default ABOVE the older 7-stream Kelly-MC projection (", html.B(_sb_med_str),
                  f"), which has not yet been re-run with NY-low. The result below is this book's modeled "
                  f"return; nothing is pinned to a target."],
                 className="sub", style={"margin": "0 0 8px", "fontSize": "11px"}),
        html.Div("Day-ahead S1 (high)", className="sub",
                 style={"margin": "2px 0 2px", "color": MINT, "fontWeight": "700"}),
        field("sb-s1edge", "High-S1 edge (c/contract, gross)", S1_HIGH_EDGE_DEFAULT, 0.1, 0, 20),
        html.Div(f"Gross model edge before fills; ~{S1_HIGH_EDGE_DEFAULT - 2.0:.1f}c net after the 2c fill "
                 "cost (slippage + fee). Default = the RAW backtest NY/LAX/CHI activated-book mean (4.9c), "
                 "not NY alone.", className="sub",
                 style={"margin": "0 0 4px", "fontSize": "10.5px", "opacity": .8}),
        field("sb-cities", "Active high-S1 cities (streams)", 3, 1, 0, 7),
        field("sb-s1trades", "High-S1 trades / month / city", 84, 1, 0, 400),
        html.Div("Daily-LOW S1 (validated, overnight)", className="sub",
                 style={"margin": "14px 0 2px", "color": MINT, "fontWeight": "700"}),
        field("sb-lowedge", "Daily-low S1 edge (c/contract, gross)", LOW_EDGE_DEFAULT, 0.1, 0, 20),
        field("sb-lowcities", "Daily-low S1 cities", 5, 1, 0, 7),
        field("sb-lowtrades", "Daily-low trades / month / city", 82, 1, 0, 400),
        html.Div("Lock-in (latency, NYC + airports)", className="sub",
                 style={"margin": "14px 0 2px", "color": DIM, "fontWeight": "700"}),
        field("sb-lock", "Lock-in edge (c/contract)", 12, 0.5, 0, 30),
        field("sb-lockpm", "Locks / month / city (0 = not in deployed book)", 0, 1, 0, 120)],
        style={"flex": "1", "minWidth": "270px"})

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
        html.Div("Median path with p5/p95 bands, modeled from the median %/m and an implied monthly "
                 "sigma backed out of the p5 outcome. A model, not a forecast.", className="sub"),
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

    return html.Div([section("Sandbox — Interactive Risk / Return Lab"),
        html.Div("Tune the edges, capital, and risk profile. Every output is a transparent paper model "
                 "(see the note at the bottom) — research only, never realized P&L.", className="sub",
                 style={"marginBottom": "10px"}),
        html.Div([edge_inputs, cap_inputs, out], className="grid"),
        html.Div([html.Div(risk_panel, className="col-12")], className="grid12"),
        html.Div([html.Div(charts, className="col-6"), html.Div(chart_rr, className="col-6")],
                 className="grid12"),
        html.Div([html.Div(chart_dist, className="col-6"), html.Div(chart_break, className="col-6")],
                 className="grid12"),
        html.Div([html.Div(chart_ruin, className="col-12")], className="grid12"),
        html.Div([html.Div(chart_cap, className="col-12")], className="grid12"),
        html.Div([html.Div(disclaimer, className="col-12")], className="grid12")])


def render_risk():
    items = [("Capacity ceiling", "Each edge is depth-capacity-bounded; absolute $ per city has a ceiling that "
              "does NOT grow with bankroll. Scale comes from MORE validated cities, not more capital per city.",
              "warn"),
             ("Fills are the gating unknown", "Edges modeled at ≤~1c slippage; worse real fills shrink them. "
              "Forward fill validation is in progress.", "warn"),
             ("Edges are thin", "Validated multi-city S1 nets are ~3–5c/contract with bootstrap CIs whose "
              "lower bound is small — real but fragile. Sized conservatively.", "warn"),
             ("Paper only", "No authentication, no orders, no account, no real money — anywhere. Every figure "
              "is a paper/backtest/forward estimate, never realized P&L.", "bad"),
             ("Lock-in reality", "NYC lock-in is a latency artifact at the ~128s METAR floor (no faster KNYC feed "
              "exists). Airport-city lock-in is a thin speed race, not a fat edge.", "neut")]
    return html.Div([section("Risk & Honesty"),
                     html.Div([card([html.Div([html.H3(t), badge(k.upper(), k)],
                                              style={"display": "flex", "justifyContent": "space-between",
                                                     "alignItems": "center"}),
                                     html.Div(d, className="sub")]) for t, d, k in items]),
                     html.Div([html.Div(panel_latency(), className="col-12")], className="grid12")])


def render_methodology():
    m = table("methodology")
    return html.Div([section("Methodology & Provenance"),
                     card(dt(m, page_size=20) if not m.empty else html.Div("—", className="sub")),
                     html.Div([html.Div(panel_calibration_streams(), className="col-12")],
                              className="grid12", style={"marginTop": "10px"})])


RENDER = {"overview": render_overview, "markets": render_markets, "bankroll": render_bankroll,
          "forecasts": render_forecasts, "edges": render_edges, "multicity": render_multicity,
          "accuracy": render_accuracy, "quantlab": render_quantlab, "forward": render_forward,
          "scalability": render_scalability,
          "sandbox": render_sandbox, "risk": render_risk, "methodology": render_methodology}

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


def statusbar():
    return html.Div(className="statusbar", children=[
        html.Span([html.Span(className="dot"), "DATA STREAM"], className="sb-item"),
        html.Span(id="status-updated", className="sb-item mono"),
        html.Span("MODE: PAPER", className="sb-item sb-paper"),
        html.Div(style={"flex": "1"}),
        html.Span("ALL SYSTEMS NOMINAL — paper / backtest / forward, never realized P&L",
                  className="sb-item sb-ok")])


def sidebar():
    items = [html.Div([html.Span(ic, className="ic"), html.Span(lbl)], className="nav-item",
                      id={"type": "nav", "key": k}, n_clicks=0) for k, ic, lbl in NAV]
    items.append(html.Div([html.Div("BOT ENGINE", className="lbl"),
                           html.Div([html.Span(className="dot"), html.Span("RUNNING", className="st")]),
                           html.Div(id="sb-uptime", className="lbl", style={"marginTop": "6px"})],
                          className="engine"))
    return html.Div(items, className="sidebar")


app.layout = html.Div([
    dcc.Store(id="active", data="overview"),
    dcc.Store(id="theme-store", storage_type="local", data="dark"),
    dcc.Interval(id="tick", interval=60_000, n_intervals=0),
    topbar(),
    html.Div(className="shell", children=[sidebar(), html.Div(id="main", className="main")]),
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


@app.callback(Output("active", "data"), Input({"type": "nav", "key": ALL}, "n_clicks"),
              prevent_initial_call=True)
def _nav(_clicks):
    t = ctx.triggered_id
    return t["key"] if t else "overview"


@app.callback(Output({"type": "nav", "key": ALL}, "className"), Input("active", "data"))
def _nav_style(active):
    return [f"nav-item active" if o["id"]["key"] == active else "nav-item"
            for o in ctx.outputs_list]


# BUG FIX #1: route ONLY on nav change (dcc.Store "active"), NOT on the 60s tick. Re-rendering the whole
# page every minute wiped Sandbox inputs and made nav sluggish. Live elements (clock/tickers/staleness/
# market-feed) update via their OWN small callbacks below, never by re-rendering the active page.
@app.callback(Output("main", "children"), Input("active", "data"))
def _route(active):
    return RENDER.get(active, render_overview)()


# $1k paper-equity time-window selector (USER ASK 2026-06-21): re-window the equity series + readout on each
# RadioItems pick (12hr / 1D / 3D / 1W / 1M / All). Keyed only on the selector -> does not re-render the page.
@app.callback(Output("run-equity-graph", "figure"), Output("run-equity-readout", "children"),
              Input("run-equity-window", "value"))
def _run_equity_window(window):
    fig, readout, _col = _equity_figure(window or "1W")
    return fig, readout


# (2026-06-22) The resolution-day current/next TOGGLE was replaced by full per-date SECTIONS rendered
# statically in panel_resolution_day_curve (_resday_section), so this callback + its component IDs are gone.


# Per-stream calibration deck (Bayes feed): the dropdown drives the chip + PIT + coverage + meta line.
# Keyed only on the dropdown -> does NOT re-render the page on the 60s tick.
@app.callback(Output("calib-chip", "children"), Output("calib-pit", "figure"),
              Output("calib-cov", "figure"), Output("calib-meta", "children"),
              Input("calib-stream", "value"))
def _calib_stream(stream):
    d = table("calibration_streams")
    empty = go.Figure()
    if d.empty or stream is None:
        return "", _tpl(empty, h=300), _tpl(empty, h=180), ""
    sel = d[d["stream"] == stream]
    if sel.empty:
        return "", _tpl(empty, h=300), _tpl(empty, h=180), ""
    row = sel.iloc[0]
    chip = _conf_chip(row.get("direction"), row.get("s_star"))
    pit = _calib_pit_figure(row)
    cov = _calib_cov_figure(row)
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


@app.callback(Output("tb-clock", "children"), Output("tb-tickers", "children"),
              Output("sb-uptime", "children"), Input("tick", "n_intervals"))
def _live(_n):
    now = datetime.now(timezone.utc)
    cn = table("city_network")
    tk = []
    # HONEST market-style ticker strip: per-city day-ahead forecast high + the validated paper edge as the
    # green/red "delta". Real public signals (forecast + our paper edge) -- NOT invented SPX/VIX, NOT P&L.
    if not cn.empty:
        for _, r in cn.head(6).iterrows():
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
    return now.strftime("%H:%M:%S UTC"), tk, f"data · {meta_value('generated_at_utc')}"


@app.callback(Output("ov-updated", "children"), Input("tick", "n_intervals"))
def _ov_updated(_n):
    return f"updated {meta_value('generated_at_utc')}"


@app.callback(Output("status-updated", "children"), Output("tb-stale", "children"),
              Input("tick", "n_intervals"))
def _statusbar(_n):
    return f"LAST {meta_value('generated_at_utc')}", staleness_chip()


# ---- Sandbox profitability model (transparent; paper estimate only) ----
DEPTH_CAP = 250        # contracts fillable within slippage (measured median; flat fallback only)
# HIGH-S1 and DAILY-LOW edges are DIRECT c/contract inputs (user ask: c/ct is more conventional than the old
# RMSE->edge derivation). The edge field is the GROSS RAW backtest model-edge MEAN across the FULL activated
# book (kelly_activated_book per_stream mean_c), NOT NY alone (2026-06-24 fix: the old 3.9/6.7 were NY-anchored
# net_opt+1c -> understated). High = mean(NY 4.02, LAX 5.24, CHI 5.42) = 4.9c; daily-low = mean over ALL 5
# activated low streams INCLUDING NY-low = mean(NY-low 6.96, AUS 8.38, LAX 9.77, DEN 7.03, MIA 5.75) = 7.6c.
# The activated-book MC's net_opt = mean_c - 2.0c (slippage + fee) -> base FILL-COST default 2.0c lands the
# net at net_opt (~2.9c high / ~5.6c low).
S1_HIGH_EDGE_DEFAULT = 4.9     # gross raw-backtest c/ct -> ~2.9c net_opt after the 2c fill cost (NY/LAX/CHI mean)
LOW_EDGE_DEFAULT = 7.6         # gross raw-backtest c/ct -> ~5.6c net_opt after the 2c fill cost (5 low streams, incl NY-low)
# Per-trade contract SIZING, anchored ONCE to the deployed sub-book and then HELD FIXED -- it is NOT re-pinned
# when the scenario grows (doing so would suppress real scaling, the bug a user caught 2026-06-24). Anchor:
# the 7-stream sub-book (3 high + 4 warm low) at its accurate net edges, 0.50x Kelly on $1,000, reproduces
# that sub-book's Kelly-MC median (~14.6%/m, run_projection's kelly_activated_book artifact, n_streams=7).
# CT_CAL=2.81 fits that anchor. From there, profit SCALES with the inputs: adding NY-low (-> 8 streams) lifts
# the default to ~17%/m (NY-low's real contribution, which the 7-stream MC projection does NOT yet include);
# raising edges/cities scales it further. The run_projection MC is still the older 7-stream artifact -> the
# sandbox 8-stream default (~17%) is HIGHER than that projection by design until the MC is re-run with NY-low.
SANDBOX_CT_CAL = 2.81

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


def _real_stream_curves():
    """{stream_id: [(size_ct, slip_c), ...]} for every stream with depth_state=='real_curve' in the curated
    fill_scalability table (NY/LAX/CHI high today). {} if none materialized."""
    sc = table("fill_scalability")
    if sc.empty or "depth_state" not in sc:
        return {}
    sc = sc[sc["depth_state"] == "real_curve"]
    if sc.empty:
        return {}
    out = {}
    for sid, g in sc.groupby("stream_id"):
        gg = g.groupby("size_ct")["slippage_vs_best_c"].mean().sort_index()
        out[str(sid)] = [(float(s), float(v)) for s, v in gg.items()]
    return out


def _scal_curves():
    """Representative high/low curves for the HEADLINE net-at-size math (auto/manual slip modes). high = the
    deepest real high curve (NY); low = the real warm-low curve if promoted, else the conservative fallback."""
    real = _real_stream_curves()
    high = next((real[s] for s in _HIGH_CITY_ORDER if s in real), None)
    low = real.get("AUS_low_S1") or _LOW_FALLBACK_CURVE
    out = {}
    if high:
        out["high"] = high
    if low:
        out["low"] = low
    return out


def _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm, s1_edge_c, low_edge_c, lock_edge_c):
    """Each ACTIVE per-market book as (key, net_edge_c, slip_curve, trades_per_mo). Real per-city HIGH curves
    + a conservative DAILY-LOW curve -> shallower low books saturate at a smaller size (earlier bankroll) than
    deep high books, so the capacity-vs-bankroll curve is a STAIRCASE that drops streams a group at a time."""
    real = _real_stream_curves()
    hi = [real[s] for s in _HIGH_CITY_ORDER if s in real] or [None]
    low_curve = real.get("AUS_low_S1") or _LOW_FALLBACK_CURVE
    books = []
    for i in range(max(0, int(cities))):
        books.append((f"high{i+1}", s1_edge_c, hi[i] if i < len(hi) else hi[-1], s1tr))
    for j in range(max(0, int(low_cities))):
        books.append((f"low{j+1}", low_edge_c, low_curve, low_trades))
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
                              s1_edge_c, low_edge_c, lock_edge_c):
    """Absolute-$ monthly profit CEILING from REAL non-linear market depth, summed PER BOOK (each book fills
    its profit-MAXIMIZING size along its OWN archetype slippage(size) curve; net edge degrades with size, $
    peaks then falls). Bankroll-independent -> the hard plateau. Because shallow daily-low books peak at a
    smaller size than the deep high books, the books saturate at DIFFERENT bankrolls -> the staircase. Falls
    back to the flat 250ct cap per book if a curve is absent."""
    books = _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm,
                                s1_edge_c, low_edge_c, lock_edge_c)
    tot = 0.0
    for _key, edge, curve, trades in books:
        peak, _ = _optimal_fill_dollars(edge, curve, trades, 1)
        tot += peak * trades
    return max(0.0, tot)


@app.callback(
    Output("sb-profit", "children"), Output("sb-roi", "children"), Output("sb-roi", "style"),
    Output("sb-kelly-band", "children"), Output("sb-note", "children"), Output("sb-risk-metrics", "children"),
    Output("sb-chart", "figure"), Output("sb-fan", "figure"), Output("sb-rr", "figure"),
    Output("sb-dist", "figure"), Output("sb-cap", "figure"), Output("sb-cap-flag", "children"),
    Output("sb-ruin", "figure"),
    Input("sb-s1edge", "value"), Input("sb-cities", "value"), Input("sb-s1trades", "value"),
    Input("sb-lowedge", "value"), Input("sb-lowcities", "value"), Input("sb-lowtrades", "value"),
    Input("sb-lock", "value"), Input("sb-lockpm", "value"),
    Input("sb-bankroll", "value"), Input("sb-kelly", "value"),
    Input("sb-slip", "value"), Input("sb-slip-mode", "value"), Input("sb-slip-manual", "value"))
def _sandbox(s1_c, cities, s1tr, low_c, low_cities, low_trades, lock_c, lockpm,
             bankroll, kelly, slip, slip_mode, slip_manual):
    import numpy as _np
    blank = _tpl(go.Figure(), h=300)
    try:
        s1_c = max(0.0, float(s1_c)); cities = max(0, int(cities)); s1tr = max(0.0, float(s1tr))
        low_c = float(low_c); low_cities = max(0, int(low_cities)); low_trades = max(0.0, float(low_trades))
        lock_c = float(lock_c); lockpm = max(0.0, float(lockpm))
        bankroll = max(0.0, float(bankroll)); kelly = float(kelly)
        slip = max(0.0, float(slip))
        slip_mode = slip_mode if slip_mode in ("off", "auto", "manual") else "off"
        slip_manual = max(0.0, float(slip_manual)) if slip_manual is not None else 0.0
    except (TypeError, ValueError):
        return ("—", "—", {"color": DIM}, "", "Enter valid numbers in every field.", "",
                blank, blank, blank, blank, blank, "", blank)
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
    low_gross_c = max(0.0, low_c)
    lock_gross_c = max(0.0, lock_c)
    # per-stream NET edge after the BASE slippage (used by the depth-ceiling/optimal-fill math, which is
    # mode-agnostic and conservative -- the base slip floor)
    s1_edge_c = max(0.0, s1_gross_c - slip)
    low_edge_c = max(0.0, low_gross_c - slip)
    lock_edge_c = max(0.0, lock_gross_c - slip)
    # contracts per trade: CALIBRATED so the DEFAULT scenario (deployed 7-stream book) at 0.50x Kelly on
    # $1,000 reproduces the activated-book median (~14.63%/m) -- the headline stays GROUNDED to the real number,
    # it does NOT run away. From that anchor it scales LINEARLY with Kelly fraction and bankroll, and is
    # CAPPED at the measured fillable depth (DEPTH_CAP/market). Every per-stream input (edge, trades, cities)
    # still multiplies through, so all inputs MOVE the output (FIX 1) while the magnitude stays honest.
    active_books = max(1, cities + low_cities)        # used only for the note/labeling
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
    low_net_at = _net_at(low_gross_c, _curves.get("low"), low_ct)
    lock_net_at = _net_at(lock_gross_c, _curves.get("high"), lock_ct)
    s1_net_at = max(0.0, s1_net_at); low_net_at = max(0.0, low_net_at); lock_net_at = max(0.0, lock_net_at)
    # monthly $ per stream = net_edge_at_size($) * trades/mo * markets * contracts/trade (size-degraded edge)
    s1_monthly = (s1_net_at / 100.0) * s1tr * cities * s1_ct
    low_monthly = (low_net_at / 100.0) * low_trades * low_cities * low_ct
    lock_monthly = (lock_net_at / 100.0) * lockpm * cities * lock_ct
    total_uncapped = s1_monthly + low_monthly + lock_monthly
    # ---- DEPTH-CAPACITY CAP (deliverable #3): absolute-$ profit cannot exceed what real market depth
    # fills. ceiling = DEPTH_CAP ct * edge * trades/mo * markets -> bankroll-INDEPENDENT plateau. Past the
    # ceiling, more capital earns the SAME dollars (lower %). This is the "$100M -> same as ~$5k" reality.
    cap_ceiling = _capacity_ceiling_dollars(cities, s1tr, low_cities, low_trades, lockpm,
                                            s1_edge_c, low_edge_c, lock_edge_c)
    capacity_bound = bankroll > 0 and cap_ceiling > 0 and total_uncapped > cap_ceiling
    total = min(total_uncapped, cap_ceiling) if cap_ceiling > 0 else total_uncapped
    # if the cap binds, shrink each stream proportionally so the breakdown still sums to the capped total
    if capacity_bound and total_uncapped > 0:
        _scale = total / total_uncapped
        s1_monthly *= _scale; low_monthly *= _scale; lock_monthly *= _scale
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
    # drift = the COMPUTED scenario ROI (so the fan reflects the user's edge/city/trade inputs, FIX 1); the
    # monthly sigma is scaled from the Kelly sweep's median/p5 spread (the validated risk shape) about it.
    mu_m = roi / 100.0
    swp_sigma = max(1e-4, (k["med"] - k["p5"]) / 100.0 / 1.645)      # sweep's monthly sigma at this fraction
    sigma_m = max(1e-4, swp_sigma * (abs(mu_m) / max(abs(k["med"]) / 100.0, 1e-6)) if k["med"] else swp_sigma)
    months = _np.arange(0, 13)
    rng = _np.random.default_rng(12345)
    n_paths = 4000
    # multiplicative monthly returns -> equity multiple paths
    draws = rng.normal(mu_m, sigma_m, size=(n_paths, 12))
    eq = _np.cumprod(1.0 + _np.clip(draws, -0.95, None), axis=1)
    eq = _np.hstack([_np.ones((n_paths, 1)), eq]) * (bankroll if bankroll > 0 else 1.0)
    med_path = _np.median(eq, axis=0)
    p5_path = _np.percentile(eq, 5, axis=0)
    p95_path = _np.percentile(eq, 95, axis=0)
    fan = go.Figure()
    fan.add_scatter(x=list(months) + list(months)[::-1], y=list(p95_path) + list(p5_path)[::-1],
                    fill="toself", fillcolor="rgba(22,199,132,.10)", line=dict(width=0), mode="lines",
                    name="p5–p95", hoverinfo="skip")
    fan.add_scatter(x=months, y=med_path, mode="lines", name="median",
                    line=dict(color=MINT, width=2.4, shape="spline", smoothing=0.4),
                    hovertemplate="month %{x}<br>%{y:$,.0f}<extra></extra>")
    fan.add_scatter(x=months, y=p5_path, mode="lines", name="p5", line=dict(color=RED, width=1.3, dash="dot"),
                    hovertemplate="month %{x}<br>p5 %{y:$,.0f}<extra></extra>")
    fan.add_scatter(x=months, y=p95_path, mode="lines", name="p95",
                    line=dict(color=CYAN, width=1.3, dash="dot"),
                    hovertemplate="month %{x}<br>p95 %{y:$,.0f}<extra></extra>")
    base = bankroll if bankroll > 0 else 1.0
    fan.add_hline(y=base, line=dict(color=AXISCOL, width=1, dash="dash"))
    fan.update_layout(title=None)
    fan.update_yaxes(title="paper equity ($)", tickprefix="$", tickformat=",.0f")
    fan.update_xaxes(title="month", nticks=13)

    # ---- (b) risk vs return curve across fractions ----
    fr = [r["f"] for r in KELLY_SWEEP]; med = [r["med"] for r in KELLY_SWEEP]; dd = [r["dd"] for r in KELLY_SWEEP]
    rr = go.Figure()
    rr.add_scatter(x=dd, y=med, mode="lines+markers+text", name="Kelly frontier",
                   text=[f"{f:.2f}x" for f in fr], textposition="top center",
                   textfont=dict(size=10, color=DIM),
                   line=dict(color=CYAN, width=2, shape="spline", smoothing=0.3),
                   marker=dict(size=9, color=[MINT if f <= KELLY_CEILING else RED for f in fr],
                               line=dict(width=1, color="rgba(255,255,255,.25)")),
                   hovertemplate="%{text}<br>median %{y:+.1f}%/m<br>p95 maxDD %{x:.1f}%<extra></extra>")
    rr.add_scatter(x=[k["dd"]], y=[k["med"]], mode="markers", name="your pick",
                   marker=dict(size=16, color=AMBER, symbol="star",
                               line=dict(width=1.4, color="#fff")),
                   hovertemplate=f"your pick {kelly:.2f}x<br>median %{{y:+.1f}}%/m"
                                 f"<br>p95 maxDD %{{x:.1f}}%<extra></extra>")
    # mark the RECOMMENDED ceiling drawdown (0.50x = 18%) -- beyond it is reachable but riskier
    rr.add_vline(x=18.0, line=dict(color=NEUTRAL, width=1.4, dash="dash"),
                 annotation_text="0.50x recommended", annotation_position="top",
                 annotation_font=dict(color=NEUTRAL, size=10))
    rr.update_layout(title=None)
    rr.update_yaxes(title="median return (%/month)", ticksuffix="%")
    rr.update_xaxes(title="p95 max drawdown (%)", ticksuffix="%")

    # ---- (c) return distribution + drawdown gauge ----
    dist = go.Figure()
    dist.add_trace(go.Indicator(
        mode="gauge+number", value=k["dd"],
        number={"suffix": "%", "font": {"size": 22, "color": _sev_color(_sev_dd(k["dd"]))}},
        title={"text": "p95 max drawdown", "font": {"size": 12, "color": INK}},
        gauge={"axis": {"range": [0, 40], "tickwidth": 1, "tickcolor": AXISCOL,
                        "tickfont": {"size": 9, "color": DIM}},
               "bar": {"color": _sev_color(_sev_dd(k["dd"])), "thickness": 0.72},
               "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
               "steps": [{"range": [0, 12], "color": "rgba(22,199,132,.12)"},
                         {"range": [12, 20], "color": "rgba(217,162,58,.12)"},
                         {"range": [20, 40], "color": "rgba(234,57,67,.12)"}],
               "threshold": {"line": {"color": AMBER, "width": 2}, "thickness": 0.85, "value": 18}},
        domain={"x": [0.0, 0.42], "y": [0.0, 1.0]}))
    # return spread bar (p5 / median / p95) on the right
    spread_x = [k["p5"], k["med"], k["med"] + (k["med"] - k["p5"])]   # p95 ~ symmetric proxy of the band
    dist.add_bar(x=["p5", "median", "p95"], y=spread_x,
                 marker_color=[RED, MINT, CYAN], width=0.6,
                 text=[f"{v:+.1f}%" for v in spread_x], textposition="outside", cliponaxis=False,
                 xaxis="x2", yaxis="y2",
                 hovertemplate="%{x}: %{y:+.1f}%/m<extra></extra>")
    dist.update_layout(
        title=None, template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=INK, family="Inter, system-ui", size=12), height=300, showlegend=False,
        margin=dict(l=10, r=20, t=20, b=40),
        xaxis2=dict(domain=[0.56, 1.0], anchor="y2", tickfont=dict(size=11, color=DIM),
                    showgrid=False, linecolor=GRIDCOL),
        yaxis2=dict(anchor="x2", title="%/month", ticksuffix="%", tickfont=dict(size=11, color=DIM),
                    gridcolor=GRIDCOL, griddash="dot", zerolinecolor=AXISCOL,
                    title_font=dict(size=11, color=DIM)))
    dist.add_annotation(x=0.78, y=1.08, xref="paper", yref="paper", showarrow=False,
                        text="Monthly return spread", font=dict(size=12, color=INK))

    # ---- profit breakdown by stream ----
    fig = go.Figure()
    labels = ["High S1", "Daily-low S1", "Lock-in", "TOTAL"]
    vals = [s1_monthly, low_monthly, lock_monthly, total]
    fig.add_bar(x=labels, y=vals, marker_color=[MINT, "#7fb0a0", CYAN, AMBER], width=0.62,
                text=[f"${v:,.0f}" for v in vals], textposition="outside", cliponaxis=False,
                hovertemplate="%{x}<br>%{y:$,.0f} / month<extra></extra>")
    fig.update_layout(title=None)
    _vmax = max(list(vals) + [1.0]); _vmin = min(list(vals) + [0.0])
    fig.update_yaxes(title="paper profit ($ / month)", tickprefix="$", tickformat=",.0f",
                     range=[_vmin * 1.18 if _vmin < 0 else 0, _vmax * 1.18])
    fig.update_xaxes(title="")

    # ---- (d) capacity-ceiling-vs-bankroll line chart (deliverable #3 + Mosaic non-linear 2026-06-20) ----
    # Re-evaluate the SAME per-stream profit model across a $100 -> $100M bankroll sweep. UNCAPPED = flat full
    # model edge, contracts grow with bankroll, NO depth limit (the naive linear extrapolation). CAPPED = the
    # REAL Mosaic curve: net edge per contract degrades with per-market size along slippage(size), and a stream
    # never fills past the size that maximizes its $ -> a SMOOTH plateau at the per-stream capacity ceiling
    # (not a hard 250ct cliff). LOG x AND LOG y so the plateau and the rising uncapped line are both legible.
    cap = go.Figure()
    if cap_ceiling > 0:
        bxs = _np.geomspace(100.0, 1e8, 90)                          # $100 -> $100M log axis
        # PER-BOOK staircase (2026-06-21): each active book fills along its OWN curve; shallow daily-low books
        # cap at a smaller size (lower bankroll) than the deep high books, so streams drop a group at a time.
        _books = _capacity_book_list(cities, s1tr, low_cities, low_trades, lockpm,
                                     s1_edge_c, low_edge_c, lock_edge_c)
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
        cap.add_scatter(x=bxs, y=prof_uncapped, mode="lines", name="uncapped (no depth limit)",
                        line=dict(color=NEUTRAL, width=1.6, dash="dash"),
                        hovertemplate="bankroll $%{x:,.0f}<br>uncapped $%{y:,.0f}/mo<extra></extra>")
        cap.add_scatter(x=bxs, y=prof_capped, mode="lines", name="depth-capped (your actual)",
                        line=dict(color=GREEN, width=2.8),
                        fill="tozeroy", fillcolor="rgba(0,224,138,.08)",
                        hovertemplate="bankroll $%{x:,.0f}<br>capped $%{y:,.0f}/mo<extra></extra>")
        cap.add_hline(y=cap_ceiling, line=dict(color=RED, width=1.4, dash="dot"),
                      annotation_text=f"absolute depth ceiling ${cap_ceiling:,.0f}/mo",
                      annotation_position="top left", annotation_font=dict(color=RED, size=10))
        if step1_b and bind_b and 100 <= step1_b <= 1e8 and step1_b < bind_b * 0.9:
            cap.add_vline(x=step1_b, line=dict(color=NEUTRAL, width=1.1, dash="dot"),
                          annotation_text=f"shallow daily-low books saturate ~${step1_b:,.0f}",
                          annotation_position="bottom left", annotation_font=dict(color=DIM, size=9))
        if bind_b and 100 <= bind_b <= 1e8:
            cap.add_vline(x=bind_b, line=dict(color=AMBER, width=1.9, dash="dash"),
                          annotation_text=(f"depth fully saturates ~${bind_b:,.0f} — "
                                           f"beyond here extra bankroll adds ~$0/mo"),
                          annotation_position="top right", annotation_font=dict(color=AMBER, size=10))
        # mark the user's current bankroll on the capped (actual) curve
        bnow = max(100.0, min(1e8, bankroll if bankroll > 0 else 1000.0))
        pnow = _profit_at(bnow, capped=True)
        cap.add_scatter(x=[bnow], y=[max(pnow, 1e-9)], mode="markers", name="your bankroll",
                        marker=dict(size=15, color=AMBER, symbol="star", line=dict(width=1.4, color="#fff")),
                        hovertemplate=f"your bankroll ${bnow:,.0f}<br>$%{{y:,.0f}}/mo<extra></extra>")
        cap.update_xaxes(type="log", title="bankroll ($, log scale)", tickprefix="$", tickformat="~s")
        # LOG y so the capped plateau and rising uncapped line are both legible across decades (FIX 4)
        cap.update_yaxes(type="log", title="paper profit ($ / month, log scale)", tickprefix="$",
                         tickformat="~s")
    cap.update_layout(title=None)

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
    note = (f"Profit is a TRANSPARENT per-stream sum, driven by every input: for each active stream, "
            f"(net c/contract at this size ÷ 100) × trades/mo × active markets × contracts/trade. "
            f"Contracts/trade ({s1_ct:.1f} here) is CALIBRATED so the default scenario at 0.25x on $1,000 "
            f"equals the validated $1k Kelly-sweep median (~7.1%/m); it scales with Kelly ({kelly:.2f}x) and "
            f"bankroll (${bankroll:,.0f}). NON-LINEAR DEPTH{_nonlin}: the net c/contract DEGRADES as per-market "
            f"size grows along each book's measured VWAP-slippage curve and crosses zero at its real capacity "
            f"ceiling (deep high books fill to ~250ct; the shallower daily-low books cap sooner, so they "
            f"saturate at a lower bankroll — the capacity chart is a staircase, not a flat cliff). High-S1 "
            f"net at size = {s1_net_at:.1f}c (model {s1_edge_c:.1f}c) × {s1tr:.0f}/mo × {cities} cities = "
            f"${s1_monthly:,.0f}/mo; daily-low net at size {low_net_at:.1f}c × {low_trades:.0f}/mo × "
            f"{low_cities} cities = ${low_monthly:,.0f}/mo; lock-in net at size {lock_net_at:.1f}c × "
            f"{lockpm:.0f}/mo × {cities} cities = ${lock_monthly:,.0f}/mo. Win rate is NOT a lever — each net "
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

    # ---- (e) ITEM 7 (AUDIT-CORRECTED 2026-06-21): P(maxDD>=25%) and P(ruin=DD>=50%) vs Kelly fraction ----
    # The old P(month<0) line was DROPPED: it is essentially INVARIANT to the Kelly fraction. Scaling every
    # bet by f scales the monthly mean AND std EQUALLY, so the standardized monthly return (mu/sigma) -- and
    # hence the sign probability -- barely moves with f. A risk chart whose curve doesn't respond to the lever
    # is misleading. We replace it with P(max drawdown >= 25%), which RISES monotonically with Kelly (deeper
    # leverage -> deeper drawdowns) and pairs naturally with the existing P(ruin)=P(DD>=50%). Both come from
    # the SAME 12-month MC engine that drives the equity fan.
    DD25 = 0.25                                              # a >=25% peak-to-trough drawdown
    RUIN_DD = 0.50                                           # ruin = a >=50% peak-to-trough drawdown
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
        dd = (peak - eqp) / peak
        maxdd = dd.max(axis=1)
        p_dd25.append(float((maxdd >= DD25).mean()))
        p_ruin.append(float((maxdd >= RUIN_DD).mean()))
    ruin = go.Figure()
    ruin.add_scatter(x=fr_grid, y=[p * 100 for p in p_dd25], mode="lines+markers",
                     name=f"P(max drawdown ≥ {int(DD25*100)}% in 12mo)",
                     line=dict(color=AMBER, width=2.4), marker=dict(size=6),
                     hovertemplate="Kelly %{x:.2f}x<br>P(maxDD≥25%) %{y:.0f}%<extra></extra>")
    ruin.add_scatter(x=fr_grid, y=[p * 100 for p in p_ruin], mode="lines+markers",
                     name=f"P(ruin, ≥{int(RUIN_DD*100)}% DD in 12mo)",
                     line=dict(color=RED, width=2.6), marker=dict(size=6),
                     hovertemplate="Kelly %{x:.2f}x<br>P(ruin) %{y:.1f}%<extra></extra>")
    ruin.add_vrect(x0=0.50, x1=0.75, fillcolor="rgba(217,162,58,.08)", line_width=0)
    ruin.add_vrect(x0=0.75, x1=1.00, fillcolor="rgba(234,57,67,.08)", line_width=0)
    ruin.add_vline(x=kelly, line=dict(color=GREEN, width=1.6, dash="dash"),
                   annotation_text=f"your pick {kelly:.2f}x", annotation_position="top",
                   annotation_font=dict(color=GREEN, size=10))
    ruin.add_vline(x=0.50, line=dict(color=NEUTRAL, width=1.2, dash="dot"),
                   annotation_text="0.50x ceiling", annotation_position="bottom right",
                   annotation_font=dict(color=DIM, size=9))
    ruin.update_layout(title=None)
    ruin.update_xaxes(title="Kelly fraction", dtick=0.25)
    ruin.update_yaxes(title="probability (%)", ticksuffix="%", rangemode="tozero")

    return (f"${total:,.0f}", f"{roi:+.1f}%", {"color": roi_color}, kelly_band, note, metrics,
            _tpl(fig, h=300, legend=False), _tpl(fan, h=300), _tpl(rr, h=300, legend=False), dist,
            _tpl(cap, h=320), cap_flag, _tpl(ruin, h=300, legend=True))


def _sev_color(sev):
    return {"good": MINT, "warn": AMBER, "bad": RED}[sev]


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
