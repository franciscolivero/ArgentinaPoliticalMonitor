#!/usr/bin/env python3
"""
fetch_encuestar_firstround.py — snapshot the FIRST-ROUND Milei/Kicillof vote-intention
series from EncuestAR and write it into data.json, where the forecast reads it.

This is the sibling of fetch_encuestar.py. That one scrapes the *ballotage* (runoff) rows for
chart 2; this one scrapes the *first-round* "intención de voto por candidato" rows, because the
forecast model (forecast.html / update_forecast.py) expects first-round shares — it derives the
"other" bucket as 100 - Milei - Kicillof and models the runoff transfer itself. Feeding runoff
numbers into it would double-count, so the two series are kept separate on purpose.

It reads the public aggregator table at encuestar.netlify.app/encuestas/, keeps the first-round
Milei-vs-Kicillof rows (EXCLUDING any ballotage/segunda-vuelta rows), builds a per-poll series
plus a smoothed poll-average, and rewrites data.json -> headToHead2026 {polls, avg}. Everything
else in data.json is left untouched. Nothing is ever invented: if the fetch or the parse fails,
or fewer than 3 rows match, data.json is left exactly as it was.

  ┌─ ONE THING TO CONFIRM ON FIRST RUN ────────────────────────────────────────────────┐
  │ FIRSTROUND_RE below is the label filter. The ballotage label was verified against    │
  │ the live page ("Ballotage hipotético: Milei vs. Kicillof"); the first-round label    │
  │ was NOT, so run  `python fetch_encuestar_firstround.py --list-carreras`  once and     │
  │ check that the rows it matches are the first-round Milei-vs-Kicillof scenario you     │
  │ want. If EncuestAR's wording differs, adjust the single FIRSTROUND_RE line.           │
  └──────────────────────────────────────────────────────────────────────────────────────┘

Usage:
    pip install requests beautifulsoup4 lxml numpy
    python fetch_encuestar_firstround.py                   # live scrape -> data.json
    python fetch_encuestar_firstround.py --list-carreras   # just print the Carrera labels seen
    python fetch_encuestar_firstround.py --from-file p.html # parse a saved copy of the page
    python fetch_encuestar_firstround.py --seed seed.tsv    # build from a tab-separated seed
                                                            #  (consultora <tab> carrera <tab> n <tab> resultados)
    python fetch_encuestar_firstround.py --out other.json   # write somewhere else (testing)
"""

import re, sys, json, datetime, pathlib
from collections import Counter

SRC_URL = "https://encuestar.netlify.app/encuestas/"
OUT = "data.json"
HEAD_KEY = "headToHead2026"          # the block inside data.json the forecast reads

# --- Which rows to keep -------------------------------------------------------------------
# First-round Milei-vs-Kicillof vote intention. Kept deliberately tight — it mirrors the
# ballotage scraper's filter: a first-round cue AND both candidate names in the "Carrera" cell,
# in either order. This pins it to the explicit Milei-vs-Kicillof first-round scenario and keeps
# out generic "intención de voto" rows, "por espacio" (party) rows, and split-Peronism rows
# (Cristina + Kicillof) that would otherwise bias the average. If EncuestAR's label doesn't name
# both candidates, --list-carreras will show it and you loosen this one line (e.g. drop the
# name requirement) — better than silently over-capturing. BALLOTAGE_RE is a redundant guard.
FIRSTROUND_RE = re.compile(
    r"(primera\s+vuelta|1[ªa]\.?\s*vuelta).*"
    r"(milei\s*vs\.?\s*kicillof|kicillof\s*vs\.?\s*milei)", re.I)
BALLOTAGE_RE = re.compile(
    r"ballotage|balotaje|segunda\s+vuelta|2[ªa]\.?\s*vuelta", re.I)

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


def is_firstround(carrera):
    c = carrera or ""
    return bool(FIRSTROUND_RE.search(c)) and not BALLOTAGE_RE.search(c)


def make_poll(consultora, carrera, n_cell, resultados, url=None):
    """Turn one row's cells into a first-round poll dict, or None if it isn't one."""
    if not is_firstround(carrera):
        return None
    mm, kk = MILEI_RE.search(resultados or ""), KICI_RE.search(resultados or "")
    if not (mm and kk):
        return None
    date = parse_date(consultora or "")
    if not date:
        return None
    return {"date": date, "firm": firm_name(consultora),
            "milei": _num(mm.group(1)), "kicillof": _num(kk.group(1)),
            "archived": bool(INCOMPLETE_RE.search(consultora or ""))}


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


def build_block(polls):
    """Build the {polls, avg} block for data.json.headToHead2026 (minimal poll schema)."""
    seen, uniq = set(), []
    for p in polls:
        key = (p["firm"].lower(), p["date"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    uniq.sort(key=lambda p: p["date"])
    avg = smoothed_average(uniq)
    slim = [{"date": p["date"], "firm": p["firm"],
             "milei": p["milei"], "kicillof": p["kicillof"]} for p in uniq]
    return {"polls": slim, "avg": avg}


# ----------------------------------------------------------------- seed / file input
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


def get_rows(args):
    if "--seed" in args:
        # seed rows carry (consultora, carrera, n, resultados); reuse for label listing too
        out = []
        for line in pathlib.Path(args[args.index("--seed") + 1]).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = (line.rstrip("\n").split("\t") + ["", "", "", ""])[:4]
            out.append((parts[0], parts[1], parts[2], parts[3], None))
        return out
    if "--from-file" in args:
        html = pathlib.Path(args[args.index("--from-file") + 1]).read_text(encoding="utf-8")
        return rows_from_html(html)
    return rows_from_html(fetch_html())


# ----------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    out_path = args[args.index("--out") + 1] if "--out" in args else OUT

    try:
        rows = get_rows(args)
    except Exception as e:
        print(f"  ! fetch/parse failed ({e}); leaving {out_path} untouched.", file=sys.stderr)
        sys.exit(1)

    # Diagnostic: what "Carrera" labels are on the page, and which we keep.
    labels = Counter((r[1] or "").strip() for r in rows)
    kept_labels = Counter((r[1] or "").strip() for r in rows if is_firstround(r[1]))
    if "--list-carreras" in args:
        print("Distinct 'Carrera' labels seen (count):")
        for lab, c in labels.most_common():
            tag = "  <-- KEPT (first-round)" if is_firstround(lab) else ""
            print(f"  {c:>3}  {lab}{tag}")
        print(f"\nFIRSTROUND_RE currently keeps {sum(kept_labels.values())} row(s) "
              f"across {len(kept_labels)} label(s).")
        return

    polls = [p for p in (make_poll(*r) for r in rows) if p]
    if len(polls) < 3:
        print(f"  ! only {len(polls)} first-round rows matched — that looks wrong, so "
              f"leaving {out_path} untouched.\n"
              f"    Run  --list-carreras  to see the labels and adjust FIRSTROUND_RE.",
              file=sys.stderr)
        sys.exit(1)

    block = build_block(polls)

    # read-modify-write: replace only headToHead2026 {polls, avg}; keep the rest of data.json
    try:
        data = json.loads(pathlib.Path(out_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    head = data.get(HEAD_KEY, {}) if isinstance(data.get(HEAD_KEY), dict) else {}
    head["polls"] = block["polls"]
    head["avg"] = block["avg"]
    data[HEAD_KEY] = head
    if isinstance(data.get("meta"), dict):
        data["meta"]["updated"] = datetime.date.today().isoformat()

    pathlib.Path(out_path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    avg = block["avg"]
    tip = (f"Milei {avg['milei'][-1]} / Kicillof {avg['kicillof'][-1]}"
           if avg["dates"] else "n/a")
    print(f"wrote {out_path} [{HEAD_KEY}]: {len(block['polls'])} first-round polls "
          f"({block['polls'][0]['date']} → {block['polls'][-1]['date']}), "
          f"latest average {tip}")
    if len(kept_labels) > 1:
        print("  note: matched more than one Carrera label — confirm they're all the "
              "first-round Milei-vs-Kicillof scenario:")
        for lab, c in kept_labels.most_common():
            print(f"        {c:>3}  {lab}")


if __name__ == "__main__":
    main()
