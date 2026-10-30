#!/usr/bin/env python3
"""
forecast_sim.py — the Simulation model: chance that Javier Milei wins the 2027 presidential
election, from 40,000 simulated elections a day that play out both rounds. Writes forecast_sim.json.

Argentina's rules are what make simulation worth doing (they are our Electoral College): a candidate
wins outright in the first round with 45% of the valid vote, or with 40% and a 10-point lead;
otherwise the top two go to a runoff. So the model has to know the first round, the runoff, and how
the two are linked.

1. Poll averages, adjusted for house effects
   First round: La Libertad Avanza (LLA) and Peronismo as shares of the decided vote, from every
   poll in espacio_history.json. Runoff: Milei's two-way share against Kicillof
   (encuestas_history.json). For each day the average uses only polls published by then:
     * each firm's house effect (how far its polls sit from everyone else's on average) is
       estimated, shrunk toward zero for firms with few polls, and subtracted;
     * polls are weighted by the firm's rating (pollster_ratings.json, 0.5–1.6) and by recency
       (Gaussian kernel, 21-day bandwidth).

2. Fundamentals: the incumbent's vote from confidence in government
   Across the eleven national elections of 2005–2025 the governing party's share of the vote tracks
   the Di Tella ICG in the election month (the regression on the main page). Today's ICG gives an
   expected LLA first-round share. Its uncertainty adds the regression's leave-one-out error to how
   much the ICG itself can still move before October 2027 (0.17 points × √months, from the ICG
   series since 2002). Polls and fundamentals are combined by precision: each gets weight in
   proportion to 1 / its variance, so the polls gain weight as the election approaches.

3. Simulation
   Each of the 40,000 simulations draws:
     * a first-round error on the LLA–Peronismo margin, fat-tailed (Student t, 5 degrees of
       freedom): SD 11 points on election day — the root-mean-square error of the final-month poll
       average on the margin between the top two in the 2015, 2019 and 2023 first rounds — plus
       how far opinion can still move (3 margin points × √(months left));
     * small independent errors for each bloc's share;
     * a runoff error, also fat-tailed, SD 5.2 points of two-way share on election day plus
       2 points × √(months left), correlated 0.6 with the first-round error (a polling miss in
       October tends to carry over to November).
   Then it applies the rules: outright win at 45%, or 40% with a 10-point lead; if not, a runoff
   between the top two. Milei's chance is the share of simulations he wins.

   The runoff rival is not settled. Each simulation picks the Peronist nominee: Kicillof with
   probability 0.6, otherwise another Peronist (Massa is the one pollsters test). How Milei would do
   against that other nominee comes from polls that asked both matchups of the same sample
   (runoff_matchups.csv, updated daily from the press by fetch_runoff_matchups.py): the average difference, shrunk toward zero, plus an uncertainty equal to
   how much that difference varies across polls (at least 2 points).

Also reported: the chance he wins in the first round, the chance of a runoff, and the same chance
if LLA and the PRO run on one ticket (PRO's share added to LLA's in the first round; the runoff is
unchanged, since PRO voters already back Milei there).

Simplifications, said plainly: blocs other than LLA and Peronismo are treated as never reaching the
runoff; the candidates are those the polls ask about today; and the error sizes come from only three
presidential cycles, so they are a calibrated judgment, not a law.
"""

import json, math, datetime, pathlib, collections
import numpy as np

HERE = pathlib.Path(__file__).parent
FIRST_ROUND = datetime.date(2027, 10, 24)
N_SIMS = 40000
T_DF = 5
MARGIN_SD_FINAL = 11.0      # first-round margin error of the final-month average, 2015–2023
MARGIN_DRIFT_30D = 3.0      # margin points per √month
RUNOFF_SD_FINAL = 5.2       # two-way share error of the final-month runoff average, 2015–2023
RUNOFF_DRIFT_30D = 2.0      # two-way share points per √month
RHO = 0.6                   # correlation between first-round and runoff errors
BLOC_SD = 1.5               # independent error on each bloc's share
ICG_DRIFT_MONTH = 0.172     # ICG points per √month (RMS change, 2002–2026)
BANDWIDTH = 21              # days, recency kernel
HOUSE_SHRINK = 2            # pseudo-polls pulling each house effect toward zero
UNRATED_WEIGHT = 0.8
P_KICILLOF = 0.6            # chance the Peronist nominee is Kicillof (judgment; edit as the race clarifies)
ALT_PERONISTS = {"Massa"}   # other Peronist nominees with runoff polls (Cristina Kirchner is barred from office)
NOMINEE_SD_MIN = 2.0        # floor on the uncertainty about how another nominee would do

# ICG in the election month and the governing party's national vote (same table as the main page)
ELECTIONS = [(2005, 2.192, 39.46), (2007, 1.678, 45.29), (2009, 1.340, 29.89), (2011, 2.621, 54.11),
             (2013, 1.976, 33.05), (2015, 1.909, 37.08), (2017, 2.828, 41.75), (2019, 2.009, 40.28),
             (2021, 1.454, 34.17), (2023, 1.223, 36.78), (2025, 2.101, 40.66)]


def load(name):
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def tdraw(rng, sd, size):
    """Student-t draws scaled to standard deviation sd."""
    return rng.standard_t(T_DF, size) * sd * math.sqrt((T_DF - 2) / T_DF)


# ---------- poll averages with house effects ----------

def firm_weight(firm, ratings):
    if not ratings:
        return 1.0
    canon = ratings.get("aliases", {}).get(firm.lower().strip(), firm)
    return ratings.get("weights", {}).get(canon, UNRATED_WEIGHT)


def average(polls, day, ratings):
    """polls: list of (date, firm, value). House-effect-adjusted, rating- and recency-weighted
    average of the polls published up to `day`. Returns (value, house effects) or (None, {})."""
    ps = [p for p in polls if p[0] <= day]
    if len(ps) < 3:
        return None, {}
    age = np.array([(day - p[0]).days for p in ps], float)
    w = np.exp(-0.5 * (age / BANDWIDTH) ** 2) * np.array([firm_weight(p[1], ratings) for p in ps])
    v = np.array([p[2] for p in ps], float)
    # house effects: residual of each poll from a local average of all polls around its date
    dates = np.array([p[0].toordinal() for p in ps], float)
    wr = np.array([firm_weight(p[1], ratings) for p in ps])
    resid = collections.defaultdict(list)
    for i, p in enumerate(ps):
        k = np.exp(-0.5 * ((dates - dates[i]) / BANDWIDTH) ** 2) * wr
        k[i] = 0
        if k.sum() > 0:
            resid[p[1]].append(v[i] - (k * v).sum() / k.sum())
    house = {f: sum(r) / (len(r) + HOUSE_SHRINK) for f, r in resid.items()}
    adj = v - np.array([house.get(p[1], 0.0) for p in ps])
    if w.sum() < 1e-6:          # nothing recent: fall back to the latest polls
        w = np.exp(-0.5 * (age / (4 * BANDWIDTH)) ** 2)
    return float((w * adj).sum() / w.sum()), house


def first_round_polls():
    E = load("espacio_history.json")["spaces"]
    rows = collections.defaultdict(dict)
    for space, v in E.items():
        for p in v["polls"]:
            rows[(p["date"], p["firm"])][space] = p["value"]
    L, P, PRO = [], [], []
    for (d, firm), r in rows.items():
        if "La Libertad Avanza" not in r or "Peronismo" not in r:
            continue
        decided = 100 - r.get("Sin definir", 0)
        if decided < 40:
            continue
        day = datetime.date.fromisoformat(d)
        L.append((day, firm, 100 * r["La Libertad Avanza"] / decided))
        P.append((day, firm, 100 * r["Peronismo"] / decided))
        if "PRO" in r:
            PRO.append((day, firm, 100 * r["PRO"] / decided))
    return L, P, PRO


def runoff_polls():
    H = load("encuestas_history.json")
    out = []
    for p in H["polls"]:
        if p.get("milei") and p.get("kicillof"):
            out.append((datetime.date.fromisoformat(p["date"]), p["firm"],
                        100 * p["milei"] / (p["milei"] + p["kicillof"])))
    return out


def nominee_effect():
    """How Milei's runoff share changes if the Peronist nominee is not Kicillof, from polls that
    tested both matchups with the same sample (runoff_matchups.csv). Returns (mean, sd, n)."""
    import csv
    f = HERE / "runoff_matchups.csv"
    if not f.exists():
        return 0.0, NOMINEE_SD_MIN, 0
    by = collections.defaultdict(dict)
    with open(f, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            m, o = float(r["milei"]), float(r["opponent_share"])
            by[(r["end_date"], r["firm"])][r["opponent"]] = 100 * m / (m + o)
    diffs = [v[c] - v["Kicillof"] for v in by.values() if "Kicillof" in v for c in v if c in ALT_PERONISTS]
    if not diffs:
        return 0.0, NOMINEE_SD_MIN, 0
    n = len(diffs)
    mean = sum(diffs) / n * n / (n + 3)                      # shrunk toward no difference
    sd = max(math.sqrt(sum((d - mean) ** 2 for d in diffs) / n), NOMINEE_SD_MIN)
    return mean, sd, n


# ---------- fundamentals ----------

def fundamentals_fit():
    x = np.array([e[1] for e in ELECTIONS])
    y = np.array([e[2] for e in ELECTIONS])
    b, a = np.polyfit(x, y, 1)
    loo = []
    for i in range(len(x)):
        m = np.arange(len(x)) != i
        bi, ai = np.polyfit(x[m], y[m], 1)
        loo.append(y[i] - (ai + bi * x[i]))
    return float(a), float(b), float(np.sqrt(np.mean(np.square(loo))))


def icg_at(day):
    D = load("data.json")
    pts = [(datetime.date.fromisoformat(r["date"]), r["value"]) for r in D["icg"]["index"]]
    pts = [p for p in pts if p[0] <= day]
    return pts[-1][1] if pts else None


# ---------- simulation ----------

def simulate(L, P, PRO, R, days, rng, nominee=(0.0, NOMINEE_SD_MIN)):
    months = max(days, 0) / 30.4
    sd_m = math.sqrt(MARGIN_SD_FINAL ** 2 + MARGIN_DRIFT_30D ** 2 * months)
    sd_r = math.sqrt(RUNOFF_SD_FINAL ** 2 + RUNOFF_DRIFT_30D ** 2 * months)
    zm = tdraw(rng, 1.0, N_SIMS)                       # unit first-round shock
    zr = RHO * zm + math.sqrt(1 - RHO ** 2) * tdraw(rng, 1.0, N_SIMS)
    m_err = zm * sd_m
    l = L + m_err / 2 + rng.normal(0, BLOC_SD, N_SIMS)
    p = P - m_err / 2 + rng.normal(0, BLOC_SD, N_SIMS)
    pro = PRO + rng.normal(0, BLOC_SD / 2, N_SIMS)
    rest = np.maximum(100 - L - P, 1) + rng.normal(0, BLOC_SD, N_SIMS)
    l, p, pro, rest = (np.maximum(v, 0.1) for v in (l, p, pro, rest))
    kic = rng.random(N_SIMS) < P_KICILLOF                     # who the Peronist nominee is
    runoff = R + zr * sd_r + np.where(kic, 0.0, nominee[0] + rng.normal(0, nominee[1], N_SIMS))

    def play(lla, pj, other):
        tot = lla + pj + other
        lla, pj = 100 * lla / tot, 100 * pj / tot
        lla_out = (lla >= 45) | ((lla >= 40) & (lla - pj >= 10))
        pj_out = (pj >= 45) | ((pj >= 40) & (pj - lla >= 10))
        win = lla_out | (~pj_out & (runoff > 50))
        return win, lla_out, pj_out, lla

    win, l1, p1, lshare = play(l, p, rest)
    win_alliance, *_ = play(l + pro, p, np.maximum(rest - pro, 0.1))
    return {
        "p": float(win.mean()),
        "p_first_round_win": float(l1.mean()),
        "p_lose_first_round": float(p1.mean()),
        "p_runoff": float((~l1 & ~p1).mean()),
        "p_win_runoff_if_held": float((runoff[~l1 & ~p1] > 50).mean()) if (~l1 & ~p1).any() else None,
        "p_win_runoff_vs_kicillof": float((runoff[~l1 & ~p1 & kic] > 50).mean()) if (~l1 & ~p1 & kic).any() else None,
        "p_win_runoff_vs_other": float((runoff[~l1 & ~p1 & ~kic] > 50).mean()) if (~l1 & ~p1 & ~kic).any() else None,
        "p_alliance": float(win_alliance.mean()),
        "sd_margin": sd_m, "sd_runoff": sd_r,
        "_first": lshare, "_runoff": runoff,
    }


def main():
    ratings = load("pollster_ratings.json")
    Lp, Pp, PROp = first_round_polls()
    Rp = runoff_polls()
    a, b, fund_err = fundamentals_fit()
    nom = nominee_effect()

    start = max(min(p[0] for p in Lp), min(p[0] for p in Rp))
    today = datetime.date.today()
    rows, last = [], None
    d = start
    while d <= today:
        L, _ = average(Lp, d, ratings)
        P, _ = average(Pp, d, ratings)
        PRO, _ = average(PROp, d, ratings)
        R, _ = average(Rp, d, ratings)
        if None in (L, P, R):
            d += datetime.timedelta(days=1)
            continue
        PRO = PRO or 0.0
        days = (FIRST_ROUND - d).days
        months = max(days, 0) / 30.4
        # fundamentals for LLA's first-round share, combined with the polls by precision
        icg = icg_at(d)
        L_poll, P_poll, R_poll = L, P, R
        L_fund, w_poll = None, 1.0
        if icg is not None:
            L_fund = a + b * icg
            var_f = fund_err ** 2 + (b * ICG_DRIFT_MONTH) ** 2 * months
            var_p = (MARGIN_SD_FINAL / 2) ** 2 + (MARGIN_DRIFT_30D / 2) ** 2 * months
            w_poll = (1 / var_p) / (1 / var_p + 1 / var_f)
            L = w_poll * L_poll + (1 - w_poll) * L_fund
            P = P_poll * (100 - L) / (100 - L_poll)          # others give up or gain share pro rata
            R = R_poll + 100 * (L / (L + P) - L_poll / (L_poll + P_poll))
        rng = np.random.default_rng(int(d.strftime("%Y%m%d")))
        s = simulate(L, P, PRO, R, days, rng, nom[:2])
        rows.append({"date": d.isoformat(), "p": round(s["p"], 3),
                     "p_first_round_win": round(s["p_first_round_win"], 3),
                     "p_runoff": round(s["p_runoff"], 3), "p_alliance": round(s["p_alliance"], 3),
                     "p_win_runoff_if_held": None if s["p_win_runoff_if_held"] is None else round(s["p_win_runoff_if_held"], 3),
                     "lla_poll": round(L_poll, 1), "pj_poll": round(P_poll, 1), "pro_poll": round(PRO, 1),
                     "runoff_poll": round(R_poll, 1), "icg": icg,
                     "lla_fund": None if L_fund is None else round(L_fund, 1),
                     "lla": round(L, 1), "pj": round(P, 1), "runoff": round(R, 1),
                     "weight_polls": round(w_poll, 2)})
        last = (s, rows[-1])
        d += datetime.timedelta(days=1)

    s, latest = last
    latest = {**latest,
              "p_lose_first_round": round(s["p_lose_first_round"], 3),
              "p_win_runoff_vs_kicillof": None if s["p_win_runoff_vs_kicillof"] is None else round(s["p_win_runoff_vs_kicillof"], 3),
              "p_win_runoff_vs_other": None if s["p_win_runoff_vs_other"] is None else round(s["p_win_runoff_vs_other"], 3),
              "sd_margin": round(s["sd_margin"], 1), "sd_runoff": round(s["sd_runoff"], 1)}
    fb = np.arange(15, 66, 1)
    rb = np.arange(30, 71, 1)
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "Simulation model",
        "election": FIRST_ROUND.isoformat(),
        "n_sims": N_SIMS,
        "latest": latest,
        "series": {k: [r[k] for r in rows] for k in ("date", "p", "p_first_round_win", "p_runoff",
                                                       "p_win_runoff_if_held", "p_alliance")},
        "distribution": {
            "first_round_lla": {"bins": fb[:-1].tolist(),
                                "share_of_sims": (np.histogram(s["_first"], fb)[0] / N_SIMS).round(4).tolist()},
            "runoff_milei": {"bins": rb[:-1].tolist(),
                             "share_of_sims": (np.histogram(s["_runoff"], rb)[0] / N_SIMS).round(4).tolist()},
        },
        "method": {"n_sims": N_SIMS, "t_df": T_DF, "margin_sd_final": MARGIN_SD_FINAL,
                   "margin_drift_30d": MARGIN_DRIFT_30D, "runoff_sd_final": RUNOFF_SD_FINAL,
                   "runoff_drift_30d": RUNOFF_DRIFT_30D, "rho": RHO, "p_kicillof_nominee": P_KICILLOF,
                   "other_nominee_effect": {"mean": round(nom[0], 2), "sd": round(nom[1], 2), "n_pairs": nom[2]},
                   "fundamentals": {"a": round(a, 2), "b": round(b, 2), "loo_rmse": round(fund_err, 2)}},
    }
    (HERE / "forecast_sim.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Simulation model, {N_SIMS:,} sims: Milei {latest['p']:.0%} | first-round win {latest['p_first_round_win']:.0%}, "
          f"runoff {latest['p_runoff']:.0%}, wins runoff if held {latest['p_win_runoff_if_held']:.0%} "
          f"(vs Kicillof {latest['p_win_runoff_vs_kicillof']:.0%}, vs another Peronist {latest['p_win_runoff_vs_other']:.0%}) | "
          f"with PRO ticket {latest['p_alliance']:.0%}")
    print(f"  LLA {latest['lla_poll']} (polls) / {latest['lla_fund']} (ICG) -> {latest['lla']} "
          f"[poll weight {latest['weight_polls']}], PJ {latest['pj']}, runoff {latest['runoff_poll']} -> {latest['runoff']}")


if __name__ == "__main__":
    main()
