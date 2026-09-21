#!/usr/bin/env python3
"""
build_approval.py — presidential approval average for the Argentina 2027 monitor, following the
Silver Bulletin polling-average methodology
(https://www.natesilver.net/p/silver-bulletin-polling-average-methodology).

Input : approval_polls.csv   (one row per poll, curated by hand; see columns below)
Output: approval_history.json (read by index.html) + re-embeds the same JSON into index.html
        between the APPROVAL_START / APPROVAL_END markers as an offline fallback.

What is implemented, step by step, and how it maps to the methodology page
-------------------------------------------------------------------------
1. Which polls.  Only "aprobación de gestión" questions. "Imagen" (favourability) readings are a
   different question and are kept out, as Silver keeps favourability and approval separate.
   Poll = one row per firm/wave, dated on its fieldwork-end date.

2. Poll weights ("influence score"), three factors:
   a) pollster rating  -> POLLSTER_RATING below. There is no public accuracy rating for Argentine
      firms, so every firm starts at 1.0. Edit the dict if you want to encode your own view.
   b) sample size with diminishing returns -> sqrt(min(n, N_CAP) / 1000). Missing n -> 1000.
   c) recency -> handled inside the local regression by a Gaussian kernel in time.
   Anti-flooding: a firm's total sample inside a +/-FLOOD_WINDOW-day window is capped at
   FIRM_N_CAP, and that capped weight is split across its polls in the window.

3. Local polynomial regression (degree 1, i.e. local linear) evaluated daily. The average is a
   50/50 blend of an aggressive and a conservative bandwidth, both chosen EMPIRICALLY: every
   (short, long) pair on the grid is scored on how well the average built only from earlier
   polls predicts each next poll (one-step-ahead, house effects removed), and the best pair wins.

4. House effects, estimated separately for approve and disapprove (which also absorbs firms that
   carry systematically more or fewer undecideds). Iterative: average -> firm residuals ->
   shrink by the firm's aggregate sample (N / (N + HOUSE_K)) -> centre on the rating-weighted
   consensus ("true north") -> adjust polls -> recompute average -> repeat until stable.

5. Error bars: band where 90% of NEW (raw) polls should fall. Width = local residual spread
   (wider when polls disagree) combined with the uncertainty of the average itself (wider when
   there are few polls nearby); the multiplier is calibrated so 90% of the out-of-sample
   one-step-ahead polls actually fall inside it.

Not applicable here (and why): partisan-poll priors (Silver applies them to the generic ballot,
not to approval); LV/RV/adult preference (Argentine approval polls are all adults/electorate);
tracking-poll overlap (no daily trackers in this series).

Usage:
    pip install numpy
    python build_approval.py              # writes approval_history.json and re-embeds index.html
    python build_approval.py --no-embed   # JSON only
"""

import csv, json, math, re, sys, datetime, pathlib
import numpy as np

HERE = pathlib.Path(__file__).parent
CSV_IN = HERE / "approval_polls.csv"
JSON_OUT = HERE / "approval_history.json"
HTML_FILE = HERE / "index.html"

# ---- tunable constants (all documented above) ----------------------------------------------
POLLSTER_RATING = {}          # e.g. {"Poliarquía": 1.1, "Delfos": 0.8}; default 1.0
DEFAULT_N = 1000              # typical Argentine national sample when the ficha isn't published
N_CAP = 2500                  # diminishing returns: samples above this add no extra weight
FIRM_N_CAP = 3000             # anti-flooding cap on a firm's aggregate sample inside the window
FLOOD_WINDOW = 15             # days either side
HOUSE_K = 4000                # shrinkage: share of a house effect removed = N / (N + HOUSE_K)
BW_GRID = [10, 14, 21, 30, 45, 60, 90]   # candidate kernel bandwidths (days)
MIN_TRAIN = 4                 # polls needed before a poll is used to score bandwidths
BAND_Q = 0.90                 # coverage of the error band
MEASURES = ("approve", "disapprove")
SLOPE_RIDGE = 0.25            # penalty on the local slope, relative to s0*h^2


def d2i(s):
    return datetime.date.fromisoformat(s).toordinal()


def i2d(i):
    return datetime.date.fromordinal(int(i)).isoformat()


# ---------------------------------------------------------------- load + weights
def load_polls():
    polls = []
    with open(CSV_IN, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            def num(k):
                v = (r.get(k) or "").strip()
                return float(v) if v else None
            p = {"firm": r["firm"].strip(), "date": r["end_date"].strip(),
                 "approve": num("approve"), "disapprove": num("disapprove"),
                 "n": num("n"), "approx": (r.get("date_approx") or "0").strip() == "1",
                 "source": (r.get("source") or "").strip()}
            p["t"] = d2i(p["date"])
            polls.append(p)
    polls.sort(key=lambda p: (p["t"], p["firm"]))
    return polls


def poll_weights(polls):
    """Rating x diminishing-returns sample weight, with the anti-flooding cap per firm."""
    for p in polls:
        p["n_used"] = p["n"] if p["n"] else DEFAULT_N
    for p in polls:
        same = [q for q in polls if q["firm"] == p["firm"] and abs(q["t"] - p["t"]) <= FLOOD_WINDOW]
        tot = sum(q["n_used"] for q in same)
        n_eff = p["n_used"] * min(1.0, FIRM_N_CAP / tot)
        rating = POLLSTER_RATING.get(p["firm"], 1.0)
        p["w"] = rating * math.sqrt(min(n_eff, N_CAP) / 1000.0)


# ---------------------------------------------------------------- local linear regression
def loclin(t0, ts, ys, ws, h):
    """Local-linear estimate at t0 with Gaussian kernel of bandwidth h (days)."""
    if len(ts) == 0:
        return float("nan")
    k = np.exp(-0.5 * ((ts - t0) / h) ** 2) * ws
    if k.sum() < 1e-9:
        return float("nan")
    x = ts - t0
    s0, s1, s2 = k.sum(), (k * x).sum(), (k * x * x).sum()
    # ridge on the local slope: keeps the line from swinging in data gaps and from
    # extrapolating a trend past the last poll (it shrinks toward the local mean there)
    s2 = s2 + SLOPE_RIDGE * s0 * h * h
    det = s0 * s2 - s1 * s1
    if det <= 1e-9 * s0 * max(s2, 1.0):          # degenerate design -> local constant
        return float((k * ys).sum() / s0)
    return float(((s2 * (k * ys).sum()) - s1 * (k * x * ys).sum()) / det)


def blend(t0, ts, ys, ws, hs):
    return 0.5 * loclin(t0, ts, ys, ws, hs[0]) + 0.5 * loclin(t0, ts, ys, ws, hs[1])


def arrays(polls, m, key=None):
    sel = [p for p in polls if p[m] is not None]
    ts = np.array([p["t"] for p in sel], float)
    ys = np.array([p[key or m] for p in sel], float)
    ws = np.array([p["w"] for p in sel], float)
    return sel, ts, ys, ws


# ---------------------------------------------------------------- house effects
def fit_house_effects(polls, m, hs, iters=30):
    firms = sorted({p["firm"] for p in polls if p[m] is not None})
    he = {f: 0.0 for f in firms}
    for _ in range(iters):
        for p in polls:
            if p[m] is not None:
                p[m + "_adj"] = p[m] - he[p["firm"]]
        sel, ts, ya, ws = arrays(polls, m, m + "_adj")
        fitted = np.array([blend(t, ts, ya, ws, hs) for t in ts])
        new = {}
        for f in firms:
            idx = [i for i, p in enumerate(sel) if p["firm"] == f]
            raw = np.array([sel[i][m] for i in idx]) - fitted[idx]
            w = ws[idx]
            n_tot = sum(sel[i]["n_used"] for i in idx)
            shrink = n_tot / (n_tot + HOUSE_K)
            new[f] = shrink * float((w * raw).sum() / w.sum())
        # centre on the rating-weighted consensus ("true north")
        cw = {f: POLLSTER_RATING.get(f, 1.0) * sum(p["w"] for p in sel if p["firm"] == f) for f in firms}
        centre = sum(new[f] * cw[f] for f in firms) / sum(cw.values())
        new = {f: v - centre for f, v in new.items()}
        delta = max(abs(new[f] - he[f]) for f in firms)
        he = new
        if delta < 0.01:
            break
    for p in polls:
        if p[m] is not None:
            p[m + "_adj"] = p[m] - he[p["firm"]]
    info = {}
    for f in firms:
        fp = [p for p in polls if p["firm"] == f and p[m] is not None]
        n_tot = sum(p["n_used"] for p in fp)
        info[f] = {"effect": round(he[f], 2), "polls": len(fp), "shrink": round(n_tot / (n_tot + HOUSE_K), 2)}
    return he, info


# ---------------------------------------------------------------- bandwidth choice (empirical)
def one_step_errors(polls, m, hs, key):
    """Predict each poll from strictly earlier polls only; return (error, poll) pairs."""
    sel, ts, ys, ws = arrays(polls, m, key)
    out = []
    for j, p in enumerate(sel):
        prior = ts < p["t"]
        if prior.sum() < MIN_TRAIN:
            continue
        pred = blend(p["t"], ts[prior], ys[prior], ws[prior], hs)
        if not math.isnan(pred):
            out.append((ys[j] - pred, p, pred))
    return out


def choose_bandwidths(polls):
    best, best_loss = None, float("inf")
    for i, h1 in enumerate(BW_GRID):
        for h2 in BW_GRID[i:]:
            loss, wsum = 0.0, 0.0
            for m in MEASURES:
                for e, p, _ in one_step_errors(polls, m, (h1, h2), m + "_adj"):
                    loss += p["w"] * e * e
                    wsum += p["w"]
            if wsum and loss / wsum < best_loss:
                best, best_loss = (h1, h2), loss / wsum
    return best, math.sqrt(best_loss)


# ---------------------------------------------------------------- error band
def band_sd(t0, sel, ts, resid, ws, h_band, sigma0):
    k = np.exp(-0.5 * ((ts - t0) / h_band) ** 2) * ws / ws.mean()
    mass = k.sum()
    var = ((k * resid ** 2).sum() + sigma0 ** 2) / (mass + 1.0)   # 1 pseudo-poll prior at sigma0
    n_eff = max(mass, 0.25)
    return math.sqrt(var + var / n_eff)


def build():
    polls = load_polls()
    poll_weights(polls)

    hs = (21, 45)                                   # starting guess
    for _ in range(2):                              # house effects <-> bandwidth, twice
        house = {m: fit_house_effects(polls, m, hs) for m in MEASURES}
        hs, cv_rmse = choose_bandwidths(polls)
    house = {m: fit_house_effects(polls, m, hs) for m in MEASURES}

    today = datetime.date.today().toordinal()
    t_start = min(p["t"] for p in polls)
    grid = np.arange(t_start, max(today, max(p["t"] for p in polls)) + 1)
    h_band = max(hs[1], 30)

    out_avg = {"dates": [i2d(t) for t in grid]}
    calib = {}
    for m in MEASURES:
        sel, ts, ya, ws = arrays(polls, m, m + "_adj")
        yraw = np.array([p[m] for p in sel])
        fitted = np.array([blend(t, ts, ya, ws, hs) for t in ts])
        resid = yraw - fitted                          # raw polls: the band is for new raw polls
        sigma0 = math.sqrt(float((ws * resid ** 2).sum() / ws.sum()))
        # calibrate the multiplier on out-of-sample raw polls
        ratios = []
        for e, p, pred in one_step_errors(polls, m, hs, m):
            # recompute error vs the house-adjusted prior average
            prior = [q for q in sel if q["t"] < p["t"]]
            pt = np.array([q["t"] for q in prior], float)
            py = np.array([q[m + "_adj"] for q in prior], float)
            pw = np.array([q["w"] for q in prior], float)
            pr = np.array([q[m] for q in prior], float) - np.array([blend(t, pt, py, pw, hs) for t in pt])
            sd = band_sd(p["t"], prior, pt, pr, pw, h_band, sigma0)
            pred_adj = blend(p["t"], pt, py, pw, hs)
            ratios.append(abs(p[m] - pred_adj) / sd)
        z = float(np.quantile(ratios, BAND_Q)) if len(ratios) >= 8 else 1.645
        z = min(max(z, 1.3), 2.6)
        calib[m] = {"z90": round(z, 2), "oos_polls": len(ratios)}

        # after the last poll of this series there is no new information: hold the average flat
        t_last = ts.max()
        mid = np.array([blend(min(t, t_last), ts, ya, ws, hs) for t in grid])
        sd = np.array([band_sd(t, sel, ts, resid, ws, h_band, sigma0) for t in grid])
        # ...and before its first poll there is none at all: leave it blank rather than extrapolate
        live = grid >= ts.min()
        rnd = lambda arr: [round(float(v), 2) if ok else None for v, ok in zip(arr, live)]
        out_avg[m] = rnd(mid)
        out_avg[m + "_lo"] = rnd(mid - z * sd)
        out_avg[m + "_hi"] = rnd(mid + z * sd)
    out_avg["net"] = [round(a - d, 2) if (a is not None and d is not None) else None
                      for a, d in zip(out_avg["approve"], out_avg["disapprove"])]

    firms = sorted({p["firm"] for p in polls})
    house_out = {f: {m: house[m][1].get(f) for m in MEASURES} for f in firms}

    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "question": "Aprobación de la gestión de Javier Milei (national polls)",
        "method": {
            "reference": "https://www.natesilver.net/p/silver-bulletin-polling-average-methodology",
            "bandwidths_days": list(hs), "one_step_rmse": round(cv_rmse, 2),
            "band": calib, "house_k": HOUSE_K, "n_cap": N_CAP, "firm_n_cap": FIRM_N_CAP,
            "pollster_rating": POLLSTER_RATING or "all firms 1.0",
        },
        "latest": {m: out_avg[m][-1] for m in MEASURES + ("net",)},
        "last_poll": {m: max(p["date"] for p in polls if p[m] is not None) for m in MEASURES},
        "polls": [{
            "date": p["date"], "firm": p["firm"], "approx_date": p["approx"],
            "approve": p["approve"], "disapprove": p["disapprove"], "n": p["n"],
            "approve_adj": round(p["approve_adj"], 1) if p["approve"] is not None else None,
            "disapprove_adj": round(p["disapprove_adj"], 1) if p["disapprove"] is not None else None,
            "weight": round(p["w"], 2), "source": p["source"],
        } for p in polls],
        "house_effects": house_out,
        "avg": out_avg,
    }
    return data


def embed(data):
    if not HTML_FILE.exists():
        return
    html = HTML_FILE.read_text(encoding="utf-8")
    block = "\n" + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    new, k = re.subn(r"(/\*\s*APPROVAL_START\s*\*/).*?(/\*\s*APPROVAL_END\s*\*/)",
                     lambda m: m.group(1) + block + m.group(2), html, count=1, flags=re.S)
    if k:
        HTML_FILE.write_text(new, encoding="utf-8")
        print(f"  re-embedded approval data into {HTML_FILE.name}")


def main():
    data = build()
    JSON_OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    L, M = data["latest"], data["method"]
    print(f"wrote {JSON_OUT.name}: {len(data['polls'])} polls, bandwidths {M['bandwidths_days']} d, "
          f"one-step RMSE {M['one_step_rmse']} pts")
    print(f"  latest: approve {L['approve']:.1f} · disapprove {L['disapprove']:.1f} · net {L['net']:+.1f}")
    for f, h in sorted(data["house_effects"].items(), key=lambda kv: -(kv[1]["approve"] or {}).get("effect", 0)):
        a, d = h["approve"], h["disapprove"]
        print(f"  {f:<24} approve {a['effect']:+5.1f} ({a['polls']})"
              + (f"   disapprove {d['effect']:+5.1f} ({d['polls']})" if d else ""))
    if "--no-embed" not in sys.argv:
        embed(data)


if __name__ == "__main__":
    main()
