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
DAY_MON_YEAR = re.compile(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?\s*(\d{4})?")


def _wiki_html(page):
    import json
    raw = _get("https://en.wikipedia.org/w/api.php",
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


def src_wikipedia(code, log):
    polls = []
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


def run_regional(names, stoppers, have_sources, log):
    return (_safe(src_mitofsky, "Mitofsky", log, names, stoppers, have_sources, log) +
            _safe(src_cidgallup, "CID Gallup", log, names, stoppers, have_sources, log))
