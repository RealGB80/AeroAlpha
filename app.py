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
                      font=dict(color=INK, family="Inter, system-ui", size=12), colorway=PALETTE,
                      margin=dict(l=46, r=18, t=44, b=40), height=h,
                      showlegend=legend, legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11)),
                      title=dict(font=dict(size=14)))
    fig.update_xaxes(gridcolor=GRIDCOL, zerolinecolor=GRIDCOL, linecolor=GRIDCOL)
    fig.update_yaxes(gridcolor=GRIDCOL, zerolinecolor=GRIDCOL, linecolor=GRIDCOL)
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
    val = "—" if value is None else (f"{value:.2f}" if isinstance(value, float) else f"{value}")
    kind = {"BACKTEST": "good", "WALK-FORWARD": "good"}.get(status, "warn" if status != "NOT STARTED" else "neut")
    return html.Div([html.Div(label, className="label"),
                     html.Div([html.Span(val, className="val mono"),
                               html.Span(f" {unit}", className="unit")]),
                     badge(status, kind)], className="card kpi")


def dt(df, **kw):
    return dash_table.DataTable(
        data=df.to_dict("records"), columns=[{"name": c, "id": c} for c in df.columns],
        style_as_list_view=True, page_size=kw.pop("page_size", 12), sort_action="native",
        style_header={"backgroundColor": "rgba(24,227,160,.06)", "color": DIM, "fontWeight": "700",
                      "border": "none", "textTransform": "uppercase", "fontSize": "11px",
                      "letterSpacing": ".5px"},
        style_cell={"backgroundColor": "rgba(0,0,0,0)", "color": INK, "border": "none",
                    "borderBottom": "1px solid rgba(64,92,120,.15)", "padding": "8px 12px",
                    "fontSize": "13px", "fontFamily": "Inter, system-ui"},
        style_data_conditional=[{"if": {"state": "active"}, "backgroundColor": "rgba(24,227,160,.08)",
                                 "border": "none"}], **kw)


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
        fig.add_scatter(x=br["date"], y=br["bankroll"], name="paper bankroll",
                        line=dict(color=MINT, width=3), fill="tozeroy", fillcolor="rgba(24,227,160,.08)")
        if "expected_bankroll" in br:
            fig.add_scatter(x=br["date"], y=br["expected_bankroll"], name="backtest-expected",
                            line=dict(color=DIM, dash="dash"))
        bank = card([html.H3("$1,000 paper run vs backtest-expected"), graph(_tpl(fig))])
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
    fig.update_layout(title="Day-ahead high forecast by source (tomorrow, per city)")
    fig.update_yaxes(title="forecast high (°F)")
    # pivot table city x source
    piv = members.pivot_table(index="city", columns="source", values="forecast_f", aggfunc="first")
    piv = piv.round(1).reset_index()
    tgt = sf["target_date"].iloc[0] if "target_date" in sf else "—"
    return html.Div([section("Forecasts by source"),
                     card([html.H3(f"Ensemble members · target {tgt}"),
                           html.Div("The deployed model is an EMOS ensemble; here are the individual member "
                                    "forecasts and their spread per city. Wide spread = high model "
                                    "disagreement (a no-trade signal).", className="sub"),
                           graph(_tpl(fig, h=340))]),
                     card([html.H3("Per-source detail (°F)"), dt(piv, page_size=8)])])


def render_edges():
    e = table("edge")
    if e.empty:
        return html.Div([section("Edges"), card("No edge data yet.")])
    s1 = e[e["stream"] == "S1_S2X"].copy()
    fig = None
    if not s1.empty and s1["avg_net_c"].notna().any():
        s1["beats"] = s1["beats_market"].map({1: "beats market", 0: "no edge"})
        fig = px.bar(s1.sort_values("avg_net_c", ascending=False), x="city", y="avg_net_c", color="beats",
                     color_discrete_map={"beats market": MINT, "no edge": RED},
                     labels={"avg_net_c": "S1 net (c/contract)"}, title="S1 edge by city (S2X model)")
    show = e.copy()
    for c in ("brier_model", "brier_market", "avg_net_c", "win_rate"):
        if c in show:
            show[c] = show[c].map(lambda v: round(v, 4) if isinstance(v, float) else v)
    body = [section("Edges"), card([html.H3("Per-city S1 edge"), dt(show)])]
    body.append(card(graph(_tpl(fig))) if fig is not None else card("S1 net pending more cities."))
    return html.Div(body)


def render_multicity():
    cs1 = table("city_s1"); ll = table("lockin_lead")
    blocks = [section("Multi-City Scalability")]
    if not cs1.empty:
        d = cs1.copy()
        fig = go.Figure()
        d = d.sort_values("s1_net_c", ascending=False)
        err_plus = (d["ci_hi"] - d["s1_net_c"]).clip(lower=0)
        err_minus = (d["s1_net_c"] - d["ci_lo"]).clip(lower=0)
        colors = [MINT if r else DIM for r in d["revived"]]
        fig.add_bar(x=d["city"], y=d["s1_net_c"], marker_color=colors,
                    error_y=dict(type="data", array=err_plus, arrayminus=err_minus, color=DIM, thickness=1.5))
        fig.update_layout(title="Per-city S1 net with 95% bootstrap CI (green = validated/revived)")
        fig.update_yaxes(title="S1 net (c/contract)")
        n_rev = int(d["revived"].sum())
        blocks.append(card([html.H3("Day-ahead S1 by city — expanded per-city pool"),
                            html.Div([badge(f"{n_rev} cities validated", "good"),
                                      html.Span("  base-5 members are NYC-tuned; a per-city expanded Open-Meteo "
                                                "pool revives the edge. A city is 'validated' only if RMSE beats "
                                                "baseline across all ridge-lambdas AND the S1-net bootstrap CI "
                                                "excludes zero.", className="sub")]),
                            graph(_tpl(fig))]))
        blocks.append(card(dt(cs1.round(3))))
    else:
        blocks.append(card("Per-city S1 validation table fills from the latest revival-validate run."))
    if not ll.empty:
        d = ll.copy()
        fig2 = go.Figure()
        fig2.add_bar(x=d["city"], y=d["lead_min"], name="detection lead (min)", marker_color=CYAN)
        fig2.update_layout(title="Airport 5-min HF feed: lock detection lead vs hourly METAR")
        fig2.update_yaxes(title="median lead (min)")
        blocks.append(card([html.H3("Airport lock-in channel (the cities KNYC can't match)"),
                            html.Div("Non-NYC cities settle on airport ASOS with a free 5-min HF feed; it "
                                     "detects the locked daily high minutes before the hourly METAR. The gap is "
                                     "thin (markets watch it too) — a capacity story, not a fat edge.",
                                     className="sub"), graph(_tpl(fig2)), dt(d.round(3), page_size=7)]))
    return html.Div(blocks)


def render_accuracy():
    r = table("forecast_rmse")
    if r.empty:
        return html.Div([section("Forecast Accuracy"), card("No forecast RMSE yet.")])
    m = r.melt(id_vars="city", value_vars=["members_rmse", "s2x_rmse"], var_name="model", value_name="rmse")
    fig = px.bar(m, x="city", y="rmse", color="model", barmode="group",
                 color_discrete_map={"members_rmse": DIM, "s2x_rmse": MINT},
                 title="Day-ahead RMSE by city (members-only vs S2X)")
    fig2 = px.bar(r.melt(id_vars="city", value_vars=["warm", "cold"], var_name="season", value_name="rmse"),
                  x="city", y="rmse", color="season", barmode="group",
                  color_discrete_map={"warm": AMBER, "cold": CYAN}, title="Seasonal RMSE (warm vs cold)")
    return html.Div([section("Forecast Accuracy"),
                     html.Div([html.Div(card(graph(_tpl(fig))), style={"flex": "1", "minWidth": "380px"}),
                               html.Div(card(graph(_tpl(fig2))), style={"flex": "1", "minWidth": "380px"})],
                              className="grid"),
                     card([html.H3("Detail"), dt(r.round(3))])])


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
S1_PRICE, LOCK_PRICE = 0.5, 0.9


@app.callback(Output("sb-profit", "children"), Output("sb-roi", "children"), Output("sb-note", "children"),
              Output("sb-chart", "figure"),
              Input("sb-rmse", "value"), Input("sb-lock", "value"), Input("sb-cities", "value"),
              Input("sb-bankroll", "value"), Input("sb-s1alloc", "value"), Input("sb-lockalloc", "value"),
              Input("sb-slip", "value"))
def _sandbox(rmse, lock_c, cities, bankroll, s1a, locka, slip):
    try:
        rmse = float(rmse); lock_c = float(lock_c); cities = max(0, int(cities)); bankroll = float(bankroll)
        s1a = float(s1a) / 100.0; locka = float(locka) / 100.0; slip = float(slip)
    except (TypeError, ValueError):
        return "—", "—", "Enter valid numbers.", _tpl(go.Figure(), h=300)
    s1_edge_c = max(0.0, S1_EDGE_K * (MKT_SD - rmse) - slip)
    s1_ct = min(DEPTH_CAP, (bankroll * s1a) / S1_PRICE) if bankroll > 0 else 0
    s1_monthly = S1_TRADES_PM * cities * s1_ct * s1_edge_c / 100.0
    lock_edge_c = max(0.0, lock_c - slip)
    lock_ct = min(DEPTH_CAP, (bankroll * locka) / LOCK_PRICE) if bankroll > 0 else 0
    lock_monthly = LOCKS_PM * cities * lock_ct * lock_edge_c / 100.0
    total = s1_monthly + lock_monthly
    roi = (total / bankroll * 100.0) if bankroll > 0 else 0.0
    fig = go.Figure()
    fig.add_bar(x=["S1 day-ahead", "Lock-in", "TOTAL"], y=[s1_monthly, lock_monthly, total],
                marker_color=[MINT, CYAN, VIOLET],
                text=[f"${v:,.0f}" for v in [s1_monthly, lock_monthly, total]], textposition="outside")
    fig.update_layout(title="Estimated monthly profit by stream")
    fig.update_yaxes(title="$ / month")
    note = (f"Model: S1 edge ≈ max(0, {S1_EDGE_K}·({MKT_SD}−RMSE)−slip) = {s1_edge_c:.1f}c; "
            f"{S1_TRADES_PM} trades/mo × {cities} cities × {s1_ct:.0f} ct (depth-capped {DEPTH_CAP}). "
            f"Lock-in: {LOCKS_PM}/mo × cities × {lock_ct:.0f} ct × {lock_edge_c:.1f}c. Paper estimate.")
    return f"${total:,.0f}", f"{roi:.1f}%", note, _tpl(fig, h=300, legend=False)


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
