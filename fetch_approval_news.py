#!/usr/bin/env python3
"""
fetch_approval_news.py — daily harvest of new Milei approval polls from the press.

EncuestAR only relays a handful of approval polls, so on its own the approval average would
stop getting new information. This script searches the press every day (Bing News and Google
News, last three weeks), downloads each article and reads its body text, not just the headline:
headlines rarely carry both the pollster and the figures. In every sentence about approval of the
national government's gestión it reads the approval share (and the disapproval share when given),
credits it to the pollster named closest before it, and appends new polls to approval_polls.csv
with origin=news. build_approval.py then rolls them into the average.

Safeguards
  * Firm must resolve through pollster_aliases.csv / known spellings / the names already in the CSV.
  * The sentence must speak of approval or disapproval of the gestión or the government, not of
    image (imagen), a governor or mayor, or a subgroup of voters ("entre los jóvenes"...).
  * 15 <= approve <= 70, 20 <= disapprove <= 80, approve + disapprove <= 102.
  * Fieldwork end is read when the article states it ("entre el 12 y el 17 de octubre"); otherwise
    publication − 5 days, flagged date_approx=1. When EncuestAR (fetch_approval.py) later carries
    the same poll its exact date replaces this one. Hand-entered rows are never touched.
  * The same poll reported by several outlets is merged (same firm, ±20 days, ±1.5 pts).
  * At most 40 articles and 8 minutes per run; anything missed is retried the next day.

Usage:
    python fetch_approval_news.py            # merge into approval_polls.csv
    python fetch_approval_news.py --dry-run  # show what would be added
"""

import csv, re, sys, json, html, datetime, pathlib, unicodedata
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "approval_polls.csv"
ALIASES = HERE / "pollster_aliases.csv"
FIELDS = ["firm", "end_date", "approve", "disapprove", "n", "date_approx", "source", "origin"]
MATCH_DAYS, LOOKBACK_DAYS = 20, 21
MAX_ARTICLES, TIME_BUDGET = 40, 8 * 60

QUERIES = [
    "aprobación gestión Milei encuesta",
    "desaprueba gestión Milei encuesta",
    "desaprobación Milei encuesta nacional",
    "aprobación del gobierno Milei consultora",
    "encuesta nacional Milei gestión",
]
TITLE_OK = re.compile(r"aprob|aprueb|desaprueb|gesti[oó]n|encuesta|sondeo|rechazo", re.I)
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


VAGUE = re.compile(r"(?:menos|m[aá]s|cerca|debajo|encima|alrededor|casi|supera|superior|inferior)\s+(?:de|del|al|a)?\s*(?:el\s+)?$", re.I)


def first(pats, text):
    for p in pats:
        for m in p.finditer(text):
            if VAGUE.search(text[max(0, m.start(1) - 25):m.start(1)]):
                continue                      # "menos del 40%" is a threshold, not a reading
            return float(m.group(1).replace(",", "."))
    return None


def fetch_items():
    """Articles about approval polls: (title, url, pub), newest first, deduplicated."""
    import fetch_runoff_matchups as R
    from urllib.parse import quote_plus
    today = datetime.date.today()
    seen, cands = set(), []
    for tmpl in (R.BING, R.GNEWS):
        for q in QUERIES:
            try:
                items = list(R.rss(tmpl.format(q=quote_plus(q))))
            except Exception as e:
                print(f"  · feed failed ({q}): {e}")
                continue
            for title, link, pub in items:
                if (today - pub).days > LOOKBACK_DAYS or not TITLE_OK.search(title):
                    continue
                k = key(re.sub(r"\s+-\s+[^-]{2,60}$", "", title))[:80]
                if k in seen:
                    continue
                seen.add(k)
                cands.append((title, link, pub))
    cands.sort(key=lambda c: c[2], reverse=True)
    out, urls, decodes = [], set(), 0
    for title, link, pub in cands:
        if len(out) >= MAX_ARTICLES:
            break
        if "news.google.com" in link:
            if decodes >= R.MAX_GOOGLE_DECODES:
                continue
            decodes += 1
        url = R.resolve(link)
        if url and url not in urls:
            urls.add(url)
            out.append({"title": html.unescape(title), "link": url, "pub": pub})
    return out


SENT_OK = re.compile(r"aprob|aprueb|desaprueb|valoraci[oó]n (?:positiva|negativa)|evaluaci[oó]n (?:positiva|negativa)", re.I)
SENT_OFF = re.compile(r"\bimagen\b(?!.{0,40}gesti[oó]n)|gobernador|intendente|jefe de gobierno porte|"
                      r"provincia de buenos aires|bonaerense|municipio|entre (?:los|las|quienes)|votantes de|"
                      r"j[oó]venes|mujeres|varones|congreso|ley\b|proyecto", re.I)
APPROVE.append(re.compile(r"valoraci[oó]n positiva[^.%]{0,30}?" + PCT, re.I))
DISAPPROVE.append(re.compile(r"valoraci[oó]n negativa[^.%]{0,30}?" + PCT, re.I))
DISAPPROVE.append(re.compile(r"(?:evaluaciones|valoraciones|opiniones|calificaciones) negativas[^.%]{0,30}?" + PCT, re.I))


TITLE_OFF = re.compile(r"mujeres|j[oó]venes|bonaerense|provincia|gobernador|intendente|c[oó]rdoba(?! *&)|"
                       r"santa fe|mendoza|tucum[aá]n|salta|ciudad de buenos aires|porte[nñ]o|kicillof|llaryora|pullaro", re.I)
PLAUSIBLE = 12      # points from the current average beyond which a reading is treated as a misread


def current_average():
    try:
        L = json.loads((HERE / "approval_history.json").read_text(encoding="utf-8"))["latest"]
        return L.get("approve"), L.get("disapprove")
    except Exception:
        return None, None


def parse_article(art, table):
    """Approval polls in one article: [{firm, end_date, approve, disapprove, source, pub, approx}].
    Strict on purpose (a wrong poll does more harm than a missed one): the article must be about the
    national government, the sentence must give BOTH approval and disapproval of the gestión
    (adding to 85–102), both must sit within 12 points of the current average, and the pollster must
    be named in that sentence or just before it."""
    import fetch_runoff_matchups as R
    if TITLE_OFF.search(art["title"]):
        return []
    try:
        text = art["title"] + "\n" + R.article_text(art["link"])
    except Exception:
        return []
    a0, d0 = current_average()
    pats = R.firm_patterns() + [(re.compile(r"(?<![a-z])" + re.escape(a) + r"(?![a-z])"), c)
                                 for a, c in table if len(a) >= 4]
    f = R.fold(text)
    sents = [(m.start(), m.group(0)) for m in re.finditer(r"[^.\n]+(?:\.(?!\d)|\n|$)", text)]
    out, firms_done = [], set()
    for k, (pos, sent) in enumerate(sents):
        if not SENT_OK.search(sent) or SENT_OFF.search(sent):
            continue
        ap, dp = first(APPROVE, sent), first(DISAPPROVE, sent)
        if ap is not None and dp is None and k + 1 < len(sents) and not SENT_OFF.search(sents[k + 1][1]):
            dp = first(DISAPPROVE, sents[k + 1][1])
        if ap is None or dp is None or not 85 <= ap + dp <= 102:
            continue
        if a0 is not None and (abs(ap - a0) > PLAUSIBLE or abs(dp - d0) > PLAUSIBLE):
            continue
        lo = max(0, pos - 400)
        firm = R.firm_near(f[lo:pos + len(sent)], pos - lo + len(sent), pats) if any(
            rx.search(f[lo:pos + len(sent)]) for rx, _ in pats) else None
        if not firm or firm in firms_done:
            continue
        firms_done.add(firm)
        end, approx = R.fieldwork_end(text, art["pub"], pos)
        out.append({"firm": firm, "end_date": end.isoformat(), "approve": ap, "disapprove": dp,
                    "source": art["link"], "pub": art["pub"], "approx": approx})
    return out


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
    import time
    t0 = time.time()
    polls = []
    for it in sorted(items, key=lambda x: x["pub"]):
        if time.time() - t0 > TIME_BUDGET:
            print("  · time budget reached; the rest will be read on the next run")
            break
        polls.extend(parse_article(it, table))
    added = []
    for p in polls:
        t = datetime.date.fromisoformat(p["end_date"])
        dup = False
        for r in rows:
            if r["firm"] != p["firm"] or not r.get("approve"):
                continue
            gap = abs((datetime.date.fromisoformat(r["end_date"]) - t).days)
            if gap <= 12 or (gap <= MATCH_DAYS + 5 and abs(float(r["approve"]) - p["approve"]) <= 1.5):
                dup = True
                if not r.get("disapprove") and p["disapprove"] is not None and r.get("origin") == "news":
                    r["disapprove"] = fmt(p["disapprove"])
                break
        if dup:
            continue
        row = {"firm": p["firm"], "end_date": p["end_date"], "approve": fmt(p["approve"]),
               "disapprove": fmt(p["disapprove"]), "n": "", "date_approx": "1" if p["approx"] else "0",
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
