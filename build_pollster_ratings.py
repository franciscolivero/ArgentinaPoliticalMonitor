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
VOLUME = HERE / "pollster_volume.csv"      # every poll in the Wikipedia tables, for the scorecard's volume
OUT = HERE / "pollster_ratings.json"
PAGE = HERE / "pollsters.html"          # gets an embedded copy as an offline fallback

# ---- tunable constants (documented above) ---------------------------------------------------
WINDOW_DAYS = 28          # a poll counts if its fieldwork ended within this many days of election day
MIN_POLLS_PER_RACE = 3    # contests with fewer usable polls are not scored
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


# ---------------------------------------------------------------- scorecard (PollsterGraph-style)
# A second, independent evaluation of each firm, following the PollsterGraph method: four
# indicators that answer different questions.
#   Desempeño (performance, 0-100): how close the firm came in the contests we can score.
#     Per contest, EMA = mean absolute error over the K major candidates (shares of the K).
#     Objective part  = 100 * exp(-EMA / (SG_EMA_SCALE * difficulty)), difficulty = how hard the
#                       contest was (its median EMA over the median of all contests, floor 1).
#     Comparative part = 100 * Phi((mean EMA of the other firms - EMA) / spread of the contest).
#     Contest score = average of the two (objective only when there are no peers).
#     Contests are weighted by type (SG_TYPE_W) and by age (half-life SG_HALF_LIFE years).
#   Desempeño ajustado = n/(n+8) * desempeño + 8/(n+8) * 50 (Bayesian shrinkage to a neutral 50).
#   Nota (letter): z-score of the adjusted performance within the cohort of firms with enough
#     history (>= SG_MIN_POLLS polls in the last 10 years and >= SG_MIN_CONTESTS scored contests);
#     A+ from z >= 1.65, A from 1.33 ... F below -1.65. Firms below the minimum get no letter.
#   Influencia (0-100): reach of the firm's polling record, national polls x2, provincial x1.5,
#     municipal x1, times log-population weight and age decay, with diminishing returns.
#   Puntuación = Desempeño * Influencia / 100. Order: letter first, then puntuación.
SG_EMA_SCALE = 4.0
SG_HALF_LIFE = 8.0
SG_SHRINK = 8
SG_MIN_POLLS, SG_MIN_CONTESTS = 10, 5
SG_TYPE_W = {"general": 1.0, "ballotage": 1.0, "paso": 0.8, "provincial": 0.6}
SG_SCOPE = {"national": 2.0, "provincial": 1.5, "municipal": 1.0}
SG_POP = {"national": 46.0e6, "PBA": 17.5e6, "CABA": 3.1e6}
SG_R0 = 25.0
SG_BANDS = [(1.65, "A+"), (1.33, "A"), (1.0, "A-"), (0.67, "B+"), (0.33, "B"), (0.0, "B-"),
            (-0.33, "C+"), (-0.67, "C"), (-1.0, "C-"), (-1.65, "D"), (-math.inf, "F")]


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _age_w(date, today):
    years = (today - datetime.date.fromisoformat(str(date)[:10])).days / 365.25
    return 0.5 ** (max(years, 0) / SG_HALF_LIFE)


def _band(z):
    for cut, g in SG_BANDS:
        if z >= cut:
            return g
    return "F"


def poll_record(amap, elections):
    """(firm, date, scope, population) for every poll we hold: election-eve polls plus the live files."""
    rec, covered = [], set()
    # full poll tables (every published poll, not only the last one before each vote)
    if VOLUME.exists():
        with open(VOLUME, encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                area, y = r["area"], int(r["year"])
                covered.add((y, area))
                # the table rows carry free-text dates; mid-year is close enough for the age decay,
                # and the row number keeps two polls by the same firm in the same year apart
                rec.append((canon(r["firm"], amap), f"{y}-07-01#{i}", "national" if area == "national" else "provincial",
                            SG_POP.get(area, SG_POP["national"])))
    # election-eve polls for the contests the full tables do not cover (2015 and anything added later)
    with open(POLLS, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            eid = r["election_id"]
            area = "PBA" if "pba" in eid else ("CABA" if "caba" in eid else "national")
            if (int(eid[:4]), area) in covered:
                continue
            rec.append((canon(r["firm"], amap), r["end_date"], "national" if area == "national" else "provincial",
                        SG_POP[area]))
    def add(name, date):
        if name and date:
            rec.append((canon(name, amap), str(date)[:10], "national", SG_POP["national"]))
    for fn, getter in (("encuestas_history.json", lambda d: d.get("polls", [])),
                       ("espacio_history.json", lambda d: [p for s in d.get("spaces", {}).values() for p in s.get("polls", [])]),
                       ("data.json", lambda d: d.get("headToHead2026", {}).get("polls", []))):
        try:
            for p in getter(json.load(open(HERE / fn, encoding="utf-8"))):
                add(p.get("firm"), p.get("date"))
        except Exception:
            pass
    for fn, fk, dk in (("approval_polls.csv", "firm", "end_date"), ("runoff_matchups.csv", "firm", "end_date")):
        try:
            with open(HERE / fn, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    add(r.get(fk), r.get(dk) or r.get("date"))
        except Exception:
            pass
    # the same poll can sit in several files (one release, several questions): count it once
    return sorted(set(rec))


def scorecard(races, elections, amap):
    today = datetime.date.today()
    med_all = st.median(st.median(p["mae_shares"] for p in r["polls"]) for r in races)
    per_firm = {}
    for r in races:
        emas = [p["mae_shares"] for p in r["polls"]]
        diff = max(1.0, st.median(emas) / med_all)
        spread = max(st.pstdev(emas) if len(emas) > 1 else 0.0, 1.0)
        w = SG_TYPE_W.get(r["kind"], 0.6) * _age_w(r["date"], today)
        for p in r["polls"]:
            obj = 100 * math.exp(-p["mae_shares"] / (SG_EMA_SCALE * diff))
            peers = [q["mae_shares"] for q in r["polls"] if q is not p]
            comp = 100 * _phi((st.mean(peers) - p["mae_shares"]) / spread) if peers else None
            sc = obj if comp is None else (obj + comp) / 2
            per_firm.setdefault(p["firm"], []).append({"race": r["id"], "label": r["label"], "kind": r["kind"],
                "ema": round(p["mae_shares"], 2), "objective": round(obj, 1),
                "comparative": None if comp is None else round(comp, 1), "score": round(sc, 1),
                "weight": round(w, 3)})
    rec = poll_record(amap, elections)
    ten = (today - datetime.timedelta(days=3653)).isoformat()
    out = []
    for f, cs in per_firm.items():
        wsum = sum(c["weight"] for c in cs)
        perf = sum(c["score"] * c["weight"] for c in cs) / wsum
        n = len(cs)
        adj = n / (n + SG_SHRINK) * perf + SG_SHRINK / (n + SG_SHRINK) * 50
        mine = [x for x in rec if x[0] == f]
        reach = sum(SG_SCOPE[s] * math.log10(pop) / math.log10(SG_POP["national"]) * _age_w(d, today)
                    for _, d, s, pop in mine if d)
        infl = 100 * (1 - math.exp(-reach / SG_R0))
        polls10 = sum(1 for x in mine if x[1][:10] >= ten)
        out.append({"firm": f, "performance": round(perf, 1), "adjusted": round(adj, 1), "n_contests": n,
                    "polls_10y": polls10, "influence": round(infl, 1), "points": round(perf * infl / 100, 1),
                    "eligible": polls10 >= SG_MIN_POLLS and n >= SG_MIN_CONTESTS,
                    "contests": sorted(cs, key=lambda c: c["race"])})
    cohort = [x["adjusted"] for x in out if x["eligible"]]
    mu = st.mean(cohort) if cohort else None
    sd = st.pstdev(cohort) if len(cohort) > 1 else None
    for x in out:
        if x["eligible"] and sd:
            x["z"] = round((x["adjusted"] - mu) / sd, 2)
            x["grade"] = _band(x["z"])
        else:
            x["z"], x["grade"] = None, None
    order = {g: i for i, (_, g) in enumerate(SG_BANDS)}
    out.sort(key=lambda x: (x["grade"] is None, order.get(x["grade"], 99), -x["points"]))
    for i, x in enumerate(out, 1):
        x["rank"] = i
    meta = {"cohort_size": len(cohort), "cohort_mean": round(mu, 2) if mu else None,
            "cohort_sd": round(sd, 2) if sd else None, "min_polls_10y": SG_MIN_POLLS,
            "min_contests": SG_MIN_CONTESTS, "shrink_n": SG_SHRINK, "half_life_years": SG_HALF_LIFE,
            "type_weights": SG_TYPE_W, "bands": [[c if c != -math.inf else None, g] for c, g in SG_BANDS],
            "median_contest_ema": round(med_all, 2), "ema_scale": SG_EMA_SCALE, "reach_r0": SG_R0}
    return out, meta


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
    card, card_meta = scorecard(races, elections, amap)

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
        "scorecard": card,
        "scorecard_method": card_meta,
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
        print(f"\nscorecard (cohort {card_meta['cohort_size']}, mean {card_meta['cohort_mean']}, sd {card_meta['cohort_sd']}):")
        for x in card:
            print(f"  {x['rank']:2d} {x['firm'][:34]:34s} {str(x['grade'] or '—'):3s} perf {x['performance']:5.1f} "
                  f"adj {x['adjusted']:5.1f} z {x['z'] if x['z'] is not None else '':>5} n {x['n_contests']} "
                  f"polls10 {x['polls_10y']:3d} infl {x['influence']:5.1f} pts {x['points']:5.1f}")


if __name__ == "__main__":
    main()
