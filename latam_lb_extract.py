#!/usr/bin/env python3
"""
latam_lb_extract.py — presidential approval by country and year from Latinobarómetro microdata.

Every Latinobarómetro wave since 2002 asks "¿Aprueba Ud. o no la gestión del gobierno que encabeza
el presidente …?" (1995–2001 do not). The variable is named differently in each wave, so it is
found by its Stata label (approval + "gestión" + gobierno/presidente). Approval = weighted share
answering "Aprueba" among respondents who were asked the question: don't-knows stay in the
denominator, as in the monitor's poll averages, while "No preguntada", "No aplicable" and
"Dato no disponible" are left out (otherwise a country where the item was skipped would read 0%).

Fieldwork dates come from each interview's day and month (diareal, mesreal): fw_start and fw_end are
the 5th and 95th percentile interview dates in that country, so forecast_latam.py can match every
election with a wave fielded before the vote.

Output: latam_lb_approval.csv  (year, iso, approval, n, variable, fw_start, fw_end)

Usage:  python latam_lb_extract.py <folder with the .dta files>
"""

import sys, re, csv, glob, pathlib
import pandas as pd

ISO = {32: "AR", 68: "BO", 76: "BR", 152: "CL", 170: "CO", 188: "CR", 214: "DO", 218: "EC", 222: "SV",
       320: "GT", 340: "HN", 484: "MX", 558: "NI", 591: "PA", 600: "PY", 604: "PE", 858: "UY", 862: "VE"}


def find(labels, *pats):
    for c, l in labels.items():
        text = f"{c} {l or ''}".lower()
        if all(re.search(p, text) for p in pats):
            return c
    return None


def main():
    folder = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    rows = []
    for f in sorted(folder.glob("*.dta")):
        r = pd.io.stata.StataReader(f)
        labels = r.variable_labels()
        # the approval question (some waves ask it twice, e.g. Honduras 2009 after the coup)
        vars_ = [c for c, l in labels.items()
                 if re.search(r"aprob|aprueb", f"{c} {l or ''}".lower()) and re.search(r"gesti", (l or "").lower())
                 and re.search(r"gobierno|presidente", (l or "").lower())]
        var = vars_[0] if vars_ else None
        cty = find(labels, r"^idenpa|pa[ií]s") or ("IDENPA" if "IDENPA" in labels else None)
        yr = find(labels, r"^numinves|^a[nñ]o|year")
        wt = next((c for c, l in labels.items()
                   if re.match(r"^(wt\d*|pondera\w*|peso)$", c.lower()) or re.search(r"ponderador", (l or "").lower())), None)
        if not var or not cty:
            print(f"  · {f.name}: no approval variable found")
            continue
        # respondents who were asked: Aprueba (1), No aprueba (2) or a don't-know / no-answer code;
        # "No preguntada", "No aplicable" and "Dato no disponible" are not part of the sample
        vlab = {}
        try:
            names = r._lbllist if hasattr(r, "_lbllist") else []
            allv = r.value_labels()
            for v in vars_:
                ln = names[list(labels).index(v)] if names else None
                vlab[v] = {int(k): str(t).lower() for k, t in allv.get(ln, {}).items()}
        except Exception:
            pass
        def asked(v, x):
            if x in (1, 2):
                return True
            t = vlab.get(v, {}).get(int(x), "")
            return bool(re.search(r"sabe|contesta|responde", t)) or (not t and x == -1)
        dd = find(labels, r"^diareal") or find(labels, r"d[ií]a de realizaci")
        mm = find(labels, r"^mesreal") or find(labels, r"mes de realizaci")
        cols = [c for c in {*vars_, cty, yr, wt, dd, mm} if c]
        d = pd.read_stata(f, columns=cols, convert_categoricals=False)
        year = int(d[yr].mode()[0]) if yr and d[yr].notna().any() else int(re.findall(r"(19|20)\d\d", f.name)[0])
        if year < 1990:
            year = int(re.search(r"(?:19|20)\d\d", f.name).group(0))
        d["w"] = d[wt] if wt else 1.0
        for code, g in d.groupby(cty):
            iso = ISO.get(int(code)) if pd.notna(code) else None
            if not iso:
                continue
            # first version of the question that was really asked in this country (Honduras 2009:
            # the general item has no valid answers there, a Honduras-specific one does)
            for v in vars_:
                ok = g[v].notna() & g[v].map(lambda x: pd.notna(x) and asked(v, x))
                gg = g[ok]
                app = gg.loc[gg[v] == 1, "w"].sum()
                if ok.sum() >= 300 and app > 0:
                    break
            else:
                continue
            fw = ("", "")
            if dd and mm:
                dm = gg[[mm, dd]].apply(pd.to_numeric, errors="coerce")
                dm = dm[(dm[mm].between(1, 12)) & (dm[dd].between(1, 31))]
                if len(dm):
                    ms = dm[mm].astype(int)
                    # a wave that runs past New Year (e.g. Nov–Jan): early months belong to year + 1
                    late = (ms >= 9).mean() > 0.5
                    ys = [year + 1 if (late and m <= 3) else year for m in ms]
                    dates = sorted(pd.Timestamp(y, m, min(int(x), 28)) for y, m, x in zip(ys, ms, dm[dd]))
                    q = lambda p: dates[min(len(dates) - 1, int(p * len(dates)))].date().isoformat()
                    fw = (q(0.05), q(0.95))
            rows.append({"year": year, "iso": iso, "approval": round(100 * app / gg["w"].sum(), 1), "n": int(ok.sum()),
                         "variable": v, "fw_start": fw[0], "fw_end": fw[1]})
        print(f"  ✓ {f.name}: {year}, variable {var}, {d[cty].nunique()} countries")
    with open("latam_lb_approval.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["year", "iso", "approval", "n", "variable", "fw_start", "fw_end"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["iso"], x["year"])))
    print(f"wrote latam_lb_approval.csv ({len(rows)} country-years)")


if __name__ == "__main__":
    main()
