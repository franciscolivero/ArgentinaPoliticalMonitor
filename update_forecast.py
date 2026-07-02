#!/usr/bin/env python3
"""
update_forecast.py — append this month's default-settings forecast to forecast_history.json.

Run it once a month, after updating data.json and Evolution_ICG.xlsx, from the repo root:

    python update_forecast.py

It reads:
  - Evolution_ICG.xlsx  -> latest Milei ICG (fundamentals input)
  - data.json           -> latest Milei / Kicillof head-to-head averages (polls input)

It recomputes the model's default-settings output (same constants as forecast.html)
and appends/replaces the entry for the current month in forecast_history.json.
Commit forecast_history.json afterwards; the forecast page plots one point per month.
"""

import json, datetime, sys
import numpy as np

# ---- model constants (MUST match forecast.html) ----
FUND_A, FUND_B = -2.0789, 0.8727          # logistic trained on ICG 2003-2023
TAU_A, TAU_B, TAU_LO, TAU_HI = -0.0675, 0.2516, 0.20, 0.40
W = 0.55                                   # weight on polls
SIGMA = 4.5                                # pts, Student-t(5) shocks
N = 200000

def latest_icg(path="Evolution_ICG.xlsx"):
    import openpyxl
    ws = openpyxl.load_workbook(path)[ "Evolutivo" ]
    cur, last = None, None
    for p, m, v in list(ws.values)[1:]:
        if p: cur = p
        if v is not None and cur and "milei" in cur.lower():
            last = float(v)
    return last

def latest_polls(path="data.json"):
    d = json.load(open(path, encoding="utf-8"))
    avg = d.get("headToHead2026", {}).get("avg", {})
    if isinstance(avg, dict) and avg.get("milei") and avg.get("kicillof"):
        return round(float(avg["milei"][-1]), 1), round(float(avg["kicillof"][-1]), 1)
    return 35.0, 26.0

def simulate(m0, k0, tau0, sigma, n=N, df=5, seed=None):
    rng = np.random.default_rng(seed)
    sc = np.sqrt(df / (df - 2))
    m = np.clip(m0 + sigma * rng.standard_t(df, n) / sc, 1, 75)
    k = np.clip(k0 + sigma * rng.standard_t(df, n) / sc, 1, 75)
    o = np.clip(100 - m - k, 0, None)
    tau = np.clip(rng.normal(tau0, 0.08, n), 0.05, 0.95)
    mro = m + tau * o
    fr = (m >= 45) | ((m >= 40) & ((m - k) >= 10) & (m > k))
    ro = (~fr) & (mro > 50) & (m >= k - 6)
    return float((fr | ro).mean())

def main():
    icg = latest_icg()
    if icg is None:
        sys.exit("Could not read Milei ICG from Evolution_ICG.xlsx")
    try:
        m0, k0 = latest_polls()
    except Exception:
        m0, k0 = 35.0, 26.0
        print("data.json head-to-head not found; using fallback 35/26", file=sys.stderr)

    tau0 = min(max(TAU_A + TAU_B * icg, TAU_LO), TAU_HI)
    p_fund = 1 / (1 + np.exp(-(FUND_A + FUND_B * icg)))
    p_polls = simulate(m0, k0, tau0, SIGMA, seed=1234)
    p = W * p_polls + (1 - W) * p_fund

    month = datetime.date.today().replace(day=1).isoformat()
    entry = {"date": month, "p": round(p, 3), "p_polls": round(p_polls, 3),
             "p_fund": round(float(p_fund), 3), "icg": round(float(icg), 2),
             "m": m0, "k": k0, "tau": round(float(tau0), 3), "sigma": SIGMA, "w": W}

    try:
        hist = json.load(open("forecast_history.json", encoding="utf-8"))
    except FileNotFoundError:
        hist = {"entries": []}
    hist["entries"] = [e for e in hist["entries"] if e["date"][:7] != month[:7]]
    hist["entries"].append(entry)
    hist["entries"].sort(key=lambda e: e["date"])
    json.dump(hist, open("forecast_history.json", "w", encoding="utf-8"), indent=1)
    print(f"{month}: P={p*100:.1f}%  (polls {p_polls*100:.1f} / fundamentals {p_fund*100:.1f})  "
          f"inputs m={m0} k={k0} ICG={icg:.2f} tau={tau0:.3f}")
    print(f"forecast_history.json now has {len(hist['entries'])} entries — commit it.")

if __name__ == "__main__":
    main()
