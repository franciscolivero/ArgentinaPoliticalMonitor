#!/usr/bin/env python3
"""
forecast_ar.py — the Blend model: chance that Javier Milei wins the 2027 presidential election.
Writes forecast_ar.json. (The Simulation model is forecast_sim.py.)

A hybrid model in the sense of Pasek (2015): three independent reads, combined with weights that
shift toward the polls as the election approaches.

1. Polls. Milei's two-way share in the Milei–Kicillof runoff average (encuestas_history.json).
   Election-day uncertainty has two parts:
     * the polls' own error on election day — SD 5.2 points of two-way share, the root-mean-square
       error of the final-month poll average in the eight national contests of 2015–2023
       (pollster_polls.csv vs pollster_elections.csv; no consistent partisan direction);
     * how much opinion can still move — a random walk calibrated on the runoff average itself
       (SD 2.0 points per 30 days), scaled by the square root of the time left.
   P(polls) = P(two-way share > 50) under that normal distribution.

2. Fundamentals (machine learning). The cross-national model in forecast_latam.py — a ridge-
   penalised logistic regression trained on Latin American presidential elections, predicting
   whether the governing party keeps the presidency from the president's approval and whether the
   president is on the ballot — applied to Milei's approval average (approval_history.json) with
   the president running. Its leave-one-out error is reported in forecast_latam_model.json.
   If that model has not been trained yet, this component is left out and its weight is shared out.

3. Market. Polymarket's price for Milei (polymarket_history.json).

Combination (averaged on the log-odds scale): the market gets a fixed 20%; the remaining 80% is
split between polls and fundamentals, with the polls' share = 1 − d/(d + 180), d = days to the
first round. A year out the fundamentals weigh about two-thirds of that 80%; on election eve the
polls weigh almost all of it.

The series is recomputed for every day since the runoff average starts, using only data available
on that day, so the chart shows how the forecast would have looked in real time.
"""

import json, math, datetime, pathlib
import numpy as np

HERE = pathlib.Path(__file__).parent
FIRST_ROUND = datetime.date(2027, 10, 24)
POLL_SD_FINAL = 5.2       # pts of two-way share, RMSE of final-month averages 2015–2023
DRIFT_SD_30D = 2.0        # pts per 30 days, random walk of the runoff average
W_MARKET, HALF_LIFE = 0.20, 180


def load(name):
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def at(dates, vals, d):
    out = None
    for x, v in zip(dates, vals):
        if x <= d and v is not None:
            out = v
    return out


def norm_cdf(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def p_polls(share, days):
    sd = math.sqrt(POLL_SD_FINAL ** 2 + DRIFT_SD_30D ** 2 * max(days, 0) / 30)
    return 1 - norm_cdf((50 - share) / sd), sd


def fundamentals_model():
    m = load("forecast_latam_model.json")
    if not m or "coef" not in m:
        return None
    a, b = m["coef"]["intercept"], m["coef"]["slopes"]
    feats = m["features"]

    def f(approval, running=1):
        x = {"approval": approval, "running": running}
        z = a + sum(bi * x[k] for bi, k in zip(b, feats))
        return 1 / (1 + math.exp(-z))
    return f


def main():
    ro = load("encuestas_history.json")["avg"]
    ap = load("approval_history.json")["avg"]
    pm = load("polymarket_history.json") or {"candidates": []}
    mil = next((c for c in pm["candidates"] if "Milei" in c["name"]), None)
    fund = fundamentals_model()

    start = datetime.date.fromisoformat(ro["dates"][0])
    today = datetime.date.today()
    rows = []
    d = start
    while d <= today:
        iso = d.isoformat()
        m, k = at(ro["dates"], ro["milei"], iso), at(ro["dates"], ro["kicillof"], iso)
        days = (FIRST_ROUND - d).days
        share = 100 * m / (m + k)
        pp, sd = p_polls(share, days)
        appr = at(ap["dates"], ap["approve"], iso)
        pf = fund(appr) if (fund and appr is not None) else None
        pmk = None
        if mil:
            v = at([p[0] for p in mil["points"]], [p[1] for p in mil["points"]], iso)
            pmk = v / 100 if v is not None else None
        w_poll_share = 1 - days / (days + HALF_LIFE)
        comps = [(pp, (1 - W_MARKET) * w_poll_share)]
        if pf is not None:
            comps.append((pf, (1 - W_MARKET) * (1 - w_poll_share)))
        if pmk is not None:
            comps.append((pmk, W_MARKET))
        wsum = sum(w for _, w in comps)
        z = sum(w * logit(p) for p, w in comps) / wsum
        p = 1 / (1 + math.exp(-z))
        rows.append({"date": iso, "p": round(p, 3), "p_polls": round(pp, 3),
                     "p_fund": None if pf is None else round(pf, 3),
                     "p_market": None if pmk is None else round(pmk, 3),
                     "share": round(share, 1), "sd": round(sd, 1), "approval": appr,
                     "weights": {"polls": round(comps[0][1] / wsum, 2),
                                 "fundamentals": round((1 - W_MARKET) * (1 - w_poll_share) / wsum, 2) if pf is not None else 0,
                                 "market": round(W_MARKET / wsum, 2) if pmk is not None else 0}})
        d += datetime.timedelta(days=1)

    last = rows[-1]
    # distribution of Milei's runoff share on election day, for the histogram
    rng = np.random.default_rng(2027)
    sims = rng.normal(last["share"], last["sd"], 40000)
    bins = np.arange(30, 71, 1)
    hist, _ = np.histogram(sims, bins=bins)
    out = {
        "updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "election": FIRST_ROUND.isoformat(),
        "latest": last,
        "series": {k: [r[k] for r in rows] for k in ("date", "p", "p_polls", "p_fund", "p_market")},
        "runoff_distribution": {"bins": bins[:-1].tolist(), "share_of_sims": (hist / len(sims)).round(4).tolist()},
        "method": {"poll_sd_final": POLL_SD_FINAL, "drift_sd_30d": DRIFT_SD_30D, "w_market": W_MARKET,
                   "half_life_days": HALF_LIFE, "fundamentals": "forecast_latam_model.json" if fund else None},
    }
    (HERE / "forecast_ar.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Milei: {last['p']:.0%} (polls {last['p_polls']:.0%}, fund {last['p_fund']}, market {last['p_market']}); "
          f"runoff share {last['share']}% ± {last['sd']}")


if __name__ == "__main__":
    main()
