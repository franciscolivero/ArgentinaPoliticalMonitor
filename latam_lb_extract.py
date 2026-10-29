#!/usr/bin/env python3
"""
latam_lb_extract.py — presidential approval by country and year from Latinobarómetro microdata.

Every Latinobarómetro wave asks "¿Aprueba Ud. o no la gestión del gobierno que encabeza el
presidente …?". The variable is named differently in each wave, so it is found by its Stata label
(contains "aprob" and "gobierno"/"presidente"). Approval = weighted share answering "Aprueba" over
all respondents (don't-knows stay in the denominator, as in the monitor's poll averages).

Output: latam_lb_approval.csv  (year, iso, approval, n, variable)

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
        var = find(labels, r"aprob", r"gobierno|presidente")
        cty = find(labels, r"^idenpa|pa[ií]s") or ("IDENPA" if "IDENPA" in labels else None)
        yr = find(labels, r"^numinves|^a[nñ]o|year")
        wt = next((c for c, l in labels.items()
                   if re.match(r"^(wt\d*|pondera\w*|peso)$", c.lower()) or re.search(r"ponderador", (l or "").lower())), None)
        if not var or not cty:
            print(f"  · {f.name}: no approval variable found")
            continue
        cols = [c for c in {var, cty, yr, wt} if c]
        d = pd.read_stata(f, columns=cols, convert_categoricals=False)
        year = int(d[yr].mode()[0]) if yr and d[yr].notna().any() else int(re.findall(r"(19|20)\d\d", f.name)[0])
        if year < 1990:
            year = int(re.search(r"(?:19|20)\d\d", f.name).group(0))
        d["w"] = d[wt] if wt else 1.0
        for code, g in d.groupby(cty):
            iso = ISO.get(int(code)) if pd.notna(code) else None
            if not iso:
                continue
            total = g["w"].sum()
            app = g.loc[g[var] == 1, "w"].sum()
            rows.append({"year": year, "iso": iso, "approval": round(100 * app / total, 1), "n": len(g), "variable": var})
        print(f"  ✓ {f.name}: {year}, variable {var}, {d[cty].nunique()} countries")
    with open("latam_lb_approval.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["year", "iso", "approval", "n", "variable"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["iso"], x["year"])))
    print(f"wrote latam_lb_approval.csv ({len(rows)} country-years)")


if __name__ == "__main__":
    main()
