#!/usr/bin/env python3
"""
build_pollster_ratings.py — credit-style ratings for Argentine polling firms.

Inputs (all hand-curated, edit in Excel if you like)
  pollster_elections.csv  one row per contest: date, kind, the K major candidates and their result
  pollster_polls.csv      one row per election-eve poll: firm, fieldwork end date, candidate shares
  pollster_aliases.csv    firm-name variants -> canonical name
  live 2026 files         encuestas_history.json, espacio_history.json, data.json, approval_polls.csv
                          (only read to flag which rated firms are still publishing)

Output
  pollster_ratings.json   read by pollsters.html and by build_approval.py (POLLSTER_RATING)
                          (also re-embedded into pollsters.html between RATINGS_START / RATINGS_END)

Method, step by step
--------------------
1. Which polls count.  For each contest, the LAST poll each firm published whose fieldwork ended
   within WINDOW_DAYS of election day. Argentina's publication ban (veda) starts 8 days before the
   vote, so in practice this is the final two to three weeks of public polling.

2. Comparable shares.  Poll and result are both re-expressed as shares of the K major candidates
   listed for that contest (K = 3 for PASO/general, 2 for ballotage). This strips out undecided,
   blank and minor-candidate columns, which firms report in incompatible ways.

3. The error that matters.  Error on the MARGIN between the two candidates who actually finished
   first and second:  e = (poll margin) - (actual margin). Negative e = the firm underestimated
   the winner's lead. |e| is the accuracy measure.
   Also stored, not used for the grade: the signed error on the Peronist candidate's share, and
   the same thing relative to the other firms in the contest ("house_lean"): the firm's own lean
   toward (+) or against (-) the Peronist candidate once the industry-wide miss is netted out.

4. Relative error ("spread over benchmark").  plus_minus = |e| - mean |e| of the OTHER firms in the
   same contest. This is the key step: in the 2019 PASO every firm missed by 10+ points, so raw
   error would fail them all equally; relative error rewards the ones that missed by less.
   Negative plus_minus = better than peers.

5. Short histories are shrunk (empirical Bayes).  A firm's score is its average plus_minus pulled
   toward 0 (the market average):  score = n / (n + k) * mean_plus_minus.  k = sigma^2 / tau^2 is
   estimated from the data (within-firm noise vs genuine between-firm differences), so one lucky
   contest cannot buy an AAA. Firms with a single contest are marked "provisional".

6. Grade, outlook, weight.
   grade    : letter scale on the shrunk score (points of margin better/worse than peers)
   outlook  : direction the latest contest moved the score (Positive / Stable / Negative),
              i.e. score with the latest contest minus score without it, beyond +/- OUTLOOK_PTS
   weight   : (market error / firm's expected error)^2, rescaled so rated firms average 1.0 and
              clipped to [W_MIN, W_MAX]. Unrated firms keep 1.0 in the models that use this.

7. Systemic events ("eventos de crédito").  A contest is flagged when the industry-average SIGNED
   margin error is at least SYSTEMIC_PTS in absolute value — everyone missed in the same direction.

Known limits, stated plainly
  - Dates marked date_approx=1 are publication dates, not fieldwork dates.
  - PASO polls are scored against the PASO, where turnout and coalition-vs-candidate framing make
    polling harder; relative error (step 4) is what keeps this fair across contest types.
  - No time-to-election adjustment inside the window; the window is short and step 4 compares firms
    polling the same contest.
  - Ratings measure vote-intention accuracy. Applying them to approval polls (build_approval.py) is
    a proxy, the same one Silver Bulletin uses.

Usage
    python build_pollster_ratings.py             # writes pollster_ratings.json, re-embeds pollsters.html
    python build_pollster_ratings.py --print     # also prints the ratings table
    python build_pollster_ratings.py --no-embed  # JSON only
"""

import csv, json, sys, math, datetime, pathlib, statistics as st

HERE = pathlib.Path(__file__).parent
ELECTIONS = HERE / "pollster_elections.csv"
POLLS = HERE / "pollster_polls.csv"
ALIASES = HERE / "pollster_aliases.csv"
OUT = HERE / "pollster_ratings.json"
PAGE = HERE / "pollsters.html"          # gets an embedded copy as an offline fallback

# ---- tunable constants (documented above) ---------------------------------------------------
WINDOW_DAYS = 28          # a poll counts if its fieldwork ended within this many days of election day
MIN_POLLS_PER_RACE = 4    # contests with fewer usable polls are not scored
SYSTEMIC_PTS = 5.0        # |industry-average signed margin error| that flags a systemic event
K_MIN, K_MAX = 1.0, 8.0   # bounds on the empirical-Bayes shrinkage constant
W_MIN, W_MAX = 0.5, 1.6   # bounds on model weights
OUTLOOK_PTS = 0.5         # how far the latest contest must move the score to change the outlook
# Grade cut-offs on the shrunk score (points of margin error relative to peers; lower is better).
# BBB is notched (+ / flat / -) as in S&P scales, because most of the market sits there.
# BBB- and above = "investment grade"; BB and below = "speculative".
GRADES = [(-3.0, "AAA"), (-2.0, "AA"), (-1.0, "A"), (-0.33, "BBB+"), (0.33, "BBB"), (1.0, "BBB-"),
          (2.0, "BB"), (3.5, "B"), (math.inf, "CCC")]
INVESTMENT_GRADE = {"AAA", "AA", "A", "BBB+", "BBB", "BBB-"}


# ---------------------------------------------------------------- loading
def num(s):
    s = (s or "").strip()
    return float(s.replace(",", ".")) if s else None


def load_aliases():
    amap = {}
    if ALIASES.exists():
        with open(ALIASES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                amap[r["alias"].strip().lower()] = r["canonical"].strip()
    return amap


def canon(name, amap):
    n = (name or "").strip()
    return amap.get(n.lower(), n)


def load_elections():
    out = {}
    with open(ELECTIONS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cands, res = [], []
            for i in (1, 2, 3):
                c, v = r.get(f"c{i}", "").strip(), num(r.get(f"c{i}_result"))
                if c and v is not None:
                    cands.append(c)
                    res.append(v)
            out[r["election_id"]] = {
                "id": r["election_id"], "date": r["date"], "label": r["label"], "kind": r["kind"],
                "cands": cands, "result": res,
                "peronist": int(r["peronist"].strip().lstrip("c")) - 1 if r.get("peronist") else None,
            }
    return out


def load_polls(elections, amap):
    rows = []
    with open(POLLS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            e = elections.get(r["election_id"])
            if not e:
                print(f"  ! unknown election_id {r['election_id']!r}; row skipped", file=sys.stderr)
                continue
            vals = [num(r.get(f"c{i+1}")) for i in range(len(e["cands"]))]
            if any(v is None for v in vals):
                continue                                   # need every major candidate
            rows.append({"election": e["id"], "firm_raw": r["firm"].strip(),
                         "firm": canon(r["firm"], amap), "end": r["end_date"].strip(),
                         "approx": r.get("date_approx", "0").strip() == "1",
                         "n": int(num(r.get("n")) or 0) or None, "vals": vals})
    return rows


# ---------------------------------------------------------------- scoring
def shares(vals):
    s = sum(vals)
    return [100.0 * v / s for v in vals]


def score_races(elections, polls):
    races = []
    for e in sorted(elections.values(), key=lambda x: x["date"]):
        eday = datetime.date.fromisoformat(e["date"])
        act = shares(e["result"])
        order = sorted(range(len(act)), key=lambda i: -act[i])
        w, s = order[0], order[1]
        act_margin = act[w] - act[s]

        # last poll per firm inside the window
        last = {}
        for p in polls:
            if p["election"] != e["id"]:
                continue
            d = datetime.date.fromisoformat(p["end"])
            days = (eday - d).days
            if days < 0 or days > WINDOW_DAYS:
                continue
            if p["firm"] not in last or p["end"] > last[p["firm"]]["end"]:
                last[p["firm"]] = dict(p, days=days)
        sel = list(last.values())
        if len(sel) < MIN_POLLS_PER_RACE:
            continue

        for p in sel:
            sh = shares(p["vals"])
            p["margin"] = sh[w] - sh[s]
            p["err"] = p["margin"] - act_margin
            p["abs_err"] = abs(p["err"])
            p["mae_shares"] = sum(abs(a - b) for a, b in zip(sh, act)) / len(act)
            p["peronist_lean"] = (sh[e["peronist"]] - act[e["peronist"]]) if e["peronist"] is not None else None
        for p in sel:
            others = [q["abs_err"] for q in sel if q is not p]
            p["plus_minus"] = p["abs_err"] - sum(others) / len(others)
            if p["peronist_lean"] is not None:
                ol = [q["peronist_lean"] for q in sel if q is not p]
                p["house_lean"] = p["peronist_lean"] - sum(ol) / len(ol)
            else:
                p["house_lean"] = None

        mean_signed = st.mean(p["err"] for p in sel)
        races.append({
            "id": e["id"], "label": e["label"], "date": e["date"], "kind": e["kind"],
            "winner": e["cands"][w], "runner_up": e["cands"][s],
            "actual_margin": round(act_margin, 2),
            "n_polls": len(sel),
            "mean_signed_err": round(mean_signed, 2),
            "mean_abs_err": round(st.mean(p["abs_err"] for p in sel), 2),
            "median_abs_err": round(st.median(p["abs_err"] for p in sel), 2),
            "systemic": abs(mean_signed) >= SYSTEMIC_PTS,
            "polls": sorted(({
                "firm": p["firm"], "firm_as_published": p["firm_raw"], "end_date": p["end"],
                "date_approx": p["approx"], "days_before": p["days"],
                "poll_margin": round(p["margin"], 2), "err": round(p["err"], 2),
                "abs_err": round(p["abs_err"], 2), "plus_minus": round(p["plus_minus"], 2),
                "mae_shares": round(p["mae_shares"], 2),
                "peronist_lean": None if p["peronist_lean"] is None else round(p["peronist_lean"], 2),
                "house_lean": None if p["house_lean"] is None else round(p["house_lean"], 2),
            } for p in sel), key=lambda x: x["abs_err"]),
        })
    return races


def shrinkage_k(by_firm):
    """k = sigma^2 / tau^2 by method of moments, from firms with 2+ contests."""
    multi = {f: v for f, v in by_firm.items() if len(v) >= 2}
    if len(multi) < 3:
        return 3.0
    sigma2 = st.mean(st.variance(v) for v in multi.values())          # within-firm noise
    means = [st.mean(v) for v in multi.values()]
    avg_inv_n = st.mean(1 / len(v) for v in multi.values())
    tau2 = max(st.variance(means) - sigma2 * avg_inv_n, 1e-6)         # genuine between-firm spread
    return min(max(sigma2 / tau2, K_MIN), K_MAX), sigma2, tau2


def grade_for(score):
    for cut, g in GRADES:
        if score <= cut:
            return g
    return GRADES[-1][1]


def rate_firms(races):
    by_firm, hist = {}, {}
    for r in races:
        for p in r["polls"]:
            by_firm.setdefault(p["firm"], []).append(p["plus_minus"])
            hist.setdefault(p["firm"], []).append(dict(p, race=r["id"], race_label=r["label"], date=r["date"]))
    k_info = shrinkage_k(by_firm)
    k = k_info[0] if isinstance(k_info, tuple) else k_info

    market_err = st.median(p["abs_err"] for r in races for p in r["polls"])
    firms = []
    for f, pms in by_firm.items():
        h = sorted(hist[f], key=lambda x: x["date"])
        n = len(pms)
        raw = st.mean(pms)
        score = n / (n + k) * raw
        expected = max(market_err + score, 0.25 * market_err)
        outlook = None
        if n >= 2:
            prev = [x["plus_minus"] for x in h[:-1]]
            score_prev = len(prev) / (len(prev) + k) * st.mean(prev)
            d = score - score_prev
            outlook = "Positive" if d <= -OUTLOOK_PTS else ("Negative" if d >= OUTLOOK_PTS else "Stable")
        leans = [x["house_lean"] for x in h if x["house_lean"] is not None]
        firms.append({
            "firm": f, "n_races": n, "provisional": n < 2,
            "score": round(score, 2), "raw_plus_minus": round(raw, 2),
            "grade": grade_for(score), "investment_grade": grade_for(score) in INVESTMENT_GRADE,
            "outlook": outlook,
            "mean_abs_err": round(st.mean(x["abs_err"] for x in h), 2),
            "house_lean": round(st.mean(leans), 2) if leans else None,
            "expected_abs_err": round(expected, 2),
            "_w": (market_err / expected) ** 2,
            "first_race": h[0]["race"], "last_race": h[-1]["race"],
            "history": [{"race": x["race"], "label": x["race_label"], "err": x["err"],
                         "abs_err": x["abs_err"], "plus_minus": x["plus_minus"],
                         "house_lean": x["house_lean"]} for x in h],
        })
    # weights: rated firms average 1.0 (so unrated firms at the default 1.0 sit at the average)
    mean_w = st.mean(x["_w"] for x in firms)
    for x in firms:
        x["weight"] = round(min(max(x.pop("_w") / mean_w, W_MIN), W_MAX), 3)
    firms.sort(key=lambda x: (x["score"], -x["n_races"]))
    return firms, {"k": round(k, 2), "market_median_abs_err": round(market_err, 2),
                   "sigma2": round(k_info[1], 2) if isinstance(k_info, tuple) else None,
                   "tau2": round(k_info[2], 2) if isinstance(k_info, tuple) else None}


# ---------------------------------------------------------------- live activity (2026 files)
def active_firms(amap):
    seen = {}
    def add(name, date):
        c = canon(name, amap)
        if c and (c not in seen or (date or "") > seen[c]):
            seen[c] = date or ""
    try:
        for p in json.load(open(HERE / "encuestas_history.json", encoding="utf-8"))["polls"]:
            add(p.get("firm"), p.get("date"))
    except Exception:
        pass
    try:
        for s in json.load(open(HERE / "espacio_history.json", encoding="utf-8"))["spaces"].values():
            for p in s.get("polls", []):
                add(p.get("firm"), p.get("date"))
    except Exception:
        pass
    try:
        for p in json.load(open(HERE / "data.json", encoding="utf-8")).get("headToHead2026", {}).get("polls", []):
            add(p.get("firm"), p.get("date"))
    except Exception:
        pass
    try:
        with open(HERE / "approval_polls.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                add(r.get("firm"), r.get("end_date"))
    except Exception:
        pass
    return seen


# ---------------------------------------------------------------- main
def main():
    amap = load_aliases()
    elections = load_elections()
    polls = load_polls(elections, amap)
    races = score_races(elections, polls)
    if not races:
        sys.exit("No contest had enough polls to score; check the CSVs.")
    firms, params = rate_firms(races)

    live = active_firms(amap)
    rated = {f["firm"] for f in firms}
    for f in firms:
        f["active_2026"] = f["firm"] in live
        f["last_seen_2026"] = live.get(f["firm"]) or None
    unrated_active = sorted(x for x in live if x not in rated)

    data = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "method": {
            "window_days": WINDOW_DAYS, "min_polls_per_race": MIN_POLLS_PER_RACE,
            "systemic_pts": SYSTEMIC_PTS, "grades": [[c if c != math.inf else None, g] for c, g in GRADES],
            "weight_bounds": [W_MIN, W_MAX], "outlook_pts": OUTLOOK_PTS, **params,
            "metric": "error on the margin between the actual top two, shares of the K major candidates",
        },
        "races": races,
        "firms": firms,
        "weights": {f["firm"]: f["weight"] for f in firms},
        "aliases": amap,
        "unrated_active_2026": unrated_active,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if "--no-embed" not in sys.argv and PAGE.exists():
        html = PAGE.read_text(encoding="utf-8")
        a, b = "/* RATINGS_START */", "/* RATINGS_END */"
        i, j = html.find(a), html.find(b)
        if i != -1 and j > i:
            blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
            PAGE.write_text(html[:i + len(a)] + "\n" + blob + "\n" + html[j:], encoding="utf-8")

    print(f"wrote {OUT.name}: {len(firms)} firms rated over {len(races)} contests "
          f"({sum(r['n_polls'] for r in races)} polls); k={params['k']}, "
          f"market median |error|={params['market_median_abs_err']} pts")
    if "--print" in sys.argv:
        print(f"\n{'firm':42s} {'grade':6s}{'score':>6s} {'n':>2s} {'|err|':>6s} {'lean':>6s} {'w':>5s}  outlook   2026")
        for f in firms:
            print(f"{f['firm'][:42]:42s} {f['grade'] + ('*' if f['provisional'] else ''):6s}"
                  f"{f['score']:6.2f} {f['n_races']:2d} {f['mean_abs_err']:6.2f} "
                  f"{(f['house_lean'] if f['house_lean'] is not None else float('nan')):6.2f} "
                  f"{f['weight']:5.2f}  {str(f['outlook'] or '—'):9s} {'sí' if f['active_2026'] else ''}")
        print("\ncontests:")
        for r in races:
            print(f"  {r['label']:16s} n={r['n_polls']:2d}  margin real {r['actual_margin']:6.2f}  "
                  f"error medio {r['mean_signed_err']:6.2f}  |error| medio {r['mean_abs_err']:5.2f}"
                  f"{'  <- evento sistémico' if r['systemic'] else ''}")
        print("\n* = provisional (one contest)")


if __name__ == "__main__":
    main()
