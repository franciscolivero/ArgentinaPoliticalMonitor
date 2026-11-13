#!/usr/bin/env python3
"""
latam_sources.py — primary-source adapters for the Latin America approval tracker.

Google News only sees what the press headlines. These adapters go to the sources themselves:

  * Wikipedia "Opinion polling on the … presidency" tables (MediaWiki API, parsed HTML).
      Mexico (Sheinbaum), El Salvador (Bukele).
  * Pollsters that publish every wave as a PDF on a WordPress site, listed through the public
    WordPress REST API (wp-json/wp/v2/media) and read with pypdf.
      Chile: Cadem Plaza Pública (weekly), Criteria Agenda (weekly).

Every adapter returns poll dicts {date, firm, approve, disapprove, approx, source, origin} and is
fail-safe: a page or PDF that cannot be read or does not validate (approve + disapprove <= 101,
both within 3–97) is skipped and logged, never guessed. fetch_latam_approval.py calls run_all().

To add a pollster: add a WP_SOURCES entry (site, media search term, title filter, parser).
"""

import io, re, datetime

UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36 arg2027-monitor/1.0 (github.com; francisco olivero)"),
      "Accept-Language": "es-419,es;q=0.9,en;q=0.8"}
ES_MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
             "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12}
EN_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                              "sep", "oct", "nov", "dec"])}


def _get(url, binary=False, params=None):
    import requests
    r = requests.get(url, headers=UA, timeout=45, params=params)
    r.raise_for_status()
    return r.content if binary else r.text


def _ok(a, d):
    if a is None or not 3 <= a <= 97:
        return False
    return d is None or (3 <= d <= 97 and a + d <= 101.5)


def _num(s):
    s = (s or "").strip().replace("%", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------ Wikipedia
WIKI_PAGES = {
    "MX": ["Opinion_polling_on_the_Sheinbaum_presidency"],
    "SV": ["Opinion_polling_on_the_Nayib_Bukele_presidency"],
}
# Spanish Wikipedia pages with an approval table ("Encuestadora | Fecha | Aprueba | Desaprueba")
WIKI_PAGES_ES = {
    "UY": ["Gobierno_de_Yamandú_Orsi"],
    "CL": ["Anexo:Encuestas_de_aprobación_del_gobierno_de_José_Antonio_Kast"],
    "EC": ["Segundo_gobierno_de_Daniel_Noboa"],
    "GT": ["Bernardo_Arévalo"],
}
WIKI_FIRMS = [("cb consultora", "CB Consultora (imagen positiva)"), ("cid gallup", "CID Gallup"),
              ("atlas", "AtlasIntel"), ("cadem", "Cadem"), ("criteria", "Criteria"), ("cedatos", "Cedatos"),
              ("equipos", "Equipos Consultores"), ("opcion", "Opción Consultores"), ("pulso ciudadano", "Pulso Ciudadano"),
              ("panel ciudadano", "Panel Ciudadano UDD"), ("cep", "CEP"), ("maluk", "Maluk Research")]
ES_ABBR = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9,
           "oct": 10, "nov": 11, "dic": 12, "jan": 1, "apr": 4, "aug": 8, "dec": 12}
DAY_MON_YEAR = re.compile(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?\s*(\d{4})?")


def _wiki_html(page, lang="en"):
    import json
    raw = _get(f"https://{lang}.wikipedia.org/w/api.php",
               params={"action": "parse", "page": page, "prop": "text", "format": "json",
                       "formatversion": "2", "redirects": "1"})
    return json.loads(raw)["parse"]["text"]


def parse_wiki_tables(html_text, today=None):
    """Rows from every wikitable whose header has an Approve(s) column, in page order.
    Dates without a year are dated by walking down the page from the current year and stepping
    back a year whenever the month jumps forward (tables run newest first)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")
    today = today or datetime.date.today()
    out, year, prev_month = [], today.year, None
    for tbl in soup.select("table.wikitable"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        head = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]

        def col(*names):
            for i, h in enumerate(head):
                if any(h.startswith(n) for n in names):
                    return i
            return None
        ia, idis = col("approve"), col("disapprove")
        ifirm, idate = col("pollster", "polling firm"), col("date", "fieldwork")
        if None in (ia, ifirm, idate):
            continue
        for tr in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) != len(head):
                continue                                   # merged / footnote rows
            a = _num(re.sub(r"\[.*?\]", "", cells[ia]))
            d = _num(re.sub(r"\[.*?\]", "", cells[idis])) if idis is not None else None
            hits = DAY_MON_YEAR.findall(cells[idate])
            if not hits or not _ok(a, d):
                continue
            day, mon, yr = hits[-1]
            m = EN_MONTHS.get(mon.lower()[:3])
            if not m:
                continue
            if yr:
                year = int(yr)
            elif prev_month is not None and m > prev_month + 1:
                year -= 1
            prev_month = m
            try:
                dt = datetime.date(year, m, min(int(day), 28 if m == 2 else 30 if m in (4, 6, 9, 11) else 31))
            except ValueError:
                continue
            if dt > today:
                continue
            firm = re.sub(r"\[.*?\]", "", cells[ifirm]).strip()
            out.append({"date": dt.isoformat(), "firm": firm, "approve": a, "disapprove": d,
                        "approx": False, "origin": "wikipedia"})
    return out


def _grid(tbl):
    """Rows of a table with rowspan/colspan expanded: [(is_header, [cell texts])]."""
    out, carry = [], {}
    for tr in tbl.find_all("tr"):
        row, col = [], 0
        cells = tr.find_all(["th", "td"], recursive=False)
        hdr = bool(cells) and all(c.name == "th" for c in cells)
        it = iter(cells)
        while True:
            while col in carry:
                txt, left = carry[col]
                row.append(txt)
                carry[col] = (txt, left - 1)
                if left - 1 == 0:
                    del carry[col]
                col += 1
            c = next(it, None)
            if c is None:
                break
            txt = re.sub(r"\[.*?\]", "", c.get_text(" ", strip=True)).replace("\u200b", "").strip()
            span = int(c.get("colspan", 1) or 1)
            rs = int(c.get("rowspan", 1) or 1)
            for k in range(span):
                row.append(txt)
                if rs > 1:
                    carry[col] = (txt, rs - 1)
                col += 1
        out.append((hdr, row))
    return out


def _es_table_date(txt, today):
    t = _fold(txt)
    hits = re.findall(r"(\d{1,2})?\s*(?:de\s+)?([a-z]{3,10})\.?\s*(?:de\s+)?(\d{4})?", t)
    best = None
    for day, mon, yr in hits:
        m = ES_MONTHS.get(mon) or ES_ABBR.get(mon[:3]) if (mon in ES_MONTHS or mon[:3] in ES_ABBR) else None
        if not m:
            continue
        best = (int(day) if day else 15, m, int(yr) if yr else None)
    if not best:
        return None
    yrs = re.findall(r"(\d{4})", t)
    year = best[2] or (int(yrs[-1]) if yrs else today.year)
    d, m = best[0], best[1]
    try:
        return datetime.date(year, m, min(d, 28 if m == 2 else 30 if m in (4, 6, 9, 11) else 31))
    except ValueError:
        return None


def parse_wiki_es_tables(html_text, today=None):
    """Rows from Spanish wikitables with Encuestadora / Fecha / Aprueba / Desaprueba columns
    (single- or multi-row headers). 'Imagen' tables are skipped."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")
    today = today or datetime.date.today()
    out = []
    for tbl in soup.select("table.wikitable"):
        g = _grid(tbl)
        heads = [r for h, r in g if h]
        if not heads:
            continue
        width = max(len(r) for r in heads)
        labels = [" ".join(_fold(r[i]) for r in heads if i < len(r)) for i in range(width)]
        if any("imagen" in l for l in labels):
            continue

        def col(pat, avoid=None):
            for i, l in enumerate(labels):
                if re.search(pat, l) and not (avoid and re.search(avoid, l)):
                    return i
            return None
        ifirm = col(r"encuest|medio|empresa")
        idate = col(r"fecha")
        ia = col(r"\baprue|\baprob", r"desap")
        idis = col(r"desap")
        if None in (ifirm, idate, ia):
            continue
        for h, cells in g:
            if h or len(cells) <= max(i for i in (ifirm, idate, ia, idis) if i is not None):
                continue
            a = _num(cells[ia])
            d = _num(cells[idis]) if idis is not None else None
            dt = _es_table_date(cells[idate], today)
            if dt is None or dt > today or not _ok(a, d):
                continue
            out.append({"date": dt.isoformat(), "firm": cells[ifirm].strip(), "approve": a, "disapprove": d,
                        "approx": False, "origin": "wikipedia"})
    return out


def src_wikipedia(code, log):
    polls = []
    for page in WIKI_PAGES_ES.get(code, []):
        try:
            rows = parse_wiki_es_tables(_wiki_html(page, "es"))
        except Exception as e:
            log.append(f"  · {code} Wikipedia (es) {page}: {e}")
            continue
        for r in rows:
            r["source"] = "https://es.wikipedia.org/wiki/" + page
            f = _fold(r["firm"])
            for k, v in WIKI_FIRMS:
                if k in f:
                    r["firm"] = v
                    break
        polls.extend(rows)
        log.append(f"  · {code} Wikipedia (es) {page}: {len(rows)} rows")
    for page in WIKI_PAGES.get(code, []):
        try:
            rows = parse_wiki_tables(_wiki_html(page))
        except Exception as e:
            log.append(f"  · {code} Wikipedia {page}: {e}")
            continue
        for r in rows:
            r["source"] = "https://en.wikipedia.org/wiki/" + page
        polls.extend(rows)
        log.append(f"  · {code} Wikipedia {page}: {len(rows)} rows")
    return polls


# ------------------------------------------------------------------------------ WordPress PDFs
def _es_date(text):
    """Last 'DD de <mes> de YYYY' (or 'DD de <mes>' + a year nearby) in text."""
    m = list(re.finditer(r"(\d{1,2})\s+de\s+(" + "|".join(ES_MONTHS) + r")(?:\s+(?:de|del)\s+(\d{4}))?",
                         text, re.I))
    for x in reversed(m):
        if x.group(3):
            try:
                return datetime.date(int(x.group(3)), ES_MONTHS[x.group(2).lower()], int(x.group(1)))
            except ValueError:
                continue
    return None


def _pdf_pages(blob, n=12):
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(blob))
    return [(p.extract_text() or "") for p in rd.pages[:n]]


def parse_cadem(pages):
    """Plaza Pública: the approval page prints the current wave as '30% 65% 5% Aprueba Desaprueba'."""
    date = None
    for t in pages[:2]:
        m = re.search(r"Fecha de terreno:?(.{0,80})", t, re.I | re.S)
        if m:
            date = _es_date(m.group(1))
            if date:
                break
    for t in pages:
        if not re.search(r"aprueba o desaprueba la forma", t, re.I):
            continue
        after = t[t.lower().find("aprueba o desaprueba"):]
        nums = re.findall(r"(\d{1,2})\s*%", after[after.find("%") + 1:])[:3]
        if len(nums) >= 2:
            a, d = float(nums[0]), float(nums[1])
            nr = float(nums[2]) if len(nums) > 2 else 100 - a - d
            if _ok(a, d) and abs(a + d + nr - 100) <= 2:
                return a, d, date
    return None


SENT_A = re.compile(r"aprobaci[oó]n (?:del? (?:Presidente|mandatario)|presidencial)[^.%]{0,140}?"
                    r"(?:llegando a|llega a|alcanza(?:ndo)?|se ubica en|queda en|sube a|baja a|cae a|"
                    r"anota|marca|registra|con un|en un|a un|al?)\s*(\d{1,2})\s*%", re.I)
SENT_D = re.compile(r"desaprobaci[oó]n[^.%]{0,80}?(?:llegando a|llega a|alcanza|se ubica en|queda en|"
                    r"sube a|baja a|cae a|anota|marca|registra|con un|en un|a un|al?)\s*(\d{1,2})\s*%", re.I)


def parse_synthesis(pages):
    """Pollsters that write the result in prose (Criteria): 'La aprobación del Presidente … 28%'."""
    text = re.sub(r"\s+", " ", " ".join(pages))
    a, d = SENT_A.search(text), SENT_D.search(text)
    date = None
    m = re.search(r"Fecha de (?:campo|terreno):?(.{0,90})", text, re.I)
    if m:
        date = _es_date(m.group(1))
    if a:
        av = float(a.group(1))
        dv = float(d.group(1)) if d else None
        if _ok(av, dv):
            return av, dv, date
    return None


# code, firm, site API root, media search term, title regex, parser
WP_SOURCES = [
    ("CL", "Cadem", "https://cadem.cl/wp-json/wp/v2/media", "Track-PP", r"^Track PP \d+", parse_cadem),
    ("CL", "Criteria", "https://www.criteria.cl/wp-json/wp/v2/media", "Agenda_Criteria",
     r"^Agenda[_ ]Criteria", parse_synthesis),
]


def src_wordpress(code, since, have_sources, log, max_new=120):
    """New PDFs since `since` that are not already stored (by source URL)."""
    polls = []
    for c, firm, api, term, title_re, parser in WP_SOURCES:
        if c != code:
            continue
        items, page = [], 1
        while page <= 5:
            try:
                import json
                raw = _get(api, params={"search": term, "per_page": 100, "page": page,
                                        "mime_type": "application/pdf", "after": since + "T00:00:00"})
                batch = json.loads(raw)
            except Exception as e:
                if page == 1:
                    log.append(f"  · {code} {firm}: listing failed ({e})")
                break
            if not batch:
                break
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        items = [i for i in items if re.search(title_re, (i.get("title") or {}).get("rendered", ""), re.I)
                 and i.get("source_url") not in have_sources]
        items.sort(key=lambda i: i.get("date", ""))
        n_ok = 0
        for it in items[-max_new:]:
            url = it["source_url"]
            try:
                res = parser(_pdf_pages(_get(url, binary=True)))
            except Exception as e:
                log.append(f"  · {code} {firm}: unreadable {url.rsplit('/', 1)[-1]} ({e})")
                continue
            if not res:
                log.append(f"  · {code} {firm}: no validated figure in {url.rsplit('/', 1)[-1]}")
                continue
            a, d, date = res
            pub = datetime.date.fromisoformat(it["date"][:10])
            approx = date is None
            date = date or (pub - datetime.timedelta(days=1))
            if date.isoformat() < since:
                continue
            polls.append({"date": date.isoformat(), "firm": firm, "approve": a, "disapprove": d,
                          "approx": approx, "source": url, "origin": "pollster"})
            n_ok += 1
        log.append(f"  · {code} {firm}: {n_ok} new wave(s) from {len(items)} unread PDF(s)")
    return polls


def _safe(fn, label, log, *a):
    try:
        return fn(*a)
    except Exception as e:                     # one broken source must never stop the run
        log.append(f"  ! {label} crashed: {type(e).__name__}: {e}")
        return []


def run_all(code, since, have_sources, log):
    return (_safe(src_wikipedia, f"{code} Wikipedia", log, code, log) +
            _safe(src_wordpress, f"{code} WordPress", log, code, since, have_sources, log))


# ------------------------------------------------------------------ regional, multi-country sources
# These read one page that covers several presidents and return [(code, poll)].
# `names` maps country code -> list of folded name tokens (from PRESIDENTS); `stoppers` are other
# leaders' names that end a name's window so their figure is never credited to a tracked president.

def _fold(s):
    import unicodedata
    return "".join((unicodedata.normalize("NFKD", ch)[0] if ch.strip() else " ").lower() for ch in s)


def _name_hits(text, names, stoppers):
    f = _fold(text)
    hits = []
    for code, toks in names.items():
        for t in toks:
            for m in re.finditer(r"\b" + re.escape(_fold(t)) + r"\b", f):
                hits.append((m.start(), m.end(), code))
    for t in stoppers:
        for m in re.finditer(r"\b" + re.escape(t) + r"\b", f):
            hits.append((m.start(), m.end(), None))
    return sorted(hits)


# a figure right after a name: "con 72", "con 59 por ciento", "coinciden en 36 por ciento", "(68,3%)"
FIG = re.compile(r"(?:\bcon\b|\ben\b|\bde\b|\(|:)?\s*(\d{1,2}(?:[.,]\d)?)\s*(%|por ciento)?(?!\s*(?:puntos|pts|lugar|º|°))",
                 re.I)


def credit_figures(text, names, stoppers, win=60, need_pct=False):
    """{code: value}: each tracked name gets the first figure after it, before any other name."""
    hits = _name_hits(text, names, stoppers)
    out = {}
    for i, (s, e, code) in enumerate(hits):
        if code is None or code in out:
            continue
        nxt = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        window = text[e:min(e + win, nxt)]
        for m in FIG.finditer(window):
            if need_pct and not m.group(2):
                continue
            v = _num(m.group(1))
            if v is not None and 3 <= v <= 97 and not re.match(r"\s*(?:de|del)\s+(?:enero|febrero|marzo|abril|mayo|junio|julio|"
                                                                  r"agosto|septiembre|octubre|noviembre|diciembre)",
                                                                  window[m.end():], re.I):
                out[code] = v
                break
    return out


# --- Mitofsky: monthly "Ranking aprobación de mandatarios" posts (mitofsky.mx, Evaluación de gobierno)
MITOFSKY_CAT = "https://www.mitofsky.mx/estudios/categories/evgobierno"
WIKI_FIRMS = [("cb consultora", "CB Consultora (imagen positiva)"), ("cid gallup", "CID Gallup"),
              ("atlas", "AtlasIntel"), ("cadem", "Cadem"), ("criteria", "Criteria"), ("cedatos", "Cedatos"),
              ("equipos", "Equipos Consultores"), ("opcion", "Opción Consultores"), ("pulso ciudadano", "Pulso Ciudadano"),
              ("panel ciudadano", "Panel Ciudadano UDD"), ("cep", "CEP"), ("maluk", "Maluk Research")]
ES_ABBR = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9,
           "oct": 10, "nov": 11, "dic": 12}


def _month_end(y, m):
    nxt = datetime.date(y + (m == 12), m % 12 + 1, 1)
    return nxt - datetime.timedelta(days=1)


def src_mitofsky(names, stoppers, have_sources, log):
    from bs4 import BeautifulSoup
    out = []
    try:
        cat = _get(MITOFSKY_CAT)
    except Exception as e:
        log.append(f"  · Mitofsky listing failed ({e})")
        return out
    urls = sorted(set(re.findall(r"https?://www\.mitofsky\.mx/post/[a-z0-9\-]*mandatarios[a-z0-9\-]*", cat, re.I)))
    for url in urls:
        if url in have_sources:
            continue
        try:
            page = _get(url)
        except Exception as e:
            log.append(f"  · Mitofsky {url}: {e}")
            continue
        soup = BeautifulSoup(page, "lxml")
        title = soup.title.get_text(" ", strip=True) if soup.title else url
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        m = re.search(r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\s+(?:de\s+)?(\d{4})",
                      title, re.I)
        if m:
            date = _month_end(int(m.group(2)), ES_MONTHS[m.group(1).lower()])
        else:
            s = re.search(r"-(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)(\d{2})$", url)
            if not s:
                log.append(f"  · Mitofsky {url}: no month in title")
                continue
            date = _month_end(2000 + int(s.group(2)), ES_ABBR[s.group(1)])
        figs = credit_figures(text, names, stoppers)
        for code, v in figs.items():
            out.append((code, {"date": date.isoformat(), "firm": "Mitofsky (ranking)", "approve": v,
                               "disapprove": None, "approx": True, "source": url, "origin": "pollster"}))
        log.append(f"  · Mitofsky {date:%Y-%m}: {len(figs)} tracked presidents")
    return out


# --- CID Gallup: weekly bulletin listing (cidgallup.com/publicaciones.php), title + excerpt + PDF
CID_LIST = "https://www.cidgallup.com/publicaciones.php"
CID_APPROVAL = [
    re.compile(r"\(?(\d{1,2})\s*%\)?\s*(?:de los \w+\s+)?aprueban? (?:la (?:labor|gesti[oó]n)|el trabajo)", re.I),
    re.compile(r"(\d{1,2})\s*%\s*de aprobaci[oó]n", re.I),
    re.compile(r"aprobaci[oó]n(?: de (?:labores|gesti[oó]n))?\s*(?:en|de|del|llega a|alcanza)\s*(?:un\s*)?(\d{1,2})\s*%", re.I),
    re.compile(r"evaluaci[oó]n positiva[^.%]{0,60}?(\d{1,2})\s*%", re.I),
]
CID_NOT = re.compile(r"\bvotos?\b|triunfar|ganar[ií]a|primera vuelta|intenci[oó]n", re.I)


def src_cidgallup(names, stoppers, have_sources, log):
    from bs4 import BeautifulSoup
    out = []
    try:
        soup = BeautifulSoup(_get(CID_LIST), "lxml")
    except Exception as e:
        log.append(f"  · CID Gallup listing failed ({e})")
        return out
    for h in soup.find_all(["h1", "h2"]):
        a = h.find("a", href=True)
        if not a:
            continue
        url = a["href"] if a["href"].startswith("http") else "https://www.cidgallup.com/" + a["href"].lstrip("/")
        if url in have_sources:
            continue
        title = h.get_text(" ", strip=True)
        body = []
        for sib in h.find_all_next():
            if sib.name in ("h1", "h2"):
                break
            if sib.name == "p":
                body.append(sib.get_text(" ", strip=True))
        text = title + ". " + " ".join(body)
        codes = {c for _, _, c in _name_hits(text, names, stoppers) if c}
        if len(codes) != 1:
            continue
        code = codes.pop()
        val = None
        for sent in re.split(r"(?<=[.;])\s+", text):
            if CID_NOT.search(sent):
                continue
            for p in CID_APPROVAL:
                m = p.search(sent)
                if m:
                    val = _num(m.group(1))
                    break
            if val is not None:
                break
        if val is None or not _ok(val, None):
            continue
        ts = re.search(r"publication_(\d{13})", url)
        if ts:
            pub = datetime.datetime.utcfromtimestamp(int(ts.group(1)) / 1000).date()
        else:
            pub = _es_date(text) or datetime.date.today()
        out.append((code, {"date": (pub - datetime.timedelta(days=7)).isoformat(), "firm": "CID Gallup",
                           "approve": val, "disapprove": None, "approx": True, "source": url,
                           "origin": "pollster"}))
        log.append(f"  · CID Gallup {code} {pub}: {val}%")
    return out


# --- CB Global Data: monthly "Ranking presidentes de Latinoamérica" (cbglobaldata.com)
# The post only embeds a PDF from Google Drive. In that PDF the current month's approval and
# disapproval are drawn inside the chart image, while the text layer carries only last month's
# figure ("IMAGEN POSITIVA OCT./26"). So:
#   1. the ranking page is rendered and read with OCR (tesseract) for this month's approve /
#      disapprove; the rows are matched to the presidents by rank and checked against the text
#      layer's last-month column, which OCR also reads on each row — if they do not line up the OCR
#      result is discarded rather than guessed;
#   2. if OCR is unavailable or fails the check, this month's figures are taken from the executive
#      summary (top three and bottom three only), and last month's column is kept for everyone.
CB_FIRM = "CB Consultora (imagen positiva)"
CB_SEARCH = "https://cbglobaldata.com/wp-json/wp/v2/posts"
CB_COUNTRY = {"mexico": "MX", "el salvador": "SV", "brasil": "BR", "ecuador": "EC", "colombia": "CO",
              "honduras": "HN", "peru": "PE", "bolivia": "BO", "venezuela": "VE", "panama": "PA",
              "chile": "CL", "guatemala": "GT", "argentina": "AR", "uruguay": "UY", "paraguay": "PY",
              "costa rica": "CR", "rep. dominicana": "DO", "republica dominicana": "DO", "nicaragua": "NI"}
PCT_RE = re.compile(r"(\d{1,2}[.,]\d)\s*%")


def _cb_posts(log, backfill=False):
    """Monthly presidential rankings: 'ranking presidentes de Latinoamérica' (2026 on, 18 countries)
    and, for backfill, the older 'ranking presidentes de Sudamérica' (2024–25, South America only)."""
    import requests
    searches = ["ranking presidentes latinoamerica"] + (["ranking presidentes sudamerica",
                                                          "ranking de presidentes sudamericanos"] if backfill else [])
    out, seen = [], set()
    for q in searches:
        try:
            r = requests.get(CB_SEARCH, params={"search": q, "per_page": 100 if backfill else 6,
                                                "_fields": "link,date,title,content"}, timeout=60, headers=UA)
            r.raise_for_status()
            for p in r.json():
                l = p["link"]
                if "ranking" in l and "presidentes" in l and "alcaldes" not in l and l not in seen:
                    seen.add(l)
                    out.append((l, p["date"][:10], p["content"]["rendered"]))
        except Exception as e:
            log.append(f"  · CB Global Data listing failed ({e})")
    return out


def _cb_pdf(content):
    import requests
    m = re.search(r"drive\.google\.com/file/d/([\w-]+)", content)
    if not m:
        return None
    url = f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    blob = requests.get(url, timeout=120, headers=UA).content
    if not blob.startswith(b"%PDF"):
        tok = re.search(rb'name="confirm" value="([^"]+)"', blob)
        uuid = re.search(rb'name="uuid" value="([^"]+)"', blob)
        if tok:
            blob = requests.get("https://drive.usercontent.google.com/download",
                                params={"id": m.group(1), "export": "download", "confirm": tok.group(1).decode(),
                                        "uuid": uuid.group(1).decode() if uuid else ""}, timeout=120, headers=UA).content
    return blob if blob.startswith(b"%PDF") else None


def _cb_ocr_page(blob, page_no):
    """OCR readings of one PDF page (1-based) under a few settings, or [] if poppler/tesseract are
    not installed. parse_cb_ranking keeps the first reading that passes its checks."""
    import subprocess, tempfile, shutil, os
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        return []
    outs = []
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "r.pdf")
        open(pdf, "wb").write(blob)
        for dpi in (300, 200):
            stem = os.path.join(d, f"p{dpi}")
            subprocess.run(["pdftoppm", "-r", str(dpi), "-gray", "-f", str(page_no), "-l", str(page_no),
                            "-png", pdf, stem], check=True, timeout=180)
            png = [os.path.join(d, f) for f in os.listdir(d) if f.startswith(f"p{dpi}") and f.endswith(".png")][0]
            for psm in ("6", "4"):
                r = subprocess.run(["tesseract", png, "-", "--psm", psm], capture_output=True, text=True, timeout=180)
                outs.append(r.stdout)
    return outs


def parse_cb_ranking(page_text, ocr_texts):
    """-> rows [{rank, name, country, prev, approve, disapprove}] from the ranking page.
    `prev` (last month) comes from the text layer; approve/disapprove from OCR when it lines up."""
    t = re.sub(r"\s+", " ", page_text)
    countries = sorted({k for k in CB_COUNTRY}, key=len, reverse=True)
    rx = re.compile(r"\b(\d{1,2})\.\s+([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\.\s'’-]{2,40}?)\s+(" +
                    "|".join(re.escape(c).replace("e", "[EÉ]").replace("a", "[AÁ]").replace("u", "[UÚ]")
                             .replace("i", "[IÍ]").replace("o", "[OÓ]") for c in countries) + r")\b", re.I)
    rows, seen = [], set()
    for m in rx.finditer(t):
        c = CB_COUNTRY.get(_fold(m.group(3)).strip())
        rank = int(m.group(1))
        if c and rank not in seen:
            seen.add(rank)
            rows.append({"rank": rank, "name": m.group(2).strip(), "country": c, "prev": None,
                         "approve": None, "disapprove": None})
    rows.sort(key=lambda r: r["rank"])
    prev = [_num(x) for x in PCT_RE.findall(t)]     # the text layer holds only last month's column
    if rows and len(prev) == len(rows):
        for r, v in zip(rows, prev):
            r["prev"] = v
    if isinstance(ocr_texts, str):
        ocr_texts = [ocr_texts]
    for ocr_text in ocr_texts or []:
        lines = [PCT_RE.findall(l) for l in ocr_text.splitlines()]
        lines = [[_num(x) for x in l] for l in lines if len(l) >= 2]
        if len(lines) != len(rows):
            continue
        checked = ok = 0
        sane = True
        for r, l in zip(rows, lines):
            a, d = l[0], l[1]
            if not (3 <= a <= 97 and 3 <= d <= 97 and 85 <= a + d <= 101.5):
                sane = False
            if len(l) >= 3 and r["prev"] is not None:
                checked += 1
                ok += abs(l[-1] - r["prev"]) < 0.05
        if sane and checked >= len(rows) // 2 and ok >= checked - 1:
            for r, l in zip(rows, lines):
                r["approve"], r["disapprove"] = l[0], l[1]
            break
    return rows


def _cb_text_pages(blob):
    """Text of each page. poppler's pdftotext is fast; pypdf can take many minutes on this
    image-heavy PDF, so it is only a fallback and is cut off after two minutes."""
    import subprocess, tempfile, shutil, os, signal
    if shutil.which("pdftotext"):
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "r.pdf")
            open(pdf, "wb").write(blob)
            r = subprocess.run(["pdftotext", "-enc", "UTF-8", pdf, "-"], capture_output=True, text=True, timeout=120)
            return r.stdout.split("\f")
    from pypdf import PdfReader

    def _stop(*_):
        raise TimeoutError("pypdf took too long")
    signal.signal(signal.SIGALRM, _stop)
    signal.alarm(120)
    try:
        return [(p.extract_text() or "") for p in PdfReader(io.BytesIO(blob)).pages]
    finally:
        signal.alarm(0)


def src_cbglobal(names, stoppers, have_sources, log, backfill=False):
    out = []
    for link, pub, content in _cb_posts(log, backfill):
        # daily: only recent rankings; backfill: every ranking CB has published
        if link in have_sources or (not backfill and (datetime.date.today() - datetime.date.fromisoformat(pub)).days > 40):
            continue
        try:
            blob = _cb_pdf(content)
        except Exception as e:
            log.append(f"  · CB {link}: PDF download failed ({e})")
            continue
        if not blob:
            log.append(f"  · CB {link}: no PDF found")
            continue
        try:
            pages = _cb_text_pages(blob)
        except Exception as e:
            log.append(f"  · CB {link}: could not read the PDF text ({e})")
            continue
        full = "\n".join(pages)
        fw = re.search(r"DEL\s+\d{1,2}\s+AL\s+(\d{1,2})\s+de\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s+de\s+(\d{4})", full, re.I)
        if fw and _fold(fw.group(2)).strip() in ES_MONTHS:
            end = datetime.date(int(fw.group(3)), ES_MONTHS[_fold(fw.group(2)).strip()], int(fw.group(1)))
        else:
            end = datetime.date.fromisoformat(pub) - datetime.timedelta(days=2)
        rp = next((i for i, p in enumerate(pages) if "APROBACI" in p.upper() and re.search(r"\b1\.\s+[A-Z]", p)), None)
        if rp is None:
            log.append(f"  · CB {link}: ranking page not found")
            continue
        ocr = []
        try:
            ocr = _cb_ocr_page(blob, rp + 1)
        except Exception as e:
            log.append(f"  · CB {link}: OCR failed ({e})")
        rows = parse_cb_ranking(pages[rp], ocr)
        n_by = {}
        for m in re.finditer(r"ENCUESTA\s+([A-ZÁÉÍÓÚÑ\.\s]+?)\s*-\s*CB GLOBAL DATA.{0,80}?MUESTRA\s+(\d[\d.]*)", full, re.S):
            c = CB_COUNTRY.get(_fold(m.group(1)).strip())
            if c:
                n_by[c] = int(m.group(2).replace(".", ""))
        if not any(r["approve"] is not None for r in rows):        # fall back to the executive summary
            summ = next((p for p in pages if "imagen positiva" in p.lower() and "%" in p), "")
            figs = credit_figures(re.sub(r"\s+", " ", summ), names, stoppers)
            for r in rows:
                if r["country"] in figs:
                    r["approve"] = figs[r["country"]]
        used = 0
        prev_date = (end - datetime.timedelta(days=31)).isoformat()
        for r in rows:
            c = r["country"]
            if c not in names:
                continue
            if r["approve"] is not None:
                out.append((c, {"date": end.isoformat(), "firm": CB_FIRM, "approve": r["approve"],
                                "disapprove": r["disapprove"], "n": n_by.get(c), "approx": False,
                                "source": link, "origin": "pollster"}))
                used += 1
            if r["prev"] is not None:     # last month's figure, kept for months whose PDF we never read
                out.append((c, {"date": prev_date, "firm": CB_FIRM, "approve": r["prev"], "disapprove": None,
                                "approx": True, "source": link + "#previous-month", "origin": "pollster"}))
        how = "OCR" if any(r["disapprove"] is not None for r in rows) else "summary only"
        log.append(f"  · CB Global Data {end:%Y-%m}: {used} presidents this month ({how}), "
                   f"{sum(r['prev'] is not None for r in rows)} with last month's figure")
    return out


def run_regional(names, stoppers, have_sources, log, backfill=False):
    return (_safe(src_mitofsky, "Mitofsky", log, names, stoppers, have_sources, log) +
            _safe(src_cidgallup, "CID Gallup", log, names, stoppers, have_sources, log) +
            _safe(src_cbglobal, "CB Global Data", log, names, stoppers, have_sources, log, backfill))
