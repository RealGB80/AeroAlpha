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

# ---- palette (mirrors assets/theme.css) ----
BG, PANEL, INK, DIM = "#05080d", "rgba(15,23,34,0.72)", "#e8f1f8", "#7e93a8"
MINT, CYAN, VIOLET, AMBER, RED = "#18e3a0", "#38bdf8", "#a78bfa", "#fbbf24", "#fb6a6a"
PALETTE = [MINT, CYAN, VIOLET, AMBER, "#f472b6", RED]
GRIDCOL = "rgba(64,92,120,0.18)"

NAV = [("overview", "◉", "Overview"), ("forecasts", "☉", "Forecasts"),
       ("edges", "↑", "Edges"), ("multicity", "▦", "Multi-City"),
       ("accuracy", "◎", "Forecast Accuracy"), ("forward", "✓", "Forward Validation"),
       ("sandbox", "⚙", "Sandbox"), ("risk", "⚠", "Risk & Honesty"),
       ("methodology", "≡", "Methodology")]


def _tpl(fig, h=300, legend=True):
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color=INK, family="Inter, system-ui", size=12.5), colorway=PALETTE,
                      margin=dict(l=62, r=24, t=58, b=48), height=h,
                      showlegend=legend,
                      legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=DIM), orientation="h",
                                  yanchor="bottom", y=1.04, xanchor="left", x=0, title_text="",
                                  itemsizing="constant"),
                      title=dict(font=dict(size=14.5, color=INK, family="Inter, system-ui"),
                                 x=0, xanchor="left", y=0.98, pad=dict(b=6)),
                      hoverlabel=dict(bgcolor="rgba(8,13,22,.96)", bordercolor="rgba(24,227,160,.35)",
                                      font=dict(family="Inter, system-ui", size=12.5, color=INK),
                                      align="left"),
                      bargap=0.42, bargroupgap=0.16, uniformtext=dict(mode="hide", minsize=9))
    fig.update_xaxes(gridcolor="rgba(0,0,0,0)", zerolinecolor="rgba(126,147,168,.30)",
                     linecolor=GRIDCOL, showline=True, ticks="outside", ticklen=5, tickcolor=GRIDCOL,
                     tickfont=dict(size=12, color=DIM),
                     title_font=dict(size=11.5, color=DIM), automargin=True)
    fig.update_yaxes(gridcolor=GRIDCOL, griddash="dot", zerolinecolor="rgba(126,147,168,.30)",
                     linecolor="rgba(0,0,0,0)", showline=False, ticks="", ticklen=0,
                     tickfont=dict(size=12, color=DIM),
                     title_font=dict(size=11.5, color=DIM), automargin=True)
    # rounded bar corners + crisp outline on every bar trace (investor-grade, not raw plotly)
    fig.update_traces(selector=dict(type="bar"),
                      marker=dict(cornerradius=6, line=dict(width=0)),
                      textfont=dict(family="JetBrains Mono, monospace", size=11.5))
    return fig


def graph(fig):
    return dcc.Graph(figure=fig, config={"displayModeBar": False})


def card(children, cls="", **kw):
    return html.Div(children, className=f"card {cls}".strip(), **kw)


def badge(text, kind="neut"):
    return html.Span(text, className=f"badge {kind}")


def section(title):
    return html.Div(title, className="page-title")


def kpi_card(label, value, unit, status):
    # clean value + unit formatting by unit type; drop redundant unit suffix for $ / cities
    suffix = f" {unit}"
    if value is None or (isinstance(value, float) and value != value):
        val = "—"
    elif unit == "$":
        val, suffix = f"${value:,.0f}", ""
    elif unit == "c/contract":
        val, suffix = f"{value:+.2f}", " c/ct"
    elif unit in ("F", "°F"):
        val, suffix = f"{value:.2f}", " °F"
    elif unit == "cities":
        val, suffix = f"{int(round(value))}", " cities"
    elif isinstance(value, float):
        val = f"{value:.2f}"
    else:
        val = f"{value}"
    kind = {"BACKTEST": "good", "WALK-FORWARD": "good"}.get(status, "warn" if status != "NOT STARTED" else "neut")
    return html.Div([html.Div(label, className="label"),
                     html.Div([html.Span(val, className="val mono"),
                               html.Span(suffix, className="unit")]),
                     badge(status, kind)], className="card kpi")


# ---- presentation helpers (clean number formatting + human-readable headers) ----
# raw store column -> (Title-Case header, formatter). Unmapped columns fall back to Title Case + str.
def _f2(v):    # 2-decimal float
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:.2f}"


def _f1(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:.1f}"


def _cents(v):   # signed cents
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:+.2f}c"


def _cents1(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:+.1f}c"


def _degf(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:.2f}°F"


def _pct01(v):   # 0..1 -> %
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{100 * v:.1f}%"


def _brier(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:.4f}"


def _intf(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:,.0f}"


def _minf(v):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v:.0f} min"


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


# ============================== PAGES ==============================
def render_overview():
    kpi = table("kpi"); br = table("bankroll_run"); cs1 = table("city_s1")
    cards = [kpi_card(r["label"], r["value"], r["unit"], r["status"]) for _, r in kpi.iterrows()] \
        if not kpi.empty else [card("no KPIs yet")]
    # bankroll
    if br.empty:
        bank = card([html.H3("$1,000 paper run"),
                     html.Div("NOT STARTED — wired and ready. The bankroll curve vs the backtest-expected "
                              "path appears here automatically once the run logs its first settled day.",
                              className="sub")])
    else:
        fig = go.Figure()
        fig.add_scatter(x=br["date"], y=br["bankroll"], name="Paper bankroll", mode="lines",
                        line=dict(color=MINT, width=2.6, shape="spline", smoothing=0.5),
                        fill="tozeroy", fillcolor="rgba(24,227,160,.07)",
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
                              className="grid")])


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
                     present_df=False)])]
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
                              present_df=False)])])


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
    def field(id_, label, val, step="any"):
        return html.Div([html.Label(label), dcc.Input(id=id_, type="number", value=val, step=step)],
                        className="sb-field")
    inputs = card([html.H3("Scenario inputs"),
                   field("sb-rmse", "Day-ahead RMSE (°F)", 1.66, 0.01),
                   field("sb-lock", "Lock-in edge (c/contract)", 12, 0.5),
                   field("sb-cities", "Number of S1 cities", 3, 1),
                   html.Div("Daily-LOW S1 (newly validated, NYC)", className="sub",
                            style={"margin": "14px 0 2px", "color": MINT, "fontWeight": "700"}),
                   field("sb-lowedge", "Daily-low S1 edge (c/contract)", 6.96, 0.1),
                   field("sb-lowcities", "Daily-low S1 cities", 1, 1),
                   field("sb-lowtrades", "Daily-low trades / month / city", 82, 1),
                   html.Div("Capital & costs", className="sub",
                            style={"margin": "14px 0 2px", "color": DIM, "fontWeight": "700"}),
                   field("sb-bankroll", "Bankroll ($)", 5000, 100),
                   field("sb-s1alloc", "S1 allocation per trade (%)", 0.5, 0.1),
                   field("sb-lockalloc", "Lock-in allocation per lock (%)", 3.0, 0.1),
                   field("sb-slip", "Slippage (c/contract)", 1.0, 0.5)],
                  style={"flex": "1", "minWidth": "290px"})
    out = card([html.H3("Projected monthly result"),
                html.Div("Estimated monthly profit", className="sub"),
                html.Div(id="sb-profit", className="sb-out", style={"color": MINT}),
                html.Div("Monthly ROI on bankroll", className="sub", style={"marginTop": "10px"}),
                html.Div(id="sb-roi", className="sb-out", style={"color": CYAN}),
                html.Div(id="sb-note", className="sub", style={"marginTop": "12px"})],
               style={"flex": "1", "minWidth": "290px"})
    chartc = card([html.H3("Profit breakdown"), graph_placeholder := dcc.Graph(id="sb-chart",
                   config={"displayModeBar": False})], style={"flex": "1.4", "minWidth": "340px"})
    return html.Div([section("Sandbox — paper profitability model"),
                     html.Div("Interactive estimate. Transparent model (see note); NOT a guarantee — paper "
                              "research only. Edit any input.", className="sub", style={"marginBottom": "10px"}),
                     html.Div([inputs, out, chartc], className="grid")])


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
                                     html.Div(d, className="sub")]) for t, d, k in items])])


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
    dcc.Interval(id="tick", interval=60_000, n_intervals=0),
    topbar(),
    html.Div(className="shell", children=[sidebar(), html.Div(id="main", className="main")]),
])


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
S1_TRADES_PM = 84      # per city per month (NY-measured)
LOCKS_PM = 14          # per city per month (~0.5/day, conservative)
DEPTH_CAP = 250        # contracts fillable within slippage (measured median)
S1_PRICE, LOCK_PRICE, LOW_PRICE = 0.5, 0.9, 0.5
# Daily-LOW S1 (KXLOWTNYC) measured defaults: net +6.96c/ct, ~82 trades/mo, NYC validated (CHI = watch).
# Modeled as a fixed measured net (orthogonal overnight signal), slippage-adjusted like the high-S1 leg.


@app.callback(Output("sb-profit", "children"), Output("sb-roi", "children"), Output("sb-note", "children"),
              Output("sb-chart", "figure"),
              Input("sb-rmse", "value"), Input("sb-lock", "value"), Input("sb-cities", "value"),
              Input("sb-lowedge", "value"), Input("sb-lowcities", "value"), Input("sb-lowtrades", "value"),
              Input("sb-bankroll", "value"), Input("sb-s1alloc", "value"), Input("sb-lockalloc", "value"),
              Input("sb-slip", "value"))
def _sandbox(rmse, lock_c, cities, low_c, low_cities, low_trades, bankroll, s1a, locka, slip):
    try:
        rmse = float(rmse); lock_c = float(lock_c); cities = max(0, int(cities))
        low_c = float(low_c); low_cities = max(0, int(low_cities)); low_trades = max(0.0, float(low_trades))
        bankroll = float(bankroll)
        s1a = float(s1a) / 100.0; locka = float(locka) / 100.0; slip = float(slip)
    except (TypeError, ValueError):
        return "—", "—", "Enter valid numbers.", _tpl(go.Figure(), h=300)
    # day-ahead high S1
    s1_edge_c = max(0.0, S1_EDGE_K * (MKT_SD - rmse) - slip)
    s1_ct = min(DEPTH_CAP, (bankroll * s1a) / S1_PRICE) if bankroll > 0 else 0
    s1_monthly = S1_TRADES_PM * cities * s1_ct * s1_edge_c / 100.0
    # daily-low S1 (measured net, slippage-adjusted; same allocation/depth model as the high leg)
    low_edge_c = max(0.0, low_c - slip)
    low_ct = min(DEPTH_CAP, (bankroll * s1a) / LOW_PRICE) if bankroll > 0 else 0
    low_monthly = low_trades * low_cities * low_ct * low_edge_c / 100.0
    # lock-in
    lock_edge_c = max(0.0, lock_c - slip)
    lock_ct = min(DEPTH_CAP, (bankroll * locka) / LOCK_PRICE) if bankroll > 0 else 0
    lock_monthly = LOCKS_PM * cities * lock_ct * lock_edge_c / 100.0
    total = s1_monthly + low_monthly + lock_monthly
    roi = (total / bankroll * 100.0) if bankroll > 0 else 0.0
    fig = go.Figure()
    labels = ["S1 day-ahead high", "Daily-low S1", "Lock-in", "TOTAL"]
    vals = [s1_monthly, low_monthly, lock_monthly, total]
    fig.add_bar(x=labels, y=vals, marker_color=[MINT, VIOLET, CYAN, AMBER], width=0.62,
                text=[f"${v:,.0f}" for v in vals], textposition="outside", cliponaxis=False,
                hovertemplate="%{x}<br>%{y:$,.0f} / month<extra></extra>")
    fig.update_layout(title="Estimated Monthly Profit by Stream")
    _vmax = max(vals + [1])
    fig.update_yaxes(title="paper profit ($ / month)", tickprefix="$", tickformat=",.0f",
                     range=[0, _vmax * 1.18])
    note = (f"Transparent paper model. High S1 edge ≈ max(0, {S1_EDGE_K}·({MKT_SD}−RMSE)−slip) "
            f"= {s1_edge_c:.1f}c; {S1_TRADES_PM} trades/mo × {cities} cities × {s1_ct:.0f} ct "
            f"(depth-capped {DEPTH_CAP}). Daily-low S1: measured net {low_c:.2f}c − slip = "
            f"{low_edge_c:.1f}c; {low_trades:.0f} trades/mo × {low_cities} city/cities × {low_ct:.0f} ct "
            f"(defaults = NYC KXLOWTNYC measured: +6.96c, ~82/mo, validated; CHI-low = watch). "
            f"Lock-in: {LOCKS_PM}/mo × {cities} cities × {lock_ct:.0f} ct × {lock_edge_c:.1f}c. "
            f"Paper/backtest estimate only — NOT a guarantee, never realized P&L.")
    return f"${total:,.0f}", f"{roi:+.1f}%", note, _tpl(fig, h=320, legend=False)


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
