#!/usr/bin/env python3
"""
forecast_latam.py — chance that each Latin American governing party keeps the presidency at the
next election. Trains the model, validates it, and writes forecast_latam_model.json (the model,
also used by forecast_ar.py as its fundamentals) and forecast_latam.json (today's forecasts).

Training data
  latam_elections.csv     every competitive presidential election 2000–2026 in 17 countries where
                          the governing party had a stake: did it keep the presidency, and was the
                          president on the ballot. Left out: Nicaragua 2006–2021 and Venezuela after
                          2013 (not competitive), Peru (governing parties rarely field candidates),
                          caretaker governments with no party (Bolivia 2005, Ecuador 2002/2006/2023).
  latam_lb_approval.csv   Latinobarómetro: share approving "la gestión del gobierno que encabeza el
                          presidente", by country and year (latam_lb_extract.py). Each election
                          takes the latest wave fielded before it (same year if the election is
                          from September on, otherwise the year before), up to two years back.

Model (machine learning with few cases)
  Ridge-penalised logistic regression: P(governing party wins) ~ approval + president running.
  The penalty is chosen by leave-one-out cross-validation, and the leave-one-out Brier score,
  log loss and accuracy are reported against a base-rate-only benchmark.

Forecasts
  Today's approval average for each president (latam_approval.json; Argentina from
  approval_history.json) goes into the model with that country's next election and whether the
  president can run. The model was trained on approval measured within about a year of each
  election, so for elections further away the forecast is pulled toward the historical base rate:
  weight on the model = 1 for elections within a year, falling linearly to 0.25 at five years.
"""

import csv, json, datetime, pathlib
import numpy as np
from ml_utils import RidgeLogit, loo, choose_lambda

HERE = pathlib.Path(__file__).parent

# next presidential election (first round) and whether the sitting president can be on the ballot
NEXT = {
    "AR": ("2027-10-24", 1, "exact"), "SV": ("2027-02-28", 1, "exact"), "GT": ("2027-06-27", 0, "exact"),
    "EC": ("2029-02-11", 0, "approx"), "PA": ("2029-05-06", 0, "approx"), "CL": ("2029-11-18", 0, "approx"),
    "HN": ("2029-11-25", 0, "approx"), "CO": ("2030-05-26", 0, "approx"), "MX": ("2030-06-02", 0, "approx"),
    "BO": ("2030-08-18", 1, "approx"), "BR": ("2030-10-06", 0, "approx"), "PE": ("2031-04-13", 0, "approx"),
}
FEATURES = ["approval", "running"]


def load_training():
    lb = {}
    with open(HERE / "latam_lb_approval.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            lb[(r["iso"], int(r["year"]))] = float(r["approval"])
    rows = []
    with open(HERE / "latam_elections.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = datetime.date.fromisoformat(r["date"])
            first = d.year if d.month >= 9 else d.year - 1
            appr, wave = None, None
            for y in range(first, first - 3, -1):
                if (r["iso"], y) in lb:
                    appr, wave = lb[(r["iso"], y)], y
                    break
            if appr is None:
                continue
            rows.append({**r, "approval": appr, "lb_wave": wave,
                         "running": int(r["incumbent_running"]), "won": int(r["incumbent_party_won"])})
    return rows


def current_inputs():
    L = json.loads((HERE / "latam_approval.json").read_text(encoding="utf-8"))
    out = {}
    for code, k in L["countries"].items():
        av = (k.get("avg") or {}).get("approve") or {}
        if len(k.get("polls", [])) >= 2 and av.get("values"):
            out[code] = (k["country"], k["president"], av["values"][-1], av["dates"][-1])
    ap = json.loads((HERE / "approval_history.json").read_text(encoding="utf-8"))
    out["AR"] = ("Argentina", "Javier Milei", ap["latest"]["approve"], ap["avg"]["dates"][-1])
    return out


def main():
    if not (HERE / "latam_lb_approval.csv").exists():
        print("latam_lb_approval.csv not found: run latam_lb_extract.py on the Latinobarómetro files first; "
              "skipping the Latin American model.")
        return
    rows = load_training()
    X = np.array([[r[f] for f in FEATURES] for r in rows], float)
    y = np.array([r["won"] for r in rows], float)
    lam, grid = choose_lambda(X, y)
    cv = loo(X, y, lam)
    model = RidgeLogit(lam).fit(X, y)
    a, b = model.coef_original_scale()
    base = float(y.mean())
    for r, p in zip(rows, cv["p"]):
        r["p_loo"] = round(p, 3)

    meta = {
        "updated": datetime.date.today().isoformat(),
        "features": FEATURES, "lambda": lam, "lambda_grid_logloss": {str(k): round(v, 4) for k, v in grid.items()},
        "coef": {"intercept": a, "slopes": b},
        "n": len(rows), "base_rate": round(base, 3),
        "loo": {k: round(v, 4) for k, v in cv.items() if k != "p"},
        "training": [{k: r[k] for k in ("country", "iso", "date", "approval", "lb_wave", "running", "won", "p_loo")}
                     for r in rows],
    }
    (HERE / "forecast_latam_model.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    today = datetime.date.today()
    preds = []
    for code, (country, pres, appr, asof) in current_inputs().items():
        if code not in NEXT:
            continue
        date, running, prec = NEXT[code]
        years = (datetime.date.fromisoformat(date) - today).days / 365.25
        w = float(np.clip(1 - 0.75 * (years - 1) / 4, 0.25, 1))
        p_model = float(model.predict_proba([[appr, running]])[0])
        p = w * p_model + (1 - w) * base
        preds.append({"iso": code, "country": country, "president": pres, "approval": round(appr, 1),
                      "approval_asof": asof, "next_election": date, "date_precision": prec,
                      "president_running": running, "years_to_election": round(years, 1),
                      "p_model": round(p_model, 3), "weight_model": round(w, 2), "p": round(p, 3)})
    preds.sort(key=lambda r: -r["p"])
    out = {"updated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "model": {k: meta[k] for k in ("n", "base_rate", "loo", "coef", "lambda", "features")},
           "forecasts": preds}
    (HERE / "forecast_latam.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"trained on {len(rows)} elections (base rate {base:.0%}), lambda {lam}; "
          f"LOO Brier {cv['brier']:.3f} vs base {cv['brier_baseline']:.3f}, accuracy {cv['accuracy']:.0%}")
    print(f"coef: intercept {a:.2f}, " + ", ".join(f"{f} {x:.3f}" for f, x in zip(FEATURES, b)))
    for r in preds:
        print(f"  {r['iso']} {r['president']:<28} appr {r['approval']:>5}  run {r['president_running']}  "
              f"{r['next_election']}  model {r['p_model']:.0%}  final {r['p']:.0%}")


if __name__ == "__main__":
    main()
