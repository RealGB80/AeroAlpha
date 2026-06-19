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

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dash
import dash_auth
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, dash_table, Input, Output, State, ALL, ctx

from data import table, meta_value

# ---- palette (mirrors assets/theme.css; RED/GREEN financial-terminal retheme 2026-06-19) ----
# GREEN (#16c784) = primary data series / positive / up / model-good. RED (#ea3943) = negative / down /
# loss. Cyan/violet are SPARING secondary series only; amber is the 3rd (paper/warn) accent.
# MINT is kept as the symbol name for the primary GREEN so existing call sites need no rename.
BG, PANEL, INK, DIM = "#0a0c0e", "rgba(22,26,30,0.88)", "#eef2f3", "#8a949b"
GREEN, RED = "#16c784", "#ea3943"
MINT = GREEN                              # alias: "MINT" historically == the primary accent (now green)
CYAN, VIOLET, AMBER = "#4a90b8", "#8a7fc0", "#d9a23a"
ACCENT = GREEN
# colorway LEADS green -> red so sign/order reads positive-first, loss-last; cyan/violet sit between as
# desaturated secondary series.
PALETTE = [GREEN, CYAN, VIOLET, AMBER, "#7fb0a0", RED]
GRIDCOL = "rgba(138,150,158,0.14)"        # gridlines: neutral slate
AXISCOL = "rgba(138,150,158,0.30)"        # axis lines / ticks: neutral slate
STALE_AFTER_MIN = 90                       # global staleness threshold

NAV = [("overview", "◉", "Overview"), ("forecasts", "☉", "Forecasts"),
       ("edges", "↑", "Edges"), ("multicity", "▦", "Multi-City"),
       ("accuracy", "◎", "Forecast Accuracy"), ("forward", "✓", "Forward Validation"),
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
    for strat, color in zip(["S1", "S3", "S3early"], [MINT, VIOLET, CYAN]):
        sub = d[d["strategy"] == strat]
        if sub.empty:
            continue
        fig.add_scatter(x=sub["market_mid"], y=sub["model_p"], mode="markers", name=strat,
                        marker=dict(size=9, color=color, opacity=.8,
                                    line=dict(width=1, color="rgba(255,255,255,.2)")),
                        customdata=sub[["ticker", "edge"]].values,
                        hovertemplate="<b>%{customdata[0]}</b><br>market mid %{x:.2f} · model %{y:.2f}"
                                      "<br>edge %{customdata[1]:+.2f}<extra></extra>")
    fig.update_layout(title=None)
    fig.update_xaxes(title="market mid (implied P)", range=[0, 1], tickformat=".0%")
    fig.update_yaxes(title="model P(yes)", range=[0, 1], tickformat=".0%")
    return card([html.H3("Market-Divergence Ribbon — Where We Disagree With the Market"),
                 _cap("Each point is a scanned contract: our model P(yes) vs the market mid. On the dashed "
                      "agreement line we have no view. The green band (model above market) is our buy-YES "
                      "edge zone; red is the opposite. Distance from the line is the raw edge before fills. "
                      "Paper/forward scans only."),
                 graph(_tpl(fig, h=360))])


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
    z = [[None] * len(weeks) for _ in range(7)]
    cd = [[None] * len(weeks) for _ in range(7)]
    wi = {w: i for i, w in enumerate(weeks)}
    for _, r in d.iterrows():
        z[int(r["dow"])][wi[r["week"]]] = r["error_f"]
        cd[int(r["dow"])][wi[r["week"]]] = r["date"]
    fig = go.Figure(go.Heatmap(z=z, x=weeks, y=dows, customdata=cd,
                               colorscale=[[0, MINT], [0.5, "#12161a"], [1, RED]],
                               zmid=0, xgap=2, ygap=2,
                               colorbar=dict(title="°F", thickness=10, len=0.8,
                                             tickfont=dict(size=10, color=DIM)),
                               hovertemplate="%{customdata}<br>error %{z:+.1f}°F<extra></extra>"))
    fig.update_layout(title=None)
    fig.update_xaxes(title="", showticklabels=False, nticks=12)
    fig.update_yaxes(title="", autorange="reversed")
    mean_err = float(d["error_f"].mean())
    return card([html.H3("Settlement-Surprise Calendar — Signed Forecast Error"),
                 _cap(f"GitHub-style date grid of (observed − forecast) high in °F over ~{len(d)} settled days. "
                      f"Red = we under-forecast (hotter than expected), green = over-forecast. Mean error "
                      f"{mean_err:+.2f}°F — a small residual warm bias; clusters reveal regime surprises. Backtest."),
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
    return card([html.H3("Trade Blotter — Recent Settled Paper Signals"),
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
                  "Changepoint", "Target Date", "Unit", "Beats Market"}


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

    def td(c, v):
        s = "—" if (v is None or (isinstance(v, float) and v != v)) else str(v)
        cls = "pt-td " + ("pt-l" if left[c] else "pt-r mono")
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


# ============================== PAGES ==============================
def render_overview():
    kpi = table("kpi"); br = table("bankroll_run"); cs1 = table("city_s1")
    cards = [kpi_card(r["label"], r["value"], r["unit"], r["status"]) for _, r in kpi.iterrows()] \
        if not kpi.empty else [card("no KPIs yet")]
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
        fig = go.Figure()
        fig.add_scatter(x=br["date"], y=br["bankroll"], name="Paper bankroll", mode="lines",
                        line=dict(color=MINT, width=2.6, shape="spline", smoothing=0.5),
                        fill="tozeroy", fillcolor="rgba(22,199,132,.07)",
                        hovertemplate="%{x}<br>%{y:$,.0f}<extra></extra>")
        if "expected_bankroll" in br:
            fig.add_scatter(x=br["date"], y=br["expected_bankroll"], name="Backtest-expected", mode="lines",
                            line=dict(color=DIM, width=1.6, dash="dash", shape="spline", smoothing=0.5),
                            hovertemplate="%{x}<br>%{y:$,.0f}<extra></extra>")
        fig.update_yaxes(title="paper bankroll ($)", tickprefix="$", tickformat=",.0f")
        fig.update_xaxes(title="")
        bank = card([html.H3("$1,000 Paper Run vs Backtest-Expected"), graph(_tpl(fig))])
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
                     html.Div(cards, className="grid"),
                     html.Div([html.Div(bank, style={"flex": "2", "minWidth": "420px"}),
                               html.Div(status_card, style={"flex": "1", "minWidth": "260px"})],
                              className="grid"),
                     html.Div([html.Div(panel_brier_gauges(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_blotter(), className="col-12")], className="grid12")])


def render_forecasts():
    sf = table("source_forecast")
    if sf.empty:
        return html.Div([section("Forecasts by source"), card("No source-forecast snapshot yet "
                         "(snapshot_source_forecasts.py runs in the pipeline; panel fills shortly).")])
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
                     html.Div([html.Div(panel_pit(), className="col-6"),
                               html.Div(panel_emos_skill(), className="col-6")], className="grid12"),
                     html.Div([html.Div(panel_fan(), className="col-12")], className="grid12"),
                     html.Div([html.Div(panel_surprise(), className="col-12")], className="grid12")])


def render_forward():
    g = table("forward_gate")
    if g.empty:
        return html.Div([section("Forward Validation"), card("No forward-gate data yet.")])
    bars = []
    for _, r in g.iterrows():
        pct = min(100, int(100 * (r["n_settled"] or 0) / max(r["n_required"] or 1, 1)))
        cp = f" · since {r['changepoint_date']}" if r.get("changepoint_date") else ""
        bars.append(html.Div([
            html.Div([html.B(r["stream"]), html.Span(f"  {r['n_settled']}/{r['n_required']} settled{cp}",
                      className="sub")], style={"display": "flex", "justifyContent": "space-between"}),
            html.Div(html.Div(className="bar-fill", style={"width": f"{pct}%"}),
                     className="bar-track", style={"margin": "5px 0 14px"})]))
    return html.Div([section("Forward Validation"),
                     card([html.H3("Pre-registered forward gates"),
                           html.Div("Thresholds fixed in advance (docs/FORWARD_PROTOCOL.md). Day-ahead streams "
                                    "reset at the 2026-06-14 S2X changepoint. All ACCUMULATING — not yet a "
                                    "proven live edge.", className="sub"), html.Div(bars, style={"marginTop": "12px"})])])


def render_sandbox():
    def field(id_, label, val, step="any", mn=None, mx=None):
        kw = {"id": id_, "type": "number", "value": val, "step": step}
        if mn is not None:
            kw["min"] = mn
        if mx is not None:
            kw["max"] = mx
        return html.Div([html.Label(label), dcc.Input(**kw)], className="sb-field")

    # ---- column 1: edge & flow inputs ----
    edge_inputs = card([html.H3("Edges & Flow"),
        html.Div("Day-ahead S1 (high)", className="sub",
                 style={"margin": "2px 0 2px", "color": MINT, "fontWeight": "700"}),
        field("sb-rmse", "Day-ahead RMSE (°F)", 1.66, 0.01, 0.5, 3.0),
        field("sb-cities", "Active high-S1 cities (streams)", 3, 1, 0, 7),
        field("sb-s1trades", "High-S1 trades / month / city", 84, 1, 0, 400),
        html.Div("Daily-LOW S1 (validated, overnight)", className="sub",
                 style={"margin": "14px 0 2px", "color": MINT, "fontWeight": "700"}),
        field("sb-lowedge", "Daily-low S1 edge (c/contract)", 6.96, 0.1, 0, 20),
        field("sb-lowcities", "Daily-low S1 cities", 1, 1, 0, 7),
        field("sb-lowtrades", "Daily-low trades / month / city", 82, 1, 0, 400),
        html.Div("Lock-in (latency, NYC + airports)", className="sub",
                 style={"margin": "14px 0 2px", "color": DIM, "fontWeight": "700"}),
        field("sb-lock", "Lock-in edge (c/contract)", 12, 0.5, 0, 30),
        field("sb-lockpm", "Locks / month / city", 14, 1, 0, 120)],
        style={"flex": "1", "minWidth": "270px"})

    # ---- column 2: capital, risk profile, frictions ----
    cap_inputs = card([html.H3("Capital & Risk Profile"),
        field("sb-bankroll", "Bankroll ($)", 1000, 100, 100, 1_000_000),
        html.Div("Kelly fraction (risk profile)", className="sb-field-lbl u-label",
                 style={"margin": "12px 0 6px"}),
        dcc.Slider(id="sb-kelly", min=0.25, max=0.75, step=0.05, value=0.25,
                   marks={0.25: "0.25", 0.35: "0.35", 0.50: "0.50", 0.65: "0.65", 0.75: "0.75"},
                   tooltip={"placement": "bottom", "always_visible": False},
                   updatemode="mouseup"),
        html.Div("Hard ceiling 0.50x — beyond it, ruin risk climbs steeply.", className="sub",
                 style={"margin": "8px 0 2px", "fontSize": "11px"}),
        html.Div(style={"height": "10px"}),
        field("sb-winrate", "Win rate (%)", 55, 1, 1, 99),
        field("sb-slip", "Slippage assumption (c/contract)", 1.0, 0.5, 0, 3),
        html.Div("Win rate + slippage tune the per-trade economics; the Kelly fraction governs "
                 "stake size and therefore the risk band below.", className="sub",
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
                  ". Hard ceiling at 0.50x."], className="sub", style={"marginBottom": "10px"}),
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

    disclaimer = card([html.Div("How this is computed", className="u-label", style={"marginBottom": "6px"}),
        html.Div(["The profit breakdown is a transparent parametric model: per-stream edge (c/contract) "
                  "× trades/month × stake (Kelly-fraction × bankroll, depth-capped at 250 contracts) less "
                  "slippage. The risk band, equity fan, risk/return curve, and drawdown gauge are read from "
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
                     card(dt(m, page_size=20) if not m.empty else html.Div("—", className="sub"))])


RENDER = {"overview": render_overview, "forecasts": render_forecasts, "edges": render_edges,
          "multicity": render_multicity, "accuracy": render_accuracy, "forward": render_forward,
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
        html.Span([html.Span(className="dot"), "LIVE"], className="pill live"),
        html.Span("PAPER ONLY — no orders, no real money", className="pill paper"),
        html.Div(style={"flex": "1"}),
        html.Div(id="tb-tickers", style={"display": "flex", "gap": "20px"}),
        html.Span("Dark", id="theme-toggle", className="pill theme-toggle", n_clicks=0,
                  title="Toggle light / dark", style={"cursor": "pointer"}),
        html.Span(id="tb-clock", className="mono", style={"color": DIM, "fontSize": "13px"})])


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


@app.callback(Output("main", "children"), Input("active", "data"), Input("tick", "n_intervals"))
def _route(active, _n):
    return RENDER.get(active, render_overview)()


@app.callback(Output("tb-clock", "children"), Output("tb-tickers", "children"),
              Output("sb-uptime", "children"), Input("tick", "n_intervals"))
def _live(_n):
    now = datetime.now(timezone.utc)
    kpi = table("kpi")
    def kv(name):
        row = kpi[kpi["name"] == name] if not kpi.empty and "name" in kpi else kpi.iloc[0:0]
        return row["value"].iloc[0] if not row.empty else None
    rmse, cities = kv("ny_rmse"), kv("cities_beating_market")
    tk = [html.Div([html.Span("NY RMSE ", className="k"),
                    html.Span(f"{rmse:.2f}°F" if rmse is not None else "—", className="v up")], className="ticker"),
          html.Div([html.Span("CITIES EDGE ", className="k"),
                    html.Span(f"{int(cities) if cities is not None else 0}", className="v up")], className="ticker"),
          html.Div([html.Span("INTEGRITY ", className="k"),
                    html.Span(meta_value("integrity_verdict"), className="v")], className="ticker")]
    return now.strftime("%H:%M:%S UTC"), tk, f"data · {meta_value('generated_at_utc')}"


@app.callback(Output("ov-updated", "children"), Input("tick", "n_intervals"))
def _ov_updated(_n):
    return f"updated {meta_value('generated_at_utc')}"


# ---- Sandbox profitability model (transparent; paper estimate only) ----
MKT_SD = 1.95          # market prices ~this implied SD; edge ~ overconfidence vs our RMSE
S1_EDGE_K = 12.0       # c/contract per F of (MKT_SD - RMSE); calibrated to ~+4c at RMSE 1.66
DEPTH_CAP = 250        # contracts fillable within slippage (measured median)
S1_PRICE, LOCK_PRICE, LOW_PRICE = 0.5, 0.9, 0.5

# Kelly stake-sweep — TRANSPARENT EMBEDDED CONSTANT, sourced verbatim from
# data/processed/kelly_1k_stake_sweep_20260619_000854.json ($1,000 correlation-aware MC, every edge
# sized at its CI lower bound). Rows: fraction -> the real risk/return numbers. Interpolated linearly
# between rows when the user picks an in-between fraction. 0.50x = the HARD CEILING.
# Keys: med = median %/m, p5 = p5 %/m (1-in-20 bad month), dd = p95 max-DD % (staged),
#       sdd = p95 max-DD % under STRESS (all edges at CI lower bound), stress = stress median %/m.
# ALL values verified against data/processed/kelly_1k_stake_sweep_20260619_000854.json. The earlier
# P(month<0)/P(DD>25%) probabilities were DROPPED: inconsistent between Kelly's .md and .json AND
# internally impossible (P(DD>25%) cannot exceed P(DD>p95)=5%). Drawdown PERCENTILES are well-defined.
KELLY_SWEEP = [
    {"f": 0.25, "med":  7.08, "p5":  -4.51, "dd":  9.4, "sdd": 12.0, "stress": 1.94},
    {"f": 0.35, "med":  9.98, "p5":  -6.32, "dd": 12.9, "sdd": 16.5, "stress": 2.66},
    {"f": 0.50, "med": 14.40, "p5":  -9.03, "dd": 18.0, "sdd": 22.7, "stress": 3.68},
    {"f": 0.65, "med": 18.91, "p5": -11.73, "dd": 22.8, "sdd": 28.6, "stress": 4.63},
    {"f": 0.75, "med": 21.97, "p5": -13.53, "dd": 25.9, "sdd": 32.3, "stress": 5.22},
]
KELLY_CEILING = 0.50   # hard ceiling — beyond it the STRESS max-drawdown climbs past ~28%


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


@app.callback(
    Output("sb-profit", "children"), Output("sb-roi", "children"), Output("sb-roi", "style"),
    Output("sb-kelly-band", "children"), Output("sb-note", "children"), Output("sb-risk-metrics", "children"),
    Output("sb-chart", "figure"), Output("sb-fan", "figure"), Output("sb-rr", "figure"),
    Output("sb-dist", "figure"),
    Input("sb-rmse", "value"), Input("sb-cities", "value"), Input("sb-s1trades", "value"),
    Input("sb-lowedge", "value"), Input("sb-lowcities", "value"), Input("sb-lowtrades", "value"),
    Input("sb-lock", "value"), Input("sb-lockpm", "value"),
    Input("sb-bankroll", "value"), Input("sb-kelly", "value"), Input("sb-winrate", "value"),
    Input("sb-slip", "value"))
def _sandbox(rmse, cities, s1tr, low_c, low_cities, low_trades, lock_c, lockpm,
             bankroll, kelly, winrate, slip):
    import numpy as _np
    blank = _tpl(go.Figure(), h=300)
    try:
        rmse = float(rmse); cities = max(0, int(cities)); s1tr = max(0.0, float(s1tr))
        low_c = float(low_c); low_cities = max(0, int(low_cities)); low_trades = max(0.0, float(low_trades))
        lock_c = float(lock_c); lockpm = max(0.0, float(lockpm))
        bankroll = max(0.0, float(bankroll)); kelly = float(kelly)
        winrate = min(99.0, max(1.0, float(winrate))) / 100.0; slip = max(0.0, float(slip))
    except (TypeError, ValueError):
        return ("—", "—", {"color": DIM}, "", "Enter valid numbers in every field.", "",
                blank, blank, blank, blank)
    kelly = min(0.75, max(0.25, kelly))

    # ---- risk band from the embedded Kelly sweep (interpolated) ----
    # The HEADLINE return is ANCHORED to the validated $1k Kelly sweep median %/m (NOT a free-running
    # contracts*trades product, which double-counts and badly overstates the edge). The per-stream
    # breakdown below shows the COMPOSITION of that anchored total, weighted by each stream's modeled
    # gross monthly contribution -- so inputs reshape the mix without inventing an inflated dollar figure.
    k = kelly_interp(kelly)
    # per-stream gross monthly contribution (relative weights only)
    s1_edge_c = max(0.0, S1_EDGE_K * (MKT_SD - rmse) - slip)
    low_edge_c = max(0.0, low_c - slip)
    lock_edge_c = max(0.0, lock_c - slip)
    wr_scale = winrate / 0.55   # win-rate tilts realized edge vs the 55% baseline calibration
    s1_gross = max(0.0, s1tr * cities * s1_edge_c)
    low_gross = max(0.0, low_trades * low_cities * low_edge_c)
    lock_gross = max(0.0, lockpm * cities * lock_edge_c)
    gross_sum = s1_gross + low_gross + lock_gross
    # headline monthly ROI = sweep median %/m, tilted by win-rate vs baseline (kept modest, capped)
    roi = k["med"] * min(2.0, max(0.0, wr_scale))
    total = roi / 100.0 * bankroll if bankroll > 0 else 0.0
    roi_color = MINT if total >= 0 else RED
    # split the anchored $ total across streams by gross weight (composition, not independent sums)
    if gross_sum > 0:
        s1_monthly = total * s1_gross / gross_sum
        low_monthly = total * low_gross / gross_sum
        lock_monthly = total * lock_gross / gross_sum
    else:
        s1_monthly = low_monthly = lock_monthly = 0.0
    ceil_flag = ""
    if kelly > KELLY_CEILING + 1e-9:
        ceil_flag = html.Span("  ABOVE 0.50x HARD CEILING", className="badge bad",
                              style={"marginLeft": "8px"})
    kelly_band = html.Div([
        html.Span(f"Kelly {kelly:.2f}x", className="badge good" if kelly <= KELLY_CEILING else "badge bad"),
        ceil_flag,
        html.Div([f"Sweep (interpolated): median ", html.B(f"{k['med']:+.1f}%/m"),
                  f" · p5 {k['p5']:+.1f}%/m · stress {k['stress']:+.1f}%/m"],
                 className="sub", style={"marginTop": "6px", "fontSize": "11.5px"})])

    metrics = html.Div([
        _risk_metric("Median return", f"{k['med']:+.1f}%/m", "good" if k["med"] > 0 else "bad",
                     "Typical month at this stake (paper model)."),
        _risk_metric("Downside p5", f"{k['p5']:+.1f}%/m", _sev_dd(abs(k["p5"]) * 1.0),
                     "1-in-20 bad month — the soft floor."),
        _risk_metric("p95 max drawdown", f"{k['dd']:.1f}%", _sev_dd(k["dd"]),
                     "Worst peak-to-trough in 19/20 paths."),
        _risk_metric("Stress max drawdown", f"{k['sdd']:.1f}%", _sev_sdd(k["sdd"]),
                     "Worst peak-to-trough if EVERY edge is at its CI lower bound. Hard ceiling 0.50x."),
        _risk_metric("Stress return", f"{k['stress']:+.1f}%/m", "good" if k["stress"] > 0 else "bad",
                     "Median month, all edges at CI lower bound at once.")],
        style={"display": "flex", "flexWrap": "wrap", "gap": "10px"})

    # ---- (a) 12-month Monte-Carlo equity fan ----
    # back out a monthly sigma from p5 (5th pctile of a normal): p5 = med - 1.645*sigma
    mu_m = k["med"] / 100.0
    sigma_m = max(1e-4, (mu_m - k["p5"] / 100.0) / 1.645)
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
    # mark the hard ceiling drawdown (0.50x = 18%)
    rr.add_vline(x=18.0, line=dict(color=AMBER, width=1.4, dash="dash"),
                 annotation_text="0.50x hard ceiling", annotation_position="top",
                 annotation_font=dict(color=AMBER, size=10))
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

    note = (f"Headline ROI is ANCHORED to the validated $1k Kelly sweep (median {k['med']:+.1f}%/m at "
            f"{kelly:.2f}x, edges sized at CI lower bound), tilted by win-rate vs the 55% baseline — it is "
            f"NOT a free-running contracts×trades product (that double-counts and overstates). The breakdown "
            f"splits that anchored total across streams by modeled gross weight: high-S1 edge "
            f"max(0,{S1_EDGE_K}·({MKT_SD}−RMSE)−slip)={s1_edge_c:.1f}c×{s1tr:.0f}/mo×{cities} cities; "
            f"daily-low {low_edge_c:.1f}c×{low_trades:.0f}/mo×{low_cities} cities; lock-in "
            f"{lock_edge_c:.1f}c×{lockpm:.0f}/mo×{cities} cities. Risk band + fan + curve + gauge all read "
            f"the same sweep. Paper/backtest — NOT a guarantee, never realized P&L; LIVE capital today = $0 "
            f"until the forward gates PASS.")
    return (f"${total:,.0f}", f"{roi:+.1f}%", {"color": roi_color}, kelly_band, note, metrics,
            _tpl(fig, h=300, legend=False), _tpl(fan, h=300), _tpl(rr, h=300, legend=False), dist)


def _sev_color(sev):
    return {"good": MINT, "warn": AMBER, "bad": RED}[sev]


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
