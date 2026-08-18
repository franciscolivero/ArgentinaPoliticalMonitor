#!/usr/bin/env python3
"""
fetch_encuestar.py — snapshot the Milei-vs-Kicillof ballotage poll series from EncuestAR.

Runs server-side (e.g. from a GitHub Action), so it is NOT subject to browser CORS.
It reads the public poll-aggregator table at encuestar.netlify.app/encuestas/, keeps the
"Ballotage hipotético: Milei vs. Kicillof" rows, builds a per-poll series plus a smoothed
poll-average, and writes encuestas_history.json at the repo root. The dashboard reads that
file (same-origin, no CORS) to draw chart 2 ("Milei vs. Kicillof — runoff voting intention").

Each poll belongs to the firm that ran it; the aggregation and labels are EncuestAR's,
reused here with attribution per their published license. No value is ever invented — if the
fetch or the parse fails, the existing encuestas_history.json is left untouched.

Usage:
    pip install requests beautifulsoup4 lxml numpy
    python fetch_encuestar.py                    # live scrape -> encuestas_history.json
    python fetch_encuestar.py --from-file p.html  # parse a saved copy of the page instead
    python fetch_encuestar.py --seed seed.tsv     # build the JSON from a tab-separated seed
                                                  #   (columns: consultora <tab> carrera <tab> n <tab> resultados)
"""

import re, sys, json, datetime, pathlib

SRC_URL = "https://encuestar.netlify.app/encuestas/"
OUT = "encuestas_history.json"

# Which race to keep. The row's "Carrera" cell must match this (case-insensitive).
# Kept deliberately tight to one methodology so the dots stay comparable. Widen if needed,
# e.g. RACE_RE = re.compile(r"milei\s*vs\.?\s*kicillof", re.I) to also pull "escenario" rows.
RACE_RE = re.compile(r"ballotage.*milei.*kicillof|ballotage.*kicillof.*milei", re.I)

MILEI_RE = re.compile(r"Javier\s+Milei\s+(\d+(?:[.,]\d+)?)", re.I)
KICI_RE  = re.compile(r"Axel\s+Kicillof\s+(\d+(?:[.,]\d+)?)", re.I)
DATE_RE  = re.compile(
    r"(\d{1,2})\s+"
    r"(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)\w*\.?\s+"
    r"(\d{4})", re.I)
INCOMPLETE_RE = re.compile(r"sin\s+ficha\s+completa", re.I)

ES_MONTHS = {"ene":1,"feb":2,"mar":3,"abr":4,"may":5,"jun":6,
             "jul":7,"ago":8,"sep":9,"set":9,"oct":10,"nov":11,"dic":12}

BW_DAYS   = 16    # Gaussian kernel bandwidth for the smoothed average
STEP_DAYS = 7     # one average point per week


# ----------------------------------------------------------------- fetching
def fetch_html():
    import requests
    r = requests.get(SRC_URL, headers={"User-Agent": "arg2027-monitor/1.0 (+github-pages)"},
                     timeout=40)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


# ----------------------------------------------------------------- row extraction
def _num(s):
    return float(s.replace(".", "").replace(",", ".")) if ("," in s) else float(s)


def parse_date(cell):
    m = DATE_RE.search(cell)
    if not m:
        return None
    day, mon, year = int(m.group(1)), ES_MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        return datetime.date(year, mon, day).isoformat()
    except ValueError:
        return None


def firm_name(cell):
    """Strip the trailing date and any '· sin ficha completa' note off the consultora cell."""
    m = DATE_RE.search(cell)
    name = cell[:m.start()] if m else cell
    return re.sub(r"\s+", " ", name).strip(" ·-–—")


def make_poll(consultora, carrera, n_cell, resultados, url=None):
    """Turn one row's cells into a poll dict, or None if it isn't a usable ballotage row."""
    if not RACE_RE.search(carrera or ""):
        return None
    mm, kk = MILEI_RE.search(resultados or ""), KICI_RE.search(resultados or "")
    if not (mm and kk):
        return None
    date = parse_date(consultora or "")
    if not date:
        return None
    n = None
    if n_cell:
        digits = re.sub(r"[^\d]", "", n_cell)
        n = int(digits) if digits else None
    return {"date": date, "firm": firm_name(consultora),
            "milei": _num(mm.group(1)), "kicillof": _num(kk.group(1)),
            "n": n, "archived": bool(INCOMPLETE_RE.search(consultora or "")),
            "url": url}


def rows_from_html(html):
    """Locate the polls table and yield (consultora, carrera, n, resultados, url) per row."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    table = None
    for t in soup.find_all("table"):
        head = " ".join(th.get_text(" ", strip=True).lower() for th in t.find_all("th"))
        if "consultora" in head and "carrera" in head:
            table = t
            break
    if table is None:
        raise RuntimeError("polls table not found on page")
    out = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if len(tds) < 7:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        link = tds[-1].find("a", href=True)
        out.append((cells[0], cells[1], cells[3], cells[6],
                    link["href"] if link else None))
    return out


# ----------------------------------------------------------------- aggregation
def smoothed_average(polls):
    import numpy as np
    comp = [p for p in polls if not p["archived"]]
    if len(comp) < 2:
        comp = polls[:]                    # too few complete polls: fall back to all
    if len(comp) < 2:
        return {"dates": [], "milei": [], "kicillof": []}
    d0 = min(datetime.date.fromisoformat(p["date"]) for p in comp)
    d1 = max(datetime.date.fromisoformat(p["date"]) for p in comp)
    xs = np.array([(datetime.date.fromisoformat(p["date"]) - d0).days for p in comp], float)
    ym = np.array([p["milei"] for p in comp], float)
    yk = np.array([p["kicillof"] for p in comp], float)

    grid, d = [], d0
    while d <= d1:
        grid.append(d)
        d += datetime.timedelta(days=STEP_DAYS)
    if grid[-1] != d1:
        grid.append(d1)

    dates, mm, kk = [], [], []
    for g in grid:
        w = np.exp(-0.5 * ((xs - (g - d0).days) / BW_DAYS) ** 2)
        s = w.sum()
        if s <= 0:
            continue
        dates.append(g.isoformat())
        mm.append(round(float((w * ym).sum() / s), 1))
        kk.append(round(float((w * yk).sum() / s), 1))
    return {"dates": dates, "milei": mm, "kicillof": kk}


def build(polls):
    # de-dup on (firm, date); page is newest-first so the first occurrence wins
    seen, uniq = set(), []
    for p in polls:
        key = (p["firm"].lower(), p["date"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: p["date"])
    return {
        "updated": datetime.datetime.utcnow().isoformat() + "Z",
        "source": SRC_URL,
        "race": "Ballotage hipotético: Milei vs. Kicillof",
        "attribution": "Poll aggregation by EncuestAR (encuestar.netlify.app); "
                       "each poll © the firm that ran it.",
        "polls": uniq,
        "avg": smoothed_average(uniq),
    }


# ----------------------------------------------------------------- seed / file paths
def polls_from_seed(path):
    polls = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.rstrip("\n")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        while len(parts) < 4:
            parts.append("")
        p = make_poll(parts[0], parts[1], parts[2], parts[3])
        if p:
            polls.append(p)
    return polls


def main():
    args = sys.argv[1:]
    try:
        if "--seed" in args:
            polls = polls_from_seed(args[args.index("--seed") + 1])
        elif "--from-file" in args:
            html = pathlib.Path(args[args.index("--from-file") + 1]).read_text(encoding="utf-8")
            polls = [make_poll(*r) for r in rows_from_html(html)]
            polls = [p for p in polls if p]
        else:
            polls = [make_poll(*r) for r in rows_from_html(fetch_html())]
            polls = [p for p in polls if p]
    except Exception as e:
        print(f"  ! fetch/parse failed ({e}); leaving {OUT} untouched.", file=sys.stderr)
        sys.exit(1)

    if len(polls) < 3:
        print(f"  ! only {len(polls)} ballotage rows parsed; that looks wrong — "
              f"leaving {OUT} untouched.", file=sys.stderr)
        sys.exit(1)

    data = build(polls)
    pathlib.Path(OUT).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                 encoding="utf-8")
    last = data["avg"]
    tip = f"Milei {last['milei'][-1]} / Kicillof {last['kicillof'][-1]}" if last["dates"] else "n/a"
    print(f"wrote {OUT}: {len(data['polls'])} polls "
          f"({data['polls'][0]['date']} → {data['polls'][-1]['date']}), "
          f"latest average {tip}")


if __name__ == "__main__":
    main()
