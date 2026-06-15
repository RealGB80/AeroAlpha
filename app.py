"""
Investor dashboard -- Plotly Dash, hosted, password-gated.

Reads ONLY the curated store (dashboard_app/data.py). PAPER ONLY: no Kalshi auth/orders/real money;
the dashboard's own login is unrelated to Kalshi. Multi-account capable (env DASH_USERS), one login now.

Pages: Overview / Edges / Scalability / Forecast / Forward validation / Risk & honesty / Methodology.
Edges + Scalability auto-fill as multi-city Stage B lands; the Overview bankroll chart auto-fills when
the $1,000 paper run writes rows -- no code change needed (pure data plug-in).

Run locally:   python dashboard_app/app.py     (then open http://127.0.0.1:8050, login from DASH_USERS)
Deploy:        gunicorn dashboard_app.app:server   (see README_DEPLOY.md)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # make `data` importable any launch path

import dash
import dash_auth
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, dash_table, Input, Output

from data import table, meta_value

# ---- theme ----
BG, CARD, INK, DIM, ACC, GRN, AMB, RED = "#0d141f", "#141c2b", "#e6ebf3", "#93a0b8", "#2f6feb", "#2da44e", "#d4a72c", "#cf222e"
PALETTE = [ACC, GRN, AMB, "#a855f7", "#06b6d4", RED]
TAB_STYLE = {"backgroundColor": CARD, "color": DIM, "border": f"1px solid #2a3650", "padding": "8px 14px"}
TAB_SEL = {"backgroundColor": ACC, "color": "#fff", "border": f"1px solid {ACC}", "padding": "8px 14px", "fontWeight": "700"}


def _tpl(fig):
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font_color=INK, margin=dict(l=40, r=20, t=40, b=40), colorway=PALETTE)
    return fig


def card(children, **kw):
    style = {"backgroundColor": CARD, "border": "1px solid #2a3650", "borderRadius": "12px",
             "padding": "14px 16px", "margin": "8px", "boxShadow": "0 4px 14px rgba(0,0,0,.3)"}
    style.update(kw.pop("style", {}))
    return html.Div(children, style=style, **kw)


def kpi_card(label, value, unit, status):
    val = "—" if value is None else (f"{value:.2f}" if isinstance(value, float) else f"{value}")
    color = {"BACKTEST": ACC, "WALK-FORWARD": ACC, "NOT STARTED": DIM}.get(status, AMB)
    return card([html.Div(label, style={"color": DIM, "fontSize": "12px", "textTransform": "uppercase",
                                        "letterSpacing": ".5px"}),
                 html.Div([html.Span(val, style={"fontSize": "26px", "fontWeight": "800"}),
                           html.Span(f" {unit}", style={"color": DIM, "fontSize": "13px"})]),
                 html.Div(status, style={"color": color, "fontSize": "11px", "fontWeight": "700"})],
                style={"flex": "1", "minWidth": "180px"})


def dt(df, **kw):
    return dash_table.DataTable(
        data=df.to_dict("records"), columns=[{"name": c, "id": c} for c in df.columns],
        style_as_list_view=True, style_header={"backgroundColor": "#1b2536", "color": DIM,
        "fontWeight": "700", "border": "none"}, style_cell={"backgroundColor": CARD, "color": INK,
        "border": "none", "padding": "6px 10px", "fontSize": "13px",
        "fontFamily": "Segoe UI, system-ui"}, **kw)


def render_overview():
    kpi = table("kpi"); br = table("bankroll_run")
    cards = [kpi_card(r["label"], r["value"], r["unit"], r["status"]) for _, r in kpi.iterrows()] \
        if not kpi.empty else [html.Div("no KPIs yet", style={"color": DIM})]
    if br.empty:
        bank = card([html.H3("$1,000 paper run", style={"marginTop": 0}),
                     html.Div("NOT STARTED — wired and ready. The bankroll curve (vs the backtest-expected "
                              "path) appears here automatically once the run logs its first settled day.",
                              style={"color": DIM})])
    else:
        fig = go.Figure()
        fig.add_scatter(x=br["date"], y=br["bankroll"], name="paper bankroll", line=dict(color=GRN, width=3))
        if "expected_bankroll" in br:
            fig.add_scatter(x=br["date"], y=br["expected_bankroll"], name="backtest-expected",
                            line=dict(color=DIM, dash="dash"))
        bank = card([html.H3("$1,000 paper run vs backtest-expected", style={"marginTop": 0}),
                     dcc.Graph(figure=_tpl(fig), config={"displayModeBar": False})])
    return html.Div([html.Div(cards, style={"display": "flex", "flexWrap": "wrap"}), bank])


def render_edges():
    e = table("edge")
    if e.empty:
        return card("No edge data yet (multi-city Stage B pending).")
    s1 = e[e["stream"] == "S1_S2X"].copy()
    fig = None
    if not s1.empty and s1["avg_net_c"].notna().any():
        s1["beats"] = s1["beats_market"].map({1: "beats market", 0: "no edge"})
        fig = px.bar(s1.sort_values("avg_net_c", ascending=False), x="city", y="avg_net_c", color="beats",
                     color_discrete_map={"beats market": GRN, "no edge": RED},
                     labels={"avg_net_c": "S1 net (c/contract)"}, title="S1 edge by city (S2X model)")
    show = e.copy()
    for c in ("brier_model", "brier_market", "avg_net_c", "win_rate"):
        if c in show:
            show[c] = show[c].map(lambda v: round(v, 4) if isinstance(v, float) else v)
    return html.Div([card([html.H3("Per-city S1 edge", style={"marginTop": 0}), dt(show)]),
                     card(dcc.Graph(figure=_tpl(fig), config={"displayModeBar": False})) if fig is not None
                     else card("S1 net pending more cities (Stage B running).")])


def render_scalability():
    e = table("edge")
    s1 = e[e["stream"] == "S1_S2X"].copy() if not e.empty else e
    n_beat = int(s1["beats_market"].sum()) if not s1.empty else 0
    note = card([html.H3("Scalability — multi-city capacity", style={"marginTop": 0}),
                 html.P([html.B(f"{n_beat}"), " cities currently show an S1 edge beating their market. ",
                         "Single-city capacity is ~$150/mo (depth-capped); capacity is roughly ADDITIVE "
                         "across qualifying, weakly-correlated cities (separate order books) — that is the "
                         "scalability multiplier over single-city. Full per-city $ capacity fills in here "
                         "once Stage B + per-city depth complete."], style={"color": DIM})])
    fig = None
    if not s1.empty and s1["avg_net_c"].notna().any():
        q = s1[s1["beats_market"] == 1].sort_values("avg_net_c", ascending=False)
        if not q.empty:
            fig = px.bar(q, x="city", y="avg_net_c", labels={"avg_net_c": "S1 net (c/contract)"},
                         title="Qualifying cities (S1 edge) — capacity stack proxy")
    return html.Div([note, card(dcc.Graph(figure=_tpl(fig), config={"displayModeBar": False}))
                     if fig is not None else card("Capacity stack populates as qualifying cities land.",
                                                  style={"color": DIM})])


def render_forecast():
    r = table("forecast_rmse")
    if r.empty:
        return card("No forecast RMSE yet.")
    m = r.melt(id_vars="city", value_vars=["members_rmse", "s2x_rmse"], var_name="model", value_name="rmse")
    fig = px.bar(m, x="city", y="rmse", color="model", barmode="group",
                 color_discrete_map={"members_rmse": DIM, "s2x_rmse": ACC},
                 title="Day-ahead RMSE by city (members-only vs S2X)")
    fig2 = px.bar(r.melt(id_vars="city", value_vars=["warm", "cold"], var_name="season", value_name="rmse"),
                  x="city", y="rmse", color="season", barmode="group",
                  color_discrete_map={"warm": AMB, "cold": ACC}, title="Seasonal RMSE (warm vs cold)")
    return html.Div([card(dcc.Graph(figure=_tpl(fig), config={"displayModeBar": False})),
                     card(dcc.Graph(figure=_tpl(fig2), config={"displayModeBar": False})),
                     card([html.H3("Detail", style={"marginTop": 0}), dt(r.round(3))])])


def render_forward():
    g = table("forward_gate")
    if g.empty:
        return card("No forward-gate data yet.")
    bars = []
    for _, r in g.iterrows():
        pct = min(100, int(100 * (r["n_settled"] or 0) / max(r["n_required"] or 1, 1)))
        cp = f" · since {r['changepoint_date']}" if r.get("changepoint_date") else ""
        bars.append(html.Div([
            html.Div([html.B(r["stream"]), html.Span(f"  {r['n_settled']}/{r['n_required']} settled{cp}",
                      style={"color": DIM, "fontSize": "12px"})]),
            html.Div(html.Div(style={"width": f"{pct}%", "height": "10px", "backgroundColor": ACC,
                     "borderRadius": "5px"}), style={"backgroundColor": "#1b2536", "borderRadius": "5px",
                     "height": "10px", "margin": "4px 0 10px"})]))
    return card([html.H3("Forward-validation gates (pre-registered)", style={"marginTop": 0}),
                 html.P("Pre-registered thresholds (docs/FORWARD_PROTOCOL.md). Day-ahead streams reset at "
                        "the 2026-06-14 S2X changepoint. All ACCUMULATING — not yet a proven live edge.",
                        style={"color": DIM, "fontSize": "12px"}), *bars])


def render_risk():
    items = [("Capacity ceiling", "The edge is depth-capacity-bounded; absolute $ has a ceiling that does "
              "NOT grow with bankroll. Scalability comes from MORE CITIES, not more capital per city."),
             ("Fills are the gating unknown", "Edges modeled at ≤~1c slippage; if real fills are worse, the "
              "edge shrinks. Forward fill validation is in progress."),
             ("Paper only", "No authentication, no orders, no account, no real money — anywhere. Every figure "
              "is a paper/backtest/forward estimate, never realized P&L."),
             ("Single-station concentration", "All edges are one city's daily high → treat as one risk unit; "
              "multi-city adds real diversification only across distinct, weakly-correlated cities."),
             ("Lock-in = parity", "The same-day lock-in 'speed edge' is parity, not advantage (no public feed "
              "beats the market to the obs) — deprioritized.")]
    return html.Div([card([html.H4(t, style={"marginTop": 0, "color": AMB}), html.Div(d, style={"color": DIM})])
                     for t, d in items])


def render_methodology():
    m = table("methodology")
    return card([html.H3("Methodology & provenance", style={"marginTop": 0}),
                 dt(m) if not m.empty else html.Div("—", style={"color": DIM})])


RENDER = {"overview": render_overview, "edges": render_edges, "scalability": render_scalability,
          "forecast": render_forecast, "forward": render_forward, "risk": render_risk,
          "methodology": render_methodology}

app = Dash(__name__, title="KXHIGH Research — Investor View")
server = app.server  # for gunicorn

# ---- auth (multi-account capable; env DASH_USERS="user:pass,user2:pass2"; one login for now) ----
_users = {}
for pair in os.environ.get("DASH_USERS", "investor:changeme").split(","):
    if ":" in pair:
        u, p = pair.split(":", 1)
        _users[u.strip()] = p.strip()
dash_auth.BasicAuth(app, _users)

app.layout = html.Div(style={"backgroundColor": BG, "minHeight": "100vh", "color": INK,
                             "fontFamily": "Segoe UI, system-ui", "padding": "0 0 30px"}, children=[
    html.Div(style={"background": "linear-gradient(120deg,#0b2447,#1d4f93)", "padding": "14px 22px",
                    "display": "flex", "alignItems": "center", "gap": "14px", "flexWrap": "wrap"}, children=[
        html.Div("KXHIGH Weather-Market Research", style={"fontWeight": "800", "fontSize": "18px"}),
        html.Span("PAPER ONLY — no orders, no real money", style={"backgroundColor": "rgba(255,255,255,.15)",
                  "padding": "3px 10px", "borderRadius": "20px", "fontSize": "11px", "fontWeight": "700"}),
        html.Span(id="status-pill", style={"backgroundColor": "rgba(255,255,255,.15)", "padding": "3px 10px",
                  "borderRadius": "20px", "fontSize": "11px"})]),
    dcc.Tabs(id="tabs", value="overview", children=[
        dcc.Tab(label=l, value=v, style=TAB_STYLE, selected_style=TAB_SEL) for v, l in [
            ("overview", "Overview"), ("edges", "Edges"), ("scalability", "Scalability"),
            ("forecast", "Forecast accuracy"), ("forward", "Forward validation"),
            ("risk", "Risk & honesty"), ("methodology", "Methodology")]],
        style={"padding": "8px 14px"}),
    html.Div(id="tab-content", style={"padding": "6px 12px"}),
])


@app.callback(Output("tab-content", "children"), Output("status-pill", "children"), Input("tabs", "value"))
def _route(tab):
    status = f"integrity: {meta_value('integrity_verdict')} · updated {meta_value('generated_at_utc')}"
    return RENDER.get(tab, render_overview)(), status


if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=8050)
