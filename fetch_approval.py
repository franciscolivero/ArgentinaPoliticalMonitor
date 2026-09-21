#!/usr/bin/env python3
"""
fetch_approval.py — pull new presidential-approval polls from EncuestAR and merge them into
approval_polls.csv, which build_approval.py turns into the Chart 04 average.

Sibling of fetch_encuestar.py / fetch_encuestar_firstround.py: it reads the same public table at
encuestar.netlify.app/encuestas/ (Consultora | Carrera | ... | n | ... | Resultados | link), keeps
the rows whose "Carrera" is approval of the national government's gestión, and parses the
approve / disapprove shares out of the Resultados cell.

Merge rules (the hand-curated CSV always wins):
  * Firm names are normalised to the names already used in the CSV (FIRM_ALIASES + accent- and
    suffix-insensitive matching), so house effects keep accumulating on one firm, not two.
  * A scraped poll that matches an existing row (same firm, fieldwork within MATCH_DAYS, approve
    within 1 pt) is the same poll:
      - manual row with an exact date  -> left untouched
      - manual row with an approximate (mid-month) date -> gets the exact date, date_approx=0
      - earlier scraped row            -> refreshed with the current values
  * Anything else is appended with origin=encuestar.
  * Sanity checks: 5 <= approve <= 90, approve + disapprove <= 102. Failing rows are skipped.
Nothing is invented: if the fetch/parse fails the CSV is untouched (exit 1); if no approval rows
are on the page it is also untouched (exit 0, a normal "nothing new" day).

  ┌─ CONFIRM ON FIRST RUN ─────────────────────────────────────────────────────────────────┐
  │ APPROVAL_RE is the Carrera filter and the APPROVE_RE / DISAPPROVE_RE patterns read the │
  │ Resultados cell. They were written against EncuestAR's conventions for the other races,│
  │ not against a live approval row. Run  `python fetch_approval.py --list-carreras`  once │
  │ and `--dry-run` to see what would be merged; adjust the regex lines if the labels differ.│
  └────────────────────────────────────────────────────────────────────────────────────────┘

Usage:
    pip install requests beautifulsoup4 lxml
    python fetch_approval.py                  # live scrape -> merge into approval_polls.csv
    python fetch_approval.py --list-carreras  # show Carrera labels and which ones are kept
    python fetch_approval.py --dry-run        # print what would change, write nothing
    python fetch_approval.py --from-file p.html
"""

import csv, re, sys, datetime, pathlib, unicodedata
from collections import Counter

from fetch_encuestar_firstround import rows_from_html, parse_date, firm_name, fetch_html, _num

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "approval_polls.csv"
FIELDS = ["firm", "end_date", "approve", "disapprove", "n", "date_approx", "source", "origin"]
MATCH_DAYS = 20

# --- which rows ------------------------------------------------------------------------------
# approval of the national government / Milei's gestión; NOT imagen (favourability), NOT vote
APPROVAL_RE = re.compile(r"aprobaci[oó]n|(?<!des)aprueb|gesti[oó]n", re.I)
EXCLUDE_RE = re.compile(r"imagen|intenci[oó]n|ballotage|balotaje|vuelta|gobernador|provincia|"
                        r"kicillof|jefe de gobierno|intendente|municip", re.I)

# --- which numbers ---------------------------------------------------------------------------
NUM = r"(\d{1,2}(?:[.,]\d+)?)\s*%?"
APPROVE_RE = re.compile(r"(?<!des)(?:aprueba|aprobaci[oó]n|positiv[oa]s?)\s*:?\s*" + NUM, re.I)
DISAPPROVE_RE = re.compile(r"(?:desaprueba|desaprobaci[oó]n|negativ[oa]s?)\s*:?\s*" + NUM, re.I)
N_RE = re.compile(r"(\d[\d.,]*)")

# --- firm names: scraped spelling -> the name used in approval_polls.csv ----------------------
FIRM_ALIASES = {
    "udesa": "ESPOP (UdeSA)", "espop": "ESPOP (UdeSA)", "universidad de san andres": "ESPOP (UdeSA)",
    "atlas": "AtlasIntel", "atlasintel": "AtlasIntel", "atlas intel": "AtlasIntel",
    "zuban": "Zuban Córdoba", "zuban cordoba": "Zuban Córdoba",
    "management fit": "Management & Fit", "m&f": "Management & Fit",
    "poliarquia": "Poliarquía", "qsocial": "QSocial", "synopsis": "Synopsis", "delfos": "Delfos",
    "alaska": "Alaska + Trespuntozero", "trespuntozero": "Alaska + Trespuntozero",
}


def _key(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[&+/]", " ", s)
    s = re.sub(r"\b(y asociados|asociados|consultores|consultora|consulting|big data|sa|srl)\b", " ", s)
    return re.sub(r"[^a-z0-9 ]", " ", re.sub(r"\s+", " ", s)).strip()


def canonical_firm(name, known):
    k = _key(name)
    for f in known:                              # exact match on a name already in the CSV
        if _key(f) == k:
            return f
    for alias, canon in FIRM_ALIASES.items():    # alias contained in the scraped name
        if _key(alias) and _key(alias) in k:
            return canon
    return re.sub(r"\s+", " ", name).strip()


def is_approval(carrera):
    c = carrera or ""
    return bool(APPROVAL_RE.search(c)) and not EXCLUDE_RE.search(c)


def make_poll(consultora, carrera, n_cell, resultados, url, known):
    if not is_approval(carrera):
        return None
    a, d = APPROVE_RE.search(resultados or ""), DISAPPROVE_RE.search(resultados or "")
    date = parse_date(consultora or "")
    if not (a and date):
        return None
    ap = _num(a.group(1))
    dp = _num(d.group(1)) if d else None
    if not (5 <= ap <= 90) or (dp is not None and (ap + dp > 102 or not 5 <= dp <= 95)):
        return None
    m = N_RE.search(n_cell or "")
    n = int(re.sub(r"[.,]", "", m.group(1))) if m else None
    if n is not None and not (200 <= n <= 100000):
        n = None
    return {"firm": canonical_firm(firm_name(consultora), known), "end_date": date,
            "approve": ap, "disapprove": dp, "n": n, "date_approx": 0,
            "source": url or "https://encuestar.netlify.app/encuestas/", "origin": "encuestar"}


# ---------------------------------------------------------------- CSV merge
def read_csv():
    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r.setdefault("origin", "")
        r["origin"] = r["origin"] or "manual"
    return rows


def write_csv(rows):
    rows.sort(key=lambda r: (r["firm"], r["end_date"]))
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})


def fmt(v):
    return "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


def merge(rows, polls):
    log = []
    for p in polls:
        t = datetime.date.fromisoformat(p["end_date"])
        match = None
        for r in rows:
            if r["firm"] != p["firm"] or not r.get("approve"):
                continue
            dt = abs((datetime.date.fromisoformat(r["end_date"]) - t).days)
            if dt <= MATCH_DAYS and abs(float(r["approve"]) - p["approve"]) <= 1.0:
                match = r
                break
        new = {"firm": p["firm"], "end_date": p["end_date"], "approve": fmt(p["approve"]),
               "disapprove": fmt(p["disapprove"]), "n": fmt(p["n"]), "date_approx": "0",
               "source": p["source"], "origin": "encuestar"}
        if match is None:
            rows.append(new)
            log.append(f"  + {p['firm']} {p['end_date']}: {p['approve']}/{p['disapprove']}")
        elif match["origin"] == "encuestar":
            before = dict(match)
            match.update(new)
            if before != match:
                log.append(f"  ~ {p['firm']} {p['end_date']}: refreshed")
        elif match.get("date_approx") == "1":
            match["end_date"], match["date_approx"] = p["end_date"], "0"
            if not match.get("n") and p["n"]:
                match["n"] = fmt(p["n"])
            if not match.get("disapprove") and p["disapprove"] is not None:
                match["disapprove"] = fmt(p["disapprove"])
            log.append(f"  = {p['firm']}: manual row dated exactly -> {p['end_date']}")
        # manual row with exact date: hand curation wins, nothing to do
    return log


def main():
    args = sys.argv[1:]
    try:
        if "--from-file" in args:
            html = pathlib.Path(args[args.index("--from-file") + 1]).read_text(encoding="utf-8")
        else:
            html = fetch_html()
        table = rows_from_html(html)
    except Exception as e:
        print(f"  ! fetch/parse failed ({e}); approval_polls.csv untouched.", file=sys.stderr)
        sys.exit(1)

    if "--list-carreras" in args:
        labels = Counter((r[1] or "").strip() for r in table)
        print("Distinct 'Carrera' labels (count):")
        for lab, c in labels.most_common():
            print(f"  {c:>3}  {lab}{'  <-- KEPT (approval)' if is_approval(lab) else ''}")
        return

    rows = read_csv()
    known = sorted({r["firm"] for r in rows})
    polls = [p for p in (make_poll(*r, known) for r in table) if p]
    if not polls:
        print("  · no approval rows on EncuestAR right now; approval_polls.csv untouched.")
        return
    log = merge(rows, polls)
    if not log:
        print(f"  · {len(polls)} approval row(s) parsed, all already in the CSV.")
        return
    print(f"{len(log)} change(s):"); print("\n".join(log))
    if "--dry-run" in args:
        print("  (dry run — nothing written)")
        return
    write_csv(rows)
    print(f"  wrote {CSV_PATH.name}")


if __name__ == "__main__":
    main()
