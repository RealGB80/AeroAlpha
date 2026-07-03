"""AeroAlpha component library v2 (redesign infrastructure, 2026-07-02).

NOT imported by app.py yet — inert until the WP-03 design-system pass adopts it. Pairs with
assets/tokens.css (panel2 / info2 / scope-chip / drawer classes live there).

Design contract (docs/dashboard_redesign_20260702/01_MASTER_PLAN.md in the private research repo):
- panel(): the ONE card scaffold — header (title + badges + actions + info), body, one-line caption,
  and a collapsible "Methodology & caveats" drawer that holds the full honest caveat text verbatim.
- info(): CSS tooltip (keyboard-focusable), replaces native title= info dots.
- scope_bar(): the multi-city scope selector (chips; pattern-matching ids for one callback).
- icon(): tiny inline-SVG set via data-URI <img> (no new deps; fixed color per call — active-state
  emphasis comes from text/inset styling, not icon recolor).
PAPER-ONLY project: components carry no data semantics; honesty text is the caller's responsibility.
"""
from __future__ import annotations

from urllib.parse import quote

from dash import html

DIM = "#8a949b"

# stroke-based 24x24 paths (lucide-style, hand-trimmed). name -> list of path d= strings.
_ICON_PATHS = {
    "overview":    ["M3 3h8v8H3z", "M13 3h8v5h-8z", "M13 10h8v11h-8z", "M3 13h8v8H3z"],
    "run":         ["M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z", "M15 9c-.6-1-1.6-1.5-3-1.5-2 0-3 1-3 2.2 0 3 6 1.6 6 4.6 0 1.2-1 2.2-3 2.2-1.4 0-2.4-.5-3-1.5", "M12 6v2", "M12 16v2"],
    "markets":     ["M22 12h-4l-3 9L9 3l-3 9H2"],
    "model":       ["M12 2v4", "M12 18v4", "M2 12h4", "M18 12h4", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z"],
    "edges":       ["M3 17l6-6 4 4 8-8", "M14 7h7v7"],
    "capacity":    ["M12 3l9 5-9 5-9-5 9-5z", "M3 13l9 5 9-5", "M3 17l9 5 9-5"],
    "lab":         ["M9 3h6", "M10 3v6L4.5 19a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 9V3", "M7 15h10"],
    "methodology": ["M4 19.5A2.5 2.5 0 0 1 6.5 17H20", "M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"],
    "clock":       ["M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z", "M12 6v6l4 2"],
    "alert":       ["M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z", "M12 9v4", "M12 17h.01"],
    "check":       ["M20 6L9 17l-5-5"],
    "chevron":     ["M9 18l6-6-6-6"],
    "external":    ["M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6", "M15 3h6v6", "M10 14L21 3"],
}


def icon(name: str, size: int = 16, color: str = DIM, stroke: float = 1.8):
    """Inline-SVG icon as a data-URI <img>. Fixed color per call (an <img> can't inherit
    currentColor) — use dim for chrome, the accent only for genuinely-live markers."""
    paths = _ICON_PATHS.get(name) or _ICON_PATHS["chevron"]
    body = "".join(f'<path d="{d}"/>' for d in paths)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
           f'stroke-linejoin="round">{body}</svg>')
    return html.Img(src="data:image/svg+xml," + quote(svg), draggable="false",
                    style={"width": f"{size}px", "height": f"{size}px", "display": "inline-block",
                           "verticalAlign": "-2px"})


def chip(text, kind: str = "neut"):
    """Status chip. kind: good | warn | bad | neut (badge classes from theme.css)."""
    return html.Span(text, className=f"badge {kind}")


def info(tip: str):
    """Keyboard-focusable CSS tooltip (tokens.css .info2). Replaces native title= dots."""
    return html.Span("i", className="info2", tabIndex="0", **{"data-tip": tip})


def caption_drawer(long_text, summary: str = "Methodology & caveats"):
    """Collapsible drawer for the FULL honest caveat text (kept verbatim, one click away)."""
    return html.Details([html.Summary(summary), html.Div(long_text)], className="drawer")


def stat(label: str, value, sub=None, color: str = "var(--ink)", big: bool = False):
    """Compact metric tile: uppercase micro-label, mono value, optional sub-line."""
    kids = [html.Div(label, className="u-label"),
            html.Div(value, className="mono",
                     style={"fontSize": "var(--fs-xl2)" if big else "19px",
                            "fontWeight": "800", "color": color})]
    if sub is not None:
        kids.append(html.Div(sub, className="sub", style={"fontSize": "var(--fs-xs2)"}))
    return html.Div(kids, className="metric-tile")


def signed(v: float, fmt: str = "{:+.2f}"):
    """Sign-glyph string so color is never the only encoding: '▲ +1.23' / '▼ -0.50'."""
    if v is None or v != v:
        return "—"
    glyph = "▲" if v > 0 else ("▼" if v < 0 else "▬")
    return f"{glyph} {fmt.format(v)}"


def empty_state2(msg: str, hint: str | None = None, icon_name: str = "clock"):
    """Empty panel body: icon + what's missing + (optionally) what fills it."""
    kids = [html.Div(icon(icon_name, size=24), className="es-ic"),
            html.Div(msg, className="es-msg")]
    if hint:
        kids.append(html.Div(hint, className="sub", style={"fontSize": "var(--fs-xs2)",
                                                           "opacity": ".75"}))
    return html.Div(kids, className="empty-state")


def panel(title, children, badges=None, caption=None, drawer=None, actions=None,
          id=None, cls: str = ""):
    """THE card scaffold (WP-03). Header row = title + badges left, actions (selector/info) right;
    then the ONE-line caption; then the body; then the collapsed caveats drawer."""
    head_left = html.Div([html.H3(title, className="p2-title")]
                         + ([html.Div(badges, className="chip-row")] if badges else []),
                         className="row-baseline")
    head = html.Div([head_left] + ([html.Div(actions, className="row-center")] if actions else []),
                    className="p2-head")
    kids = [head]
    if caption:
        kids.append(html.Div(caption, className="p2-cap"))
    kids.extend(children if isinstance(children, (list, tuple)) else [children])
    if drawer:
        kids.append(caption_drawer(drawer))
    kw = {"id": id} if id else {}
    return html.Div(kids, className=f"card panel2 {cls}".strip(), **kw)


def scope_bar(scopes, active: str = "ALL", include_all: bool = True):
    """Multi-city scope selector: chip row with pattern-matching ids. One callback serves it:
        Input({"type": "scope-chip", "key": ALL}, "n_clicks") -> the chosen scope key.
    `scopes` = stream ids like 'NY_high'; labels render as 'NY · high'."""
    def _label(s):
        parts = str(s).rsplit("_", 1)
        return f"{parts[0]} · {parts[1]}" if len(parts) == 2 else str(s)
    keys = (["ALL"] if include_all else []) + list(scopes)
    chips = [html.Button(_label(k) if k != "ALL" else "ALL",
                         id={"type": "scope-chip", "key": k}, n_clicks=0,
                         className="scope-chip active" if k == active else "scope-chip",
                         **{"aria-pressed": "true" if k == active else "false"})
             for k in keys]
    return html.Div(chips, className="scope-bar", role="tablist")
