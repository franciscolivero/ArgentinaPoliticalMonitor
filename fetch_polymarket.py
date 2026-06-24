#!/usr/bin/env python3
"""
fetch_polymarket.py — snapshot the Argentina presidential-winner market history.

Runs server-side (e.g. from a GitHub Action), so it is NOT subject to browser
CORS. It reads the Polymarket Gamma API to discover each candidate's CLOB token,
pulls the daily price history per candidate, and writes polymarket_history.json
at the repo root. The dashboard reads that file (same-origin, no CORS) to draw
the "implied probability over time" chart.

Usage:
    pip install requests
    python fetch_polymarket.py
"""

import json, datetime, sys
import requests

SLUG = "argentina-presidential-election-winner"
GAMMA = "https://gamma-api.polymarket.com/events"
CLOB = "https://clob.polymarket.com/prices-history"
TOP_N = 4                       # candidates to track
OUT = "polymarket_history.json"
HEADERS = {"User-Agent": "arg2027-monitor/1.0 (+github-pages)"}


def get(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=40)
    r.raise_for_status()
    return r.json()


def clean_name(s):
    s = (s or "").strip()
    # "Will Javier Milei win the 2027 ..." -> "Javier Milei"
    if s.lower().startswith("will "):
        s = s[5:]
    for cut in (" win", " to win"):
        i = s.lower().find(cut)
        if i > 0:
            s = s[:i]
    return s.strip()


def main():
    ev = get(GAMMA, {"slug": SLUG})
    ev = ev[0] if isinstance(ev, list) else ev
    markets = ev.get("markets") or []

    cands = []
    for m in markets:
        try:
            price = float(json.loads(m["outcomePrices"])[0])
        except Exception:
            try:
                price = float(m.get("lastTradePrice"))
            except Exception:
                continue
        try:
            token = json.loads(m["clobTokenIds"])[0]
        except Exception:
            token = None
        if not token:
            continue
        cands.append({"name": clean_name(m.get("groupItemTitle") or m.get("question")),
                      "price": price, "token": token})

    cands.sort(key=lambda c: c["price"], reverse=True)
    cands = cands[:TOP_N]
    if not cands:
        print("No candidates found; leaving existing file untouched.", file=sys.stderr)
        sys.exit(1)

    out = {"updated": datetime.datetime.utcnow().isoformat() + "Z",
           "slug": SLUG, "candidates": []}

    for c in cands:
        try:
            h = get(CLOB, {"market": c["token"], "interval": "max", "fidelity": 1440})
            hist = h.get("history") or []
        except Exception as e:
            print(f"  ! history failed for {c['name']}: {e}", file=sys.stderr)
            hist = []
        # collapse to one point per day (last value of the day)
        by_day = {}
        for d in hist:
            try:
                day = datetime.datetime.utcfromtimestamp(int(d["t"])).strftime("%Y-%m-%d")
                by_day[day] = round(float(d["p"]) * 100, 1)
            except Exception:
                continue
        points = [[day, by_day[day]] for day in sorted(by_day)]
        out["candidates"].append({"name": c["name"], "points": points})
        print(f"  {c['name']}: {len(points)} daily points "
              f"(latest {points[-1][1] if points else 'n/a'}%)")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {OUT} with {len(out['candidates'])} candidates")


if __name__ == "__main__":
    main()
