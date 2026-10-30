#!/usr/bin/env python3
"""
fetch_runoff_matchups.py — daily harvest of runoff (balotaje) polls that pit Milei against each
possible rival, from the press. Appends new ones to runoff_matchups.csv, which forecast_sim.py uses
to judge how Milei would fare if the Peronist nominee is not Kicillof.

How it works
  1. Searches Bing News RSS (direct article links) and Google News RSS (links decoded when the
     googlenewsdecoder package is installed) for the last three weeks of runoff-poll coverage.
  2. Downloads each article and keeps its body text.
  3. Looks only at the passages that follow a runoff word (balotaje, ballotage, segunda vuelta,
     mano a mano) and reads each percentage with the candidate it belongs to. Newsrooms rarely
     repeat names, so candidates are also recognised by how articles refer to them ("el Presidente",
     "el gobernador bonaerense", "el líder del Frente Renovador"...). A percentage belongs to the
     candidate introduced right after it ("el 40% del mandatario", "el 41% se inclina por el
     Presidente"), otherwise to the closest candidate before it ("Kicillof: 42%"); in "Kicillof le
     gana a Milei por 46% a 37%" the first number goes to the subject.
  4. Consecutive percentages for Milei and one rival form a matchup. Each share must be 15–70 and
     the pair must add to 55–102 (with or without undecided voters).
  5. The pollster must be named in the article (pollster_aliases.csv plus known spellings); in
     articles that review several polls each matchup takes the pollster named closest before it.
  6. The fieldwork end date is read when the article gives it ("entre el 21 y el 24 de agosto");
     otherwise publication date − 5 days, flagged date_approx=1.
  7. Duplicates are dropped: same pollster and rival, within 20 days, Milei's two-way share within
     1.5 points (this also folds "total" and "valid votes only" versions of the same poll).
  Hand-entered rows (origin=manual) are never changed.

Usage:
    python fetch_runoff_matchups.py            # merge into runoff_matchups.csv
    python fetch_runoff_matchups.py --dry-run  # show what would be added
    python fetch_runoff_matchups.py --test     # run the parser on built-in examples
"""

import csv, re, sys, html, datetime, pathlib, unicodedata
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse, parse_qs
from xml.etree import ElementTree as ET

HERE = pathlib.Path(__file__).parent
CSV_PATH = HERE / "runoff_matchups.csv"
FIELDS = ["end_date", "firm", "opponent", "milei", "opponent_share", "date_approx", "origin", "source"]
LOOKBACK_DAYS, MATCH_DAYS = 21, 20
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 (arg2027-monitor)"}

QUERIES = [
    "encuesta balotaje Milei Kicillof",
    "encuesta balotaje Milei Massa",
    "encuesta ballotage Milei",
    "encuesta segunda vuelta Milei 2027",
    "escenarios de balotaje Milei encuesta",
]
BING = "https://www.bing.com/news/search?q={q}&format=rss&setlang=es&cc=AR"
GNEWS = "https://news.google.com/rss/search?q={q}+when:21d&hl=es-419&gl=AR&ceid=AR:es-419"

# how articles refer to each candidate (folded: lower case, no accents)
CANDIDATES = {
    "Milei": [r"javier milei", r"milei", r"(?<!vice)(?<!ex )(?<!ex-)presidente(?! del| de la)", r"mandatario nacional",
              r"jefe de estado", r"libertario", r"lider de (?:la libertad avanza|lla)"],
    "Kicillof": [r"axel kicillof", r"kicillof", r"gobernador bonaerense", r"gobernador de (?:la provincia de )?buenos aires",
                 r"mandatario bonaerense", r"lider (?:de|del) (?:movimiento )?derecho al futuro"],
    "Massa": [r"sergio massa", r"massa", r"lider del frente renovador", r"ex ?ministro de economia", r"tigrense"],
    "Cristina Fernández de Kirchner": [r"cristina fernandez(?: de kirchner)?", r"cristina kirchner", r"cristina", r"\bcfk",
                                       r"ex ?presidenta"],
    "Victoria Villarruel": [r"victoria villarruel", r"villarruel", r"vicepresidenta"],
    "Mauricio Macri": [r"mauricio macri", r"(?<!jorge )macri"],
    "Patricia Bullrich": [r"patricia bullrich", r"bullrich"],
    "Juan Schiaretti": [r"juan schiaretti", r"schiaretti"],
    "Myriam Bregman": [r"myriam bregman", r"bregman"],
    "Juan Grabois": [r"juan grabois", r"grabois"],
}
RUNOFF_WORDS = r"balotaje|ballotage|segunda vuelta|mano a mano"
OTHER_QUESTIONS = (r"primera vuelta|imagen|positiva|negativa|aprob|desaprob|por espacios|"
                   r"votaria en blanco|no sabe|considera|responsabiliza|confia|dentro de ese|entre (?:los|las|quienes)|"
                   r"desencantad|segmento|votantes de|electores de|jovenes|mujeres|varones|cree que")
PCT = r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:%|por ciento)"
CONNECT = r"(?:de|del|para|por|a|al|en favor de|a favor de)\s+(?:la |el |los |las )?"
MONTHS = {m: i + 1 for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
                                          "agosto", "septiembre", "octubre", "noviembre", "diciembre"])}
MONTHS["setiembre"] = 9

FIRM_EXTRA = {
    "atlas intel": "AtlasIntel", "atlasintel": "AtlasIntel", "universidad de san andres": "ESPOP (UdeSA)",
    "udesa": "ESPOP (UdeSA)", "poliarquia": "Poliarquía", "zuban cordoba": "Zuban Córdoba",
    "management & fit": "Management & Fit", "management fit": "Management & Fit", "qsocial": "QSocial",
    "synopsis": "Synopsis", "delfos": "Delfos", "equipo mide": "Equipo Mide", "opina argentina": "Opina Argentina",
    "opinaia": "Opinaia", "trends": "Trends", "cb consultora": "CB Consultora", "proyeccion consultores": "Proyección Consultores",
    "proyeccion": "Proyección Consultores", "analogias": "Analogías", "giacobbe": "Giacobbe & Asociados",
    "isasi-burdman": "Isasi-Burdman", "isasi burdman": "Isasi-Burdman", "consultora tendencias": "Consultora Tendencias",
    "tendencias": "Consultora Tendencias", "dc consultores": "DC Consultores", "zx consultora": "ZX Consultora",
    "aresco": "Aresco", "federico gonzalez": "Federico González", "circuitos": "Circuitos", "real time data": "Real Time Data",
    "taquion": "Taquión", "afiches": "Afiches", "hugo haime": "Hugo Haime & Asociados", "haime": "Hugo Haime & Asociados",
    "opinargentina": "Opinargentina", "rubikon": "Rubikon Intel", "explicans": "Explicans", "nueva comunicacion": "Nueva Comunicación",
    "ceop": "CEOP", "celag": "CELAG", "rouvier": "Ricardo Rouvier & Asociados", "aragon": "Raúl Aragón & Asociados",
    "quorus": "Quorus", "trespuntozero": "Trespuntozero", "politicos en redes": "Políticos en Redes",
    "observatorio de psicologia social": "Observatorio de Psicología Social Aplicada (UBA)", "fixer": "Fixer",
    "inteligencia analitica": "Inteligencia Analítica", "dalessio": "D'Alessio IROL y Berensztein",
    "d'alessio": "D'Alessio IROL y Berensztein", "berensztein": "D'Alessio IROL y Berensztein",
}


def fold(s):
    """Lower case without accents, same length as s (so positions line up)."""
    return "".join(unicodedata.normalize("NFKD", c)[0].lower() if c.strip() else c for c in s)


def firm_patterns():
    table = dict(FIRM_EXTRA)
    try:
        with open(HERE / "pollster_aliases.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("alias") and r.get("canonical"):
                    table[fold(r["alias"]).strip()] = r["canonical"]
    except OSError:
        pass
    pats = []
    for alias, canon in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if len(alias) >= 4:
            pats.append((re.compile(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])"), canon))
    return pats


CAND_RE = [(name, re.compile(r"(?<![a-z])(?:" + "|".join(p) + r")(?![a-z])")) for name, p in CANDIDATES.items()]


def mentions(f):
    out = []
    for name, rx in CAND_RE:
        for m in rx.finditer(f):
            out.append((m.start(), m.end(), name))
    out.sort()
    # drop mentions nested inside a longer one ("javier milei" vs "milei")
    keep = []
    for m in out:
        if keep and m[0] < keep[-1][1]:
            continue
        keep.append(m)
    return keep


def sentence_bounds(f, i):
    left = max(f.rfind(". ", 0, i), f.rfind("\n", 0, i), f.rfind(";", 0, i))
    rights = [x for x in (f.find(". ", i), f.find("\n", i), f.find(";", i)) if x != -1]
    return left + 1, (min(rights) if rights else len(f))


def parse_text(text):
    """All (candidate, rival, milei_share, rival_share, position) matchups in an article."""
    f = fold(text)
    ms = mentions(f)
    pcts = [(m.start(), m.end(), float(m.group(1).replace(",", "."))) for m in re.finditer(PCT, f)]
    runoff_at = [m.start() for m in re.finditer(RUNOFF_WORDS, f)]
    assigned = []
    for k, (s, e, v) in enumerate(pcts):
        s0, s1 = sentence_bounds(f, s)
        sent = f[s0:s1]
        if re.search(OTHER_QUESTIONS, sent) and not re.search(RUNOFF_WORDS, sent):
            continue          # first round, image, vote by party...: not a runoff number
        last = max([a for a in runoff_at if a <= s], default=None)
        if last is None or s - last > 350 or re.search(OTHER_QUESTIONS, f[last:s0]):
            continue          # must sit in a passage introduced by a runoff word
        nxt = pcts[k + 1][0] if k + 1 < len(pcts) else len(f)
        prv = pcts[k - 1][1] if k > 0 else 0
        who = None
        # "46% a 37%": first number to the sentence's first candidate, second to the other one
        m_ab = re.match(r"\s*(?:a|contra)\s+(?:el\s+)?" + PCT, f[e:])
        if m_ab:
            sent = [m for m in ms if s0 <= m[0] < s1]
            names = []
            for m in sent:
                if m[2] not in names:
                    names.append(m[2])
            if len(names) == 2:
                who = names[0]
                assigned.append((s, v, who))
                assigned.append((e + m_ab.start(1), float(m_ab.group(1).replace(",", ".")), names[1]))
                continue
        if assigned and assigned[-1][0] == s:
            continue
        # candidate introduced right after the number, before the next number / end of sentence
        right_end = min(nxt, s1, e + 80)
        for m in ms:
            if e <= m[0] < right_end and re.search(r"(?:^|\s)" + CONNECT + r"$", f[e:m[0]]):
                who = m[2]
                break
        if who is None:   # otherwise the closest candidate before it, after the previous number
            before = [m for m in ms if max(prv, s0 - 1) <= m[0] < s]
            if before:
                who = before[-1][2]
        if who:
            assigned.append((s, v, who))
    assigned.sort()
    pairs, i = [], 0
    while i + 1 < len(assigned):
        (p1, v1, w1), (p2, v2, w2) = assigned[i], assigned[i + 1]
        if w1 != w2 and "Milei" in (w1, w2) and p2 - p1 < 400:
            mil, riv, rname = (v1, v2, w2) if w1 == "Milei" else (v2, v1, w1)
            if 15 <= mil <= 70 and 15 <= riv <= 70 and 68 <= mil + riv <= 102:
                pairs.append((rname, mil, riv, p1))
                i += 2
                continue
        i += 1
    return pairs


def fieldwork_end(text, pub, pos=None):
    """Fieldwork end date: the date range stated closest before `pos` (or anywhere if pos is None)."""
    f = fold(text)
    best = None
    rx = r"(?:entre el|del|desde el)\s+\d{1,2}(?:\s+de\s+[a-z]+)?\s+(?:y el|al|hasta el)\s+(\d{1,2})\s+de\s+([a-z]+)"
    for m in re.finditer(rx, f):
        if m.group(2) not in MONTHS:
            continue
        if pos is None or m.start() <= pos or best is None:
            best = m
        if pos is not None and m.start() > pos:
            break
    if best:
        mon, day = MONTHS[best.group(2)], int(best.group(1))
        year = pub.year if mon <= pub.month else pub.year - 1
        try:
            d = datetime.date(year, mon, day)
            if 0 <= (pub - d).days <= 60:
                return d, False
        except ValueError:
            pass
    return pub - datetime.timedelta(days=5), True


def firm_near(f, pos, pats):
    best = None
    for rx, canon in pats:
        for m in rx.finditer(f):
            if m.start() <= pos and (best is None or m.start() > best[0]):
                best = (m.start(), canon)
    if best is None:   # named only after the numbers
        for rx, canon in pats:
            m = rx.search(f)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), canon)
    return best[1] if best else None


# ---------- fetching ----------

def rss(url):
    import requests
    r = requests.get(url, timeout=12, headers=UA)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    for it in root.iter("item"):
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).date()
        except Exception:
            continue
        yield html.unescape(it.findtext("title") or ""), (it.findtext("link") or "").strip(), pub


def resolve(link):
    if "bing.com/news/apiclick" in link:
        q = parse_qs(urlparse(link).query)
        return q.get("url", [None])[0]
    if "news.google.com" in link:
        try:
            from googlenewsdecoder import gnewsdecoder
            d = gnewsdecoder(link)
            return d.get("decoded_url") if d.get("status") else None
        except Exception:
            return None
    return link


def article_text(url):
    import requests
    from bs4 import BeautifulSoup
    r = requests.get(url, timeout=12, headers=UA)
    r.raise_for_status()
    soup = BeautifulSoup(r.content, "lxml")
    for t in soup(["script", "style", "nav", "footer", "aside", "form"]):
        t.decompose()
    root = soup.find("article") or soup.body or soup
    parts = [el.get_text(" ", strip=True) for el in root.find_all(["h1", "h2", "h3", "p", "li"])]
    return "\n".join(p for p in parts if p)


RELEVANT = re.compile(r"balotaje|ballotage|segunda vuelta|mano a mano|escenario", re.I)
MAX_ARTICLES = 40          # per run; the 21-day lookback means anything missed today is retried tomorrow
MAX_GOOGLE_DECODES = 15    # decoding a Google News link takes a request of its own
TIME_BUDGET = 8 * 60       # seconds for the whole run


def harvest(today):
    """Candidate articles: titles must mention a runoff; Bing first (direct links), Google News only
    for stories Bing did not have, newest first."""
    seen_titles, cands = set(), []
    for tmpl in (BING, GNEWS):
        for q in QUERIES:
            try:
                items = list(rss(tmpl.format(q=quote_plus(q))))
            except Exception as e:
                print(f"  · feed failed ({tmpl[:30]}…, {q}): {e}")
                continue
            for title, link, pub in items:
                if (today - pub).days > LOOKBACK_DAYS or not RELEVANT.search(title):
                    continue
                k = re.sub(r"\W+", " ", fold(re.sub(r"\s+-\s+[^-]{2,60}$", "", title))).strip()[:80]
                if k in seen_titles:
                    continue
                seen_titles.add(k)
                cands.append((title, link, pub))
    cands.sort(key=lambda c: c[2], reverse=True)
    out, seen_urls, decodes = [], set(), 0
    for title, link, pub in cands:
        if len(out) >= MAX_ARTICLES:
            break
        if "news.google.com" in link:
            if decodes >= MAX_GOOGLE_DECODES:
                continue
            decodes += 1
        url = resolve(link)
        if url and url not in seen_urls:
            seen_urls.add(url)
            out.append((title, url, pub))
    return out


def fmt(v):
    return str(int(v)) if float(v).is_integer() else str(v)


def two_way(m, o):
    return 100 * float(m) / (float(m) + float(o))


def main():
    if "--test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    dry = "--dry-run" in sys.argv
    with open(CSV_PATH, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r.setdefault("date_approx", "0")
        r["date_approx"] = r.get("date_approx") or "0"
        r["origin"] = r.get("origin") or "manual"
    pats = firm_patterns()
    today = datetime.date.today()
    try:
        arts = harvest(today)
    except Exception as e:
        print(f"  ! news search failed ({e}); CSV untouched.")
        return
    print(f"  · {len(arts)} articles to read")
    added, found, read = [], [], 0
    import time
    t0 = time.time()
    for title, url, pub in sorted(arts, key=lambda a: a[2]):
        if time.time() - t0 > TIME_BUDGET:
            print("  · time budget reached; the rest will be read on the next run")
            break
        try:
            text = title + "\n" + article_text(url)
        except Exception:
            continue
        read += 1
        pairs = parse_text(text)
        if not pairs:
            continue
        f = fold(text)
        for rival, mil, riv, pos in pairs:
            end, approx = fieldwork_end(text, pub, pos)
            firm = firm_near(f, pos, pats)
            if firm:
                found.append({"end_date": end.isoformat(), "firm": firm, "opponent": rival, "milei": fmt(mil),
                              "opponent_share": fmt(riv), "date_approx": "1" if approx else "0",
                              "origin": "news", "source": url})
    # one reading per poll: group what the press reported for the same pollster and rival within
    # MATCH_DAYS; keep the figure most outlets agree on (ties: the one with fewer undecided, i.e.
    # the full-sample number rather than a subgroup), and nothing if the poll is already stored.
    def same(a, b):
        return a["firm"] == b["firm"] and a["opponent"] == b["opponent"] and \
            abs((datetime.date.fromisoformat(a["end_date"]) - datetime.date.fromisoformat(b["end_date"])).days) <= MATCH_DAYS
    groups = []
    for r in found:
        for g in groups:
            if same(g[0], r):
                g.append(r)
                break
        else:
            groups.append([r])
    for g in groups:
        if any(same(r, g[0]) for r in rows):
            continue
        def support(c):
            return sum(abs(two_way(c["milei"], c["opponent_share"]) - two_way(o["milei"], o["opponent_share"])) <= 1.5
                       for o in g)
        best = max(g, key=lambda c: (support(c), float(c["milei"]) + float(c["opponent_share"])))
        if len(g) > 1:
            best = dict(best, date_approx=min((o["date_approx"] for o in g), key=int))
        rows.append(best)
        added.append(best)
    print(f"  · {read} articles read, {len(added)} new matchups")
    for r in added:
        print(f"    + {r['end_date']} {r['firm']}: Milei {r['milei']} – {r['opponent']} {r['opponent_share']}  ({r['source'][:80]})")
    if dry or not added:
        return
    rows.sort(key=lambda r: (r["end_date"], r["firm"], r["opponent"]))
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ---------- examples taken from real coverage ----------

EXAMPLES = [
    ("En un eventual balotaje, el gobernador bonaerense obtuvo el 42% frente al 40% del mandatario nacional. "
     "Sergio Massa alcanzó el 44% de intención de voto contra el 38% del Presidente.",
     [("Kicillof", 40, 42), ("Massa", 38, 44)]),
    ("Balotaje entre Kicillof y Milei\n1° Axel Kicillof: 42%.\n2° Javier Milei: 41%.\n"
     "Balotaje entre Milei y Massa\n1° Javier Milei: 42%.\n2° Sergio Massa: 38%.",
     [("Kicillof", 41, 42), ("Massa", 42, 38)]),
    ("En una hipotética segunda vuelta entre Milei y Kicillof, el 41% de las personas encuestadas se inclina por el "
     "Presidente, mientras que el 39,1% elige al líder de Movimiento Derecho al Futuro.",
     [("Kicillof", 41, 39.1)]),
    ("En un escenario de ballotage, el gobernador bonaerense obtendría 46,1% frente al 37,4% del jefe de Estado.",
     [("Kicillof", 37.4, 46.1)]),
    ("En el balotaje, Kicillof le gana a Milei por 45,4% a 42%.", [("Kicillof", 42, 45.4)]),
    ("Mano a mano: Milei 44% vs. Massa 39%; Milei 42% vs. Kicillof 40%.", [("Massa", 44, 39), ("Kicillof", 42, 40)]),
    ("En primera vuelta Milei reúne el 37% frente al 31,6% del peronismo.", []),
    ("En una segunda vuelta, tanto Kicillof como Massa se imponen sobre Milei. Kicillof llega a 45,4% contra 42% de "
     "Milei, con 11% de voto en blanco y 1,6% de indecisos, mientras que Massa obtiene 44,4% contra 42,2% del "
     "Presidente. Dentro de ese universo, el comportamiento de los desencantados vuelve a ser determinante: 45% "
     "votaría a Kicillof y 30% volvería a Milei.",
     [("Kicillof", 42, 45.4), ("Massa", 42.2, 44.4)]),
    ("Proyección le da una ventaja mínima al Presidente en segunda vuelta. El relevamiento muestra a Milei adelante "
     "en primera vuelta con el 37% frente al 31,6% de Kicillof.\nEn un balotaje, la diferencia se achica a menos de "
     "dos puntos: Milei obtendría el 41% y Kicillof el 39,1%. El 13,4% votaría en blanco o impugnaría y el 6,5% no "
     "sabe qué haría.",
     [("Kicillof", 41, 39.1)]),
]


def self_test():
    ok = True
    for text, want in EXAMPLES:
        got = [(r, m, o) for r, m, o, _ in parse_text(text)]
        flag = "ok " if got == want else "BAD"
        ok &= got == want
        print(f"  {flag} {got}  <- {text[:70]!r}")
    print("all examples parsed correctly" if ok else "SOME EXAMPLES FAILED")
    return ok


if __name__ == "__main__":
    main()
