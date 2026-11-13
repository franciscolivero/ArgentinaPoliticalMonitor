#!/usr/bin/env python3
"""
latam_articles.py — reads the full text of press articles about presidential approval polls.

Google News headlines (fetch_latam_approval.py) catch polls whose figure and pollster are both in
the title. In countries with less coverage (Colombia, Peru, Uruguay, Paraguay, Costa Rica,
Dominican Republic, Central America) most polls are only described inside the article, so this
module downloads the articles and reads their body.

  1. Search: Bing News RSS (direct article links), last 45 days, per country, with the president's
     name and approval words. Titles must name the president and be about a poll or approval.
  2. Reading: each article is downloaded once (latam_articles_seen.json remembers what was read).
     In every sentence that speaks of approval or disapproval and of the president or the
     government — and not of a mayor, governor, Congress or a subgroup of voters ("entre los
     jóvenes") — the approval and disapproval figures are read with the same patterns as the
     headline reader.
  3. Attribution: the pollster must be named in that sentence or in the 400 characters before it
     (unnamed figures are dropped here: an article gives enough context to name the firm).
  4. Plausibility: once a country has three or more recent polls, a reading more than 18 points
     from their median is treated as a misread and skipped.
  5. Date: fieldwork end when the article states it, otherwise publication − 5 days (approx).

Budget: at most 12 new articles per country (6 for Brazil, Chile and Mexico, already well covered)
and 8 minutes per run; anything left is read the next day.
"""

import json, re, time, html, datetime, pathlib, statistics
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse, parse_qs
from xml.etree import ElementTree as ET

HERE = pathlib.Path(__file__).parent
SEEN = HERE / "latam_articles_seen.json"
LOOKBACK, KEEP_SEEN, BUDGET = 45, 75, 8 * 60
CAP = {"BR": 6, "CL": 6, "MX": 6}
BING = "https://www.bing.com/news/search?q={q}&format=rss&setlang={lang}&cc={cc}"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36 (arg2027-monitor)"}

ES_Q = ["encuesta aprobación {s}", "aprobación gestión {s} encuesta", "desaprobación {s} encuesta",
        "popularidad {s} encuesta"]
PT_Q = ["pesquisa aprovação {s}", "aprovação governo {s} pesquisa"]
SURNAME = {"CO": "De la Espriella", "VE": "Delcy Rodríguez", "BR": "Lula", "PY": "Santiago Peña",
           "CR": "Laura Fernández", "UY": "Orsi"}

TITLE_OK = re.compile(r"encuesta|sondeo|pesquisa|aprob|aprueb|aprova|desaprob|popularidad|gesti[oó]n|"
                      r"respaldo|favorabilidad|ranking|medici[oó]n", re.I)
SENT_OK = re.compile(r"aprob|aprueb|aprov|desaprob|desaprueb|desaprov|reprov|valoraci[oó]n (?:positiva|negativa)",
                     re.I)
SENT_SUBJECT = re.compile(r"president|mandatari|gobierno|gest[aã]o|gesti[oó]n|governo|jefe de estado", re.I)
SENT_OFF = re.compile(r"alcald|gobernador|governador|intendente|prefeit|congreso|congresso|parlamento|asamblea|"
                      r"\bsenado|ministr|corte suprema|fiscal|polic[ií]a|\bjuez|entre (?:los|las|quienes|os|as)\b|"
                      r"j[oó]venes|jovens|mujeres|mulheres|hombres|homens|votantes de|eleitores de|electores de|"
                      r"evang[eé]lic|cat[oó]lic|regi[oó]n|departamento de|\bestado de\b|imagen (?:de|del) (?!.*gesti)",
                      re.I)


def _get(url, timeout=12):
    import requests
    r = requests.get(url, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r


def _rss(url):
    root = ET.fromstring(_get(url, 20).content)
    for it in root.iter("item"):
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).date()
        except Exception:
            continue
        link = (it.findtext("link") or "").strip()
        if "bing.com/news/apiclick" in link:
            link = parse_qs(urlparse(link).query).get("url", [link])[0]
        yield html.unescape(it.findtext("title") or ""), link, pub


def _text(url):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_get(url).content, "lxml")
    for t in soup(["script", "style", "nav", "footer", "aside", "form"]):
        t.decompose()
    root = soup.find("article") or soup.body or soup
    return "\n".join(p for p in (el.get_text(" ", strip=True) for el in root.find_all(["h1", "h2", "h3", "p", "li"])) if p)


def _sentences(text):
    return [(m.start(), m.group(0)) for m in re.finditer(r"[^.\n;]+(?:\.(?!\d)|\n|;|$)", text)]


def parse_article(code, title, text, pub, presidents, key, find_firm, first, approve, disapprove,
                  regional, fieldwork_end):
    """[(poll dict)] read from one article for country `code`."""
    country, pres, tokens, since, gl, firms = presidents[code]
    kt = " " + key(title) + " "
    if any(f" {key(t)} " in kt for c, v in presidents.items() if c != code for t in v[2]):
        return []                                   # a ranking or a comparison: the regional readers handle it
    sents = _sentences(text)
    out, done = [], set()
    for k, (pos, sent) in enumerate(sents):
        ks = " " + key(sent) + " "
        if not SENT_OK.search(sent) or SENT_OFF.search(sent):
            continue
        names_pres = any(f" {key(t)} " in ks for t in tokens)
        if not (names_pres or SENT_SUBJECT.search(sent)):
            continue
        if any(f" {key(t)} " in ks for c, v in presidents.items() if c != code for t in v[2]):
            continue
        ap, dp = first(approve, sent), first(disapprove, sent)
        if ap is not None and dp is None and k + 1 < len(sents) and not SENT_OFF.search(sents[k + 1][1]):
            dp = first(disapprove, sents[k + 1][1])
        if ap is None or not 3 <= ap <= 97:
            continue
        if dp is not None and (not 1 <= dp <= 97 or ap + dp > 102):
            dp = None
        window = text[max(0, pos - 400):pos + len(sent)]
        firm = find_firm(window, firms + regional)
        if not firm or firm in done:
            continue
        if code == "BO" and firm == "Ipsos":
            firm = "Ipsos Ciesmori"
        done.add(firm)
        end, approx = fieldwork_end(text, pub, pos)
        if end.isoformat() < since:
            continue
        out.append({"date": end.isoformat(), "firm": firm, "approve": ap, "disapprove": dp, "approx": approx,
                    "origin": "news"})
    return out


def harvest(presidents, store, log, key, find_firm, first, approve, disapprove, regional):
    """[(code, poll)] from articles not read before."""
    from fetch_runoff_matchups import fieldwork_end
    seen = {}
    try:
        seen = json.loads(SEEN.read_text(encoding="utf-8"))
    except Exception:
        pass
    today = datetime.date.today()
    seen = {u: d for u, d in seen.items() if (today - datetime.date.fromisoformat(d)).days <= KEEP_SEEN}
    have = {p.get("source") for v in store.values() for p in v}
    t0, results, read = time.time(), [], 0
    # thinnest countries first, so the time budget goes where it matters
    order = sorted(presidents, key=lambda c: len(store.get(c, [])))
    for code in order:
        if time.time() - t0 > BUDGET:
            log.append("  · articles: time budget reached; the rest will be read on the next run")
            break
        country, pres, tokens, since, gl, firms = presidents[code]
        s = SURNAME.get(code, pres.split()[-1])
        pt = code == "BR"
        cands, links = [], set()
        for q in (PT_Q if pt else ES_Q):
            try:
                for title, link, pub in _rss(BING.format(q=quote_plus(q.format(s=s)), lang="pt-br" if pt else "es",
                                                         cc=gl)):
                    kt = " " + key(title) + " "
                    if (today - pub).days > LOOKBACK or link in links or link in seen or link in have:
                        continue
                    if not any(f" {key(t)} " in kt for t in tokens) or not TITLE_OK.search(title):
                        continue
                    links.add(link)
                    cands.append((pub, title, link))
            except Exception as e:
                log.append(f"  · articles {code}: search failed ({e})")
        cands.sort(reverse=True)
        n_new = 0
        recent = [p["approve"] for p in store.get(code, [])
                  if (today - datetime.date.fromisoformat(p["date"])).days <= 120]
        med = statistics.median(recent) if len(recent) >= 3 else None
        for pub, title, link in cands[:CAP.get(code, 12)]:
            if time.time() - t0 > BUDGET:
                break
            seen[link] = today.isoformat()
            try:
                text = title + "\n" + _text(link)
            except Exception:
                continue
            read += 1
            for p in parse_article(code, title, text, pub, presidents, key, find_firm, first, approve, disapprove,
                                   regional, fieldwork_end):
                if med is not None and abs(p["approve"] - med) > 18:
                    log.append(f"  · articles {code}: skipped {p['firm']} {p['approve']}% (far from recent median {med})")
                    continue
                p["source"] = link
                results.append((code, p))
                n_new += 1
        if cands:
            log.append(f"  · articles {code}: {min(len(cands), CAP.get(code, 12))} read, {n_new} poll reading(s)")
    try:
        SEEN.write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
    log.append(f"  · articles: {read} articles read in {time.time() - t0:.0f}s")
    return results
