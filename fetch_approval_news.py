#!/usr/bin/env python3
"""
fetch_approval_news.py — daily harvest of new Milei approval polls from the press.

EncuestAR only relays a handful of approval polls, so on its own the approval average would
stop getting new information. This script reads Google News RSS searches (Argentine edition)
for approval-poll headlines, keeps an item only when it names a known pollster AND states an
approval share of the national government's gestión, and appends it to approval_polls.csv with
origin=news. build_approval.py then rolls it into the average.

Safeguards
  * Firm must resolve through pollster_aliases.csv / the names already in the CSV.
  * Text must speak of approval of the gestión (not imagen, not a governor or a mayor).
  * 15 <= approve <= 70, 20 <= disapprove <= 80, approve + disapprove <= 102.
  * Fieldwork end is rarely in a headline, so the poll is dated publication − 5 days and
    flagged date_approx=1; when EncuestAR (fetch_approval.py) later carries the same poll its
    exact date replaces this one. Hand-entered rows are never touched.
  * The same poll reported by several outlets is merged (same firm, ±20 days, ±1.5 pts).

Usage:
    python fetch_approval_news.py            # merge into approval_polls.csv
    python fetch_approval_news.py --dry-run  # show what would be added
"""

import csv, re, sys, html, datetime, pathlib, unicodedata
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "approval_polls.csv"
ALIASES = HERE / "pollster_aliases.csv"
FIELDS = ["firm", "end_date", "approve", "disapprove", "n", "date_approx", "source", "origin"]
MATCH_DAYS, LOOKBACK_DAYS = 20, 21

QUERIES = [
    "aprobación gestión Milei encuesta",
    "aprueba gestión Milei encuesta",
    "desaprueba gestión Milei encuesta",
    "aprobación del gobierno Milei consultora",
]
RSS = "https://news.google.com/rss/search?q={q}+when:14d&hl=es-419&gl=AR&ceid=AR:es-419"

# extra spellings seen in headlines -> canonical name (pollster_aliases.csv is read on top)
EXTRA = {
    "atlas intel": "AtlasIntel", "atlasintel": "AtlasIntel", "atlas": "AtlasIntel",
    "universidad de san andres": "ESPOP (UdeSA)", "udesa": "ESPOP (UdeSA)", "espop": "ESPOP (UdeSA)",
    "san andres": "ESPOP (UdeSA)", "poliarquia": "Poliarquía", "zuban cordoba": "Zuban Córdoba",
    "zuban": "Zuban Córdoba", "management fit": "Management & Fit", "qsocial": "QSocial",
    "synopsis": "Synopsis", "delfos": "Delfos", "equipo mide": "Equipo Mide",
    "opina argentina": "Opina Argentina", "opinaia": "Opinaia", "trends": "Trends",
    "cb consultora": "CB Consultora", "proyeccion consultores": "Proyección Consultores",
    "analogias": "Analogías", "giacobbe": "Giacobbe & Asociados", "isasi burdman": "Isasi-Burdman",
    "tendencias": "Consultora Tendencias", "dc consultores": "DC Consultores",
    "zx consultora": "ZX Consultora", "aresco": "Aresco", "federico gonzalez": "Federico González",
    "circuitos": "Circuitos", "real time data": "Real Time Data", "taquion": "Taquión",
    "observatorio de psicologia social": "Observatorio de Psicología Social Aplicada (UBA)",
}

OFF_TOPIC = re.compile(r"\bimagen\b(?!.{0,40}gesti[oó]n)|gobernador|intendente|kicillof|"
                       r"llaryora|pullaro|jorge macri|provincia de|bonaerense|municipio", re.I)
ON_TOPIC = re.compile(r"aprob|aprueb|gesti[oó]n", re.I)
PCT = r"(\d{2}(?:[.,]\d{1,2})?)\s*%"
APPROVE = [
    re.compile(PCT + r"[^.%]{0,40}?(?<!des)aprueba", re.I),
    re.compile(r"(?<!des)aprobaci[oó]n[^.%]{0,60}?" + PCT, re.I),
    re.compile(r"(?<!des)aprueba[^.%]{0,30}?" + PCT, re.I),
]
DISAPPROVE = [
    re.compile(PCT + r"[^.%]{0,40}?(?:desaprueba|rechaza)", re.I),
    re.compile(r"(?:desaprobaci[oó]n|rechazo)[^.%]{0,60}?" + PCT, re.I),
    re.compile(r"desaprueba[^.%]{0,30}?" + PCT, re.I),
]


def key(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def alias_table(known):
    table = {key(k): v for k, v in EXTRA.items()}
    try:
        with open(ALIASES, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("alias") and r.get("canonical"):
                    table[key(r["alias"])] = r["canonical"]
    except OSError:
        pass
    for k in known:
        table[key(k)] = k
    # longest alias first so "zuban cordoba" beats "zuban"
    return sorted(table.items(), key=lambda kv: len(kv[0]), reverse=True)


def find_firm(text, table):
    t = " " + key(text) + " "
    for alias, canon in table:
        if alias and f" {alias} " in t:
            return canon
    return None


def first(pats, text):
    for p in pats:
        m = p.search(text)
        if m:
            return float(m.group(1).replace(",", "."))
    return None


def fetch_items():
    import requests
    from urllib.parse import quote_plus
    items = []
    for q in QUERIES:
        try:
            r = requests.get(RSS.format(q=quote_plus(q)), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (arg2027-monitor)"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"  · query failed ({q}): {e}")
            continue
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
            try:
                pub = parsedate_to_datetime(it.findtext("pubDate")).date()
            except Exception:
                continue
            items.append({"title": html.unescape(title), "desc": desc, "pub": pub,
                          "link": it.findtext("link") or ""})
    return items


def parse_item(it, table):
    # Google News titles end with " - Outlet"; drop it so outlet names don't read as firms
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", it["title"])
    text = f"{title}. {it['desc']}"
    if not ON_TOPIC.search(title) or OFF_TOPIC.search(title):
        return None
    firm = find_firm(text, table)
    if not firm:
        return None
    ap, dp = first(APPROVE, title), first(DISAPPROVE, title)
    if ap is None:
        ap = first(APPROVE, text)
    if dp is None:
        dp = first(DISAPPROVE, text)
    if ap is None or not 15 <= ap <= 70:
        return None
    if dp is not None and (not 20 <= dp <= 80 or ap + dp > 102):
        dp = None
    end = it["pub"] - datetime.timedelta(days=5)
    return {"firm": firm, "end_date": end.isoformat(), "approve": ap, "disapprove": dp,
            "source": it["link"], "pub": it["pub"]}


def fmt(v):
    return "" if v is None else (str(int(v)) if float(v).is_integer() else str(v))


def main():
    dry = "--dry-run" in sys.argv
    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    table = alias_table({r["firm"] for r in rows})
    today = datetime.date.today()
    try:
        items = fetch_items()
    except Exception as e:
        print(f"  ! news fetch failed ({e}); CSV untouched.")
        return
    added = []
    for it in sorted(items, key=lambda x: x["pub"]):
        if (today - it["pub"]).days > LOOKBACK_DAYS:
            continue
        p = parse_item(it, table)
        if not p:
            continue
        t = datetime.date.fromisoformat(p["end_date"])
        dup = False
        for r in rows:
            if r["firm"] != p["firm"] or not r.get("approve"):
                continue
            if abs((datetime.date.fromisoformat(r["end_date"]) - t).days) <= MATCH_DAYS + 5 and \
               abs(float(r["approve"]) - p["approve"]) <= 1.5:
                dup = True
                if not r.get("disapprove") and p["disapprove"] is not None and r.get("origin") == "news":
                    r["disapprove"] = fmt(p["disapprove"])
                break
        if dup:
            continue
        row = {"firm": p["firm"], "end_date": p["end_date"], "approve": fmt(p["approve"]),
               "disapprove": fmt(p["disapprove"]), "n": "", "date_approx": "1",
               "source": p["source"], "origin": "news"}
        rows.append(row)
        added.append(row)
    if not added:
        print(f"  · {len(items)} news item(s) read, no new approval polls.")
        return
    for r in added:
        print(f"  + {r['firm']} ≈{r['end_date']}: {r['approve']}/{r['disapprove'] or '–'}")
    if dry:
        print("  (dry run — nothing written)")
        return
    rows.sort(key=lambda r: (r["firm"], r["end_date"]))
    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) or "" for k in FIELDS})
    print(f"  wrote {CSV_PATH.name} (+{len(added)})")


if __name__ == "__main__":
    main()
