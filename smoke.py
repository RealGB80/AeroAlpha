"""Render-all smoke gate for the AeroAlpha dashboard.

Builds every page in RENDER, dict-ifies its figures the way the request path does, JSON-serializes the
tree with Plotly's encoder (the same load-or-break serialization the app relies on), and reports per-page
timing + payload size. Exit 0 iff every page renders and serializes.

Run:  set DASH_PREWARM=0 && python dashboard_app/smoke.py
      (or)  DASH_PREWARM=0 python dashboard_app/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("DASH_PREWARM", "0")   # never start the background prewarmer during a smoke run
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import plotly.utils  # noqa: E402  (PlotlyJSONEncoder lives here)

import app as A  # noqa: E402


def main() -> int:
    fail = 0
    total_mb = 0.0
    print(f"smoke: {len(A.RENDER)} pages  (DASH_PREWARM={os.environ.get('DASH_PREWARM')})\n")
    for key, fn in A.RENDER.items():
        t0 = time.time()
        try:
            tree = fn()
            A._dictify_figures(tree)
            payload = json.dumps(tree, cls=plotly.utils.PlotlyJSONEncoder, default=str)
            mb = len(payload) / 1e6
            total_mb += mb
            print(f"OK    {key:<12} {time.time() - t0:5.2f}s  {mb:6.2f} MB")
        except Exception as e:               # noqa: BLE001 -- smoke wants the failure surfaced, not raised
            fail += 1
            import traceback
            print(f"FAIL  {key:<12} {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'FAIL' if fail else 'PASS'}: {len(A.RENDER) - fail}/{len(A.RENDER)} pages, {total_mb:.2f} MB total")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
