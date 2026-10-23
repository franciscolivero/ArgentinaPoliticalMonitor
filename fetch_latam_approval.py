#!/usr/bin/env python3
"""
fetch_latam_approval.py — daily presidential-approval tracker for eleven Latin American countries,
built entirely from Google News (RSS) coverage of published polls. Feeds latam.html.

For each country it searches Google News in that country's edition, keeps an article only if
  * the headline names the sitting president (surname) and no other tracked president,
  * it talks about approval of the president / government (not image, rankings, mayors...),
  * it names a pollster on that country's list (or a regional one such as AtlasIntel or CID Gallup),
  * an approval share can be read off the headline or snippet (and a disapproval share if given),
and appends it to latam_approval.json. Regional rankings (CB Consultora's monthly image ranking,
Mitofsky's "Aprobación de mandatarios", AtlasIntel's Latam Pulse) are read too: each president
named in the article gets the first percentage that follows the name, as long as no other tracked
president is named in between (CB measures positive image and is labelled as such). The fieldwork end date is rarely in a headline, so the
poll is dated publication − 4 days and flagged approx. The same poll carried by several outlets is
merged (same firm, ±20 days, approval within 1.5 pts). Hand-entered rows (origin=manual) are
never touched. A country with fewer than 4 polls is searched over the last year (backfill);
otherwise over the last 14 days.

The average is a Gaussian-kernel poll average (21-day bandwidth, weekly grid), with each poll's
weight divided by the number of polls the same firm released in the surrounding 30 days, so a
weekly tracker does not drown out monthly ones.

Edit PRESIDENTS when a president changes; polls before `since` are ignored for that country.

Usage:
    pip install requests numpy
    python fetch_latam_approval.py            # scrape + rebuild averages
    python fetch_latam_approval.py --rebuild  # only rebuild averages from the stored polls
    python fetch_latam_approval.py --dry-run  # print what would be added
"""

import re, sys, json, html, datetime, pathlib, unicodedata
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

HERE = pathlib.Path(__file__).parent
OUT = HERE / "latam_approval.json"

# code: (country, president, surname tokens that must appear, in office since, gl, extra pollsters)
PRESIDENTS = {
    "CL": ("Chile", "José Antonio Kast", ["kast"], "2026-03-11", "CL",
           ["cadem", "criteria", "cep", "centro de estudios publicos", "pulso ciudadano",
            "panel ciudadano", "udd", "activa research", "black white", "feedback", "signos", "data influye"]),
    "BO": ("Bolivia", "Rodrigo Paz", ["rodrigo paz", "paz pereira", "presidente paz"], "2025-11-08", "BO",
           ["ipsos ciesmori", "ciesmori", "diagnosis", "captura consulting", "unitel", "red uno"]),
    "PE": ("Peru", "Keiko Fujimori", ["fujimori", "keiko"], "2026-07-28", "PE",
           ["ipsos peru", "ipsos", "datum", "iep", "instituto de estudios peruanos", "cpi", "imasen"]),
    "CO": ("Colombia", "Abelardo de la Espriella", ["de la espriella", "espriella"], "2026-08-07", "CO",
           ["invamer", "guarumo", "cnc", "centro nacional de consultoria", "datexco", "yanhaas",
            "opinometro", "polimetrica", "gallup colombia"]),
    "VE": ("Venezuela", "Delcy Rodríguez", ["delcy"], "2026-01-05", "VE",
           ["datanalisis", "delphos", "meganalisis", "hercon", "consultores 21", "more consulting", "poder y estrategia"]),
    "MX": ("Mexico", "Claudia Sheinbaum", ["sheinbaum"], "2024-10-01", "MX",
           ["el financiero", "enkoll", "buendia", "mitofsky", "consulta mitofsky", "demoscopia digital",
            "tresearch", "poligrama", "rubrum", "massive caller", "varela", "becerra mizuno", "el universal",
            "gii360", "polls mx", "laboratorio electoral", "el economista"]),
    "EC": ("Ecuador", "Daniel Noboa", ["noboa"], "2025-05-24", "EC",
           ["cedatos", "comunicaliza", "click research", "negocios estrategias", "maluk", "omnibus", "informe confidencial"]),
    "SV": ("El Salvador", "Nayib Bukele", ["bukele"], "2024-06-01", "SV",
           ["lpg datos", "iudop", "uca", "fundaungo", "data poll", "tecnomarket"]),
    "PA": ("Panama", "José Raúl Mulino", ["mulino"], "2024-07-01", "PA",
           ["ipsos", "dichter neira", "unimer", "gallup panama", "prodigious", "ipec", "tvn", "la prensa"]),
    "GT": ("Guatemala", "Bernardo Arévalo", ["arevalo"], "2024-01-14", "GT",
           ["prodatos", "prensa libre", "fundacion libertad y desarrollo", "proyecta", "aprobacion cid"]),
    "HN": ("Honduras", "Nasry Asfura", ["asfura"], "2026-01-27", "HN",
           ["eric", "eric sj", "le vote", "paradigma", "tresearch", "unah"]),
}
REGIONAL = ["atlasintel", "atlas intel", "cid gallup", "cid-gallup", "gallup", "ipsos", "latinobarometro",
            "mitofsky", "comunicaliza", "cb consultora", "cb global"]

QUERIES = ["aprobación {s} encuesta", "aprueba gestión {s} encuesta", "desaprobación {s} encuesta"]
RSS = "https://news.google.com/rss/search?q={q}+when:{w}&hl=es-419&gl={gl}&ceid={gl}:es-419"

OFF_TOPIC = re.compile(r"\branking\b|\bimagen\b(?!.{0,40}gesti[oó]n)|alcald|gobernador|intendente|"
                       r"intenci[oó]n de voto|primera vuelta|segunda vuelta|falso|bulo|"
                       r"\bfake\b|desmiente", re.I)
ON_TOPIC = re.compile(r"aprob|aprueb|gesti[oó]n|popularidad|respaldo", re.I)
PCT = r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
GAP_A = r"(?:(?!desaprob|desaprueb|rechaz)[^.%]){0,40}?"
GAP_D = r"(?:(?!aprob|aprueb|respald|\by\b)[^.%]){0,40}?"
FROM_TO = r"[^.%]{0,60}?\bde(?:l)?\s+\d{1,2}(?:[.,]\d{1,2})?\s*%\s+al?\s+" + PCT   # "cae de 65% a 32%"
APPROVE = [re.compile(r"(?<!des)aprobaci[oó]n" + FROM_TO, re.I),
           re.compile(PCT + GAP_A + r"(?<!des)(?:aprueba|aprobaci[oó]n|respalda)", re.I),
           re.compile(r"(?<!des)(?:aprobaci[oó]n|aprueba)[^.%]{0,60}?" + PCT, re.I)]
DISAPPROVE = [re.compile(r"(?:desaprobaci[oó]n|rechazo)" + FROM_TO, re.I),
              re.compile(r"(?:desaprobaci[oó]n|desaprueba|rechazo)[^.%]{0,60}?" + PCT, re.I),
              re.compile(PCT + GAP_D + r"(?:desaprueba|desaprobaci[oó]n|rechaza)", re.I)]

BW, STEP, FIRM_WIN = 21, 7, 30


def key(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def nice_firm(alias):
    names = {"cep": "CEP", "cnc": "CNC", "iep": "IEP", "cpi": "CPI", "udd": "Panel Ciudadano UDD",
             "iudop": "IUDOP (UCA)", "uca": "IUDOP (UCA)", "lpg datos": "LPG Datos", "atlas intel": "AtlasIntel",
             "atlasintel": "AtlasIntel", "cid gallup": "CID Gallup", "cid-gallup": "CID Gallup",
             "ipsos ciesmori": "Ipsos Ciesmori", "ciesmori": "Ipsos Ciesmori", "ipsos peru": "Ipsos",
             "consulta mitofsky": "Mitofsky", "buendia": "Buendía & Márquez", "el financiero": "El Financiero",
             "tresearch": "TResearch", "eric": "ERIC-SJ", "eric sj": "ERIC-SJ", "panel ciudadano": "Panel Ciudadano UDD",
             "centro de estudios publicos": "CEP", "centro nacional de consultoria": "CNC",
             "instituto de estudios peruanos": "IEP", "cb global": "CB Consultora", "dichter neira": "Dichter & Neira",
             "negocios estrategias": "Negocios & Estrategias", "maluk": "Maluk Research"}
    return names.get(alias, alias.title())


def find_firm(text, firms):
    t = " " + key(text) + " "
    for f in sorted(firms, key=len, reverse=True):
        if f" {key(f)} " in t:
            return nice_firm(key(f))
    return None


VAGUE = re.compile(r"(?:menos|m[aá]s|cerca|debajo|encima|alrededor|casi|supera|superior|inferior)\s+(?:de|del|al|a)?\s*(?:el\s+)?$", re.I)


def first(pats, text):
    for p in pats:
        for m in p.finditer(text):
            if VAGUE.search(text[max(0, m.start(1) - 25):m.start(1)]):
                continue                      # "menos del 50%" is a threshold, not a reading
            return float(m.group(1).replace(",", "."))
    return None


def parse_item(item, code):
    country, pres, tokens, since, gl, firms = PRESIDENTS[code]
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    kt = " " + key(title) + " "
    if not any(f" {key(t)} " in kt for t in tokens):
        return None
    others = [c for c, v in PRESIDENTS.items() if c != code and any(f" {key(t)} " in kt for t in v[2])]
    if others or is_multi(item) or not ON_TOPIC.search(title) or OFF_TOPIC.search(title):
        return None
    text = f"{title}. {item['desc']}"
    firm = find_firm(text, firms + REGIONAL)
    if not firm:
        return None
    if code == "BO" and firm == "Ipsos":
        firm = "Ipsos Ciesmori"
    ap = first(APPROVE, title)
    ap = ap if ap is not None else first(APPROVE, text)
    dp = first(DISAPPROVE, title)
    dp = dp if dp is not None else first(DISAPPROVE, text)
    if ap is None or not 3 <= ap <= 97:
        return None
    if dp is not None and (dp + ap > 102 or not 1 <= dp <= 97):
        dp = None
    end = item["pub"] - datetime.timedelta(days=4)
    if end.isoformat() < since:
        return None
    return {"date": end.isoformat(), "firm": firm, "approve": ap, "disapprove": dp,
            "approx": True, "source": item["link"], "origin": "news"}


# ---------------------------------------------------------------- regional rankings
# Monthly multi-country rankings (CB Consultora, Mitofsky, AtlasIntel Latam Pulse) name several
# presidents in one article. Each president is credited only with the first percentage that
# follows his or her name within a short window that names no other tracked president.
RANK_FIRMS = [("cb consultora", "CB Consultora (imagen positiva)"), ("cb global data", "CB Consultora (imagen positiva)"),
              ("cbglobaldata", "CB Consultora (imagen positiva)"), ("consultora cb", "CB Consultora (imagen positiva)"),
              ("ranking cb", "CB Consultora (imagen positiva)"), ("mitofsky", "Mitofsky (ranking)"),
              ("tresearch", "Mitofsky (ranking)"), ("latam pulse", "AtlasIntel"), ("atlasintel", "AtlasIntel"),
              ("atlas intel", "AtlasIntel")]
RANK_QUERIES = [("ranking CB presidentes América", "AR"), ("ranking presidentes Latinoamérica imagen CB", "UY"),
                ("Mitofsky aprobación mandatarios ranking", "MX"), ("ranking aprobación presidentes América", "MX"),
                ("AtlasIntel Latam Pulse aprobación", "CO"), ("presidentes mejor valorados América encuesta", "AR")]
RANK_WIN = 48
# other leaders who appear in these rankings: they are never credited, but they end a name's window
STOPPERS = ["milei", "lula", "orsi", "pena", "ortega", "abinader", "chaves", "laura fernandez", "trump",
            "carney", "petro", "boric", "balcazar", "jeri", "boluarte", "arce", "xiomara", "castro", "maduro",
            "takaichi", "meloni", "frederiksen", "trudeau"]


def fold(s):
    """Lower-case, accent-free copy with the SAME length, so positions map back to the original."""
    return "".join((unicodedata.normalize("NFKD", ch)[0] if ch.strip() else " ").lower() for ch in s)


def rank_firm(text):
    t = " " + key(text) + " "
    for alias, name in RANK_FIRMS:
        if f" {alias} " in t:
            return name
    return None


def tracked_hits(text):
    """[(start, end, code)] for every tracked-president name in text."""
    f = fold(text)
    hits = []
    for code, v in PRESIDENTS.items():
        for tok in v[2]:
            for m in re.finditer(r"\b" + re.escape(fold(tok)) + r"\b", f):
                hits.append((m.start(), m.end(), code))
    for tok in STOPPERS:
        for m in re.finditer(r"\b" + re.escape(tok) + r"\b", f):
            hits.append((m.start(), m.end(), None))
    hits.sort()
    return hits


def parse_multi(item):
    """Polls for every tracked president credited in a multi-country ranking article."""
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    text = f"{title}. {item['desc']}"
    if re.search(r"falso|bulo|fake|desmiente", text, re.I):
        return []
    firm = rank_firm(text)
    if not firm:
        return []
    hits = tracked_hits(text)
    out, seen = [], set()
    for i, (s, e, code) in enumerate(hits):
        if code is None or code in seen:
            continue
        stop = min(e + RANK_WIN, hits[i + 1][0] if i + 1 < len(hits) and hits[i + 1][2] != code else len(text))
        window = text[e:stop]
        m = re.search(PCT, window)
        if not m or VAGUE.search(window[max(0, m.start(1) - 25):m.start(1)]):
            continue
        v = float(m.group(1).replace(",", "."))
        since = PRESIDENTS[code][3]
        end = item["pub"] - datetime.timedelta(days=7)
        if not 3 <= v <= 97 or end.isoformat() < since:
            continue
        seen.add(code)
        out.append((code, {"date": end.isoformat(), "firm": firm, "approve": v, "disapprove": None,
                           "approx": True, "source": item["link"], "origin": "news-ranking"}))
    return out


def is_multi(item):
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    codes = {c for _, _, c in tracked_hits(title) if c}
    allc = {c for _, _, c in tracked_hits(f"{title}. {item['desc']}") if c}
    if rank_firm(f"{title}. {item['desc']}") and len(allc) > 1:
        return True
    return bool(re.search(r"\branking\b|mejor valorad|peor valorad|mandatarios|presidentes de (?:am[eé]rica|la regi[oó]n|latinoam)",
                          title, re.I)) or len(codes) > 1


def fetch_query(q, window, gl):
    import requests
    from urllib.parse import quote_plus
    url = RSS.format(q=quote_plus(q), w=window, gl=gl)
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (arg2027-monitor)"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  · query failed ({q}): {e}")
        return []
    items = []
    for it in root.iter("item"):
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).date()
        except Exception:
            continue
        items.append({"title": html.unescape(it.findtext("title") or ""),
                      "desc": re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or "")),
                      "pub": pub, "link": it.findtext("link") or ""})
    return items


def fetch(code, window):
    _, pres, tokens, _, gl, _ = PRESIDENTS[code]
    surname = {"CO": "De la Espriella", "VE": "Delcy Rodríguez"}.get(code, pres.split()[-1])
    items = []
    for q in QUERIES:
        items.extend(fetch_query(q.format(s=surname), window, gl))
    return items


def is_dup(p, polls):
    t = datetime.date.fromisoformat(p["date"])
    for q in polls:
        if q["firm"] == p["firm"] and abs(q["approve"] - p["approve"]) <= 1.5 and \
           abs((datetime.date.fromisoformat(q["date"]) - t).days) <= 20:
            return q
    return None


def average(polls, key_):
    import numpy as np
    pts = sorted([p for p in polls if p.get(key_) is not None], key=lambda p: p["date"])
    if len(pts) < 2:
        return {"dates": [p["date"] for p in pts], "values": [p[key_] for p in pts]}
    days = np.array([datetime.date.fromisoformat(p["date"]).toordinal() for p in pts], float)
    ys = np.array([p[key_] for p in pts], float)
    crowd = np.array([sum(1 for k, q in enumerate(pts) if q["firm"] == p["firm"] and abs(days[k] - days[i]) <= FIRM_WIN)
                      for i, p in enumerate(pts)], float)
    base = 1.0 / crowd
    grid = list(np.arange(days[0], days[-1] + 1, STEP))
    if grid[-1] != days[-1]:
        grid.append(days[-1])
    dates, vals = [], []
    for g in grid:
        w = base * np.exp(-0.5 * ((days - g) / BW) ** 2)
        if w.sum() < 1e-6:
            continue
        dates.append(datetime.date.fromordinal(int(g)).isoformat())
        vals.append(round(float((w * ys).sum() / w.sum()), 1))
    return {"dates": dates, "values": vals}


def add(code, p, store, log):
    polls = store[code]
    d = is_dup(p, polls)
    if d:
        if d.get("disapprove") is None and p["disapprove"] is not None and str(d.get("origin", "")).startswith("news"):
            d["disapprove"] = p["disapprove"]
        return 0
    polls.append(p)
    log.append(f"  + {code} {p['firm']} ≈{p['date']}: {p['approve']}/{p['disapprove']}")
    return 1


def main():
    args = sys.argv[1:]
    dry, rebuild = "--dry-run" in args, "--rebuild" in args
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"countries": {}}
    store, log = {}, []
    for code, (country, pres, _, since, _, _) in PRESIDENTS.items():
        blk = data["countries"].setdefault(code, {"polls": []})
        if blk.get("president") and blk["president"] != pres:
            blk["polls"] = []                     # new president: start a fresh series
        blk.update({"country": country, "president": pres, "since": since})
        store[code] = [p for p in blk["polls"] if p["date"] >= since]

    added = 0
    if not rebuild:
        pool, seen = [], set()
        def take(items):
            for it in items:
                k = (it["link"] or it["title"])
                if k not in seen:
                    seen.add(k); pool.append(it)
        for code in PRESIDENTS:
            take(fetch(code, "365d" if len(store[code]) < 4 else "14d"))
        thin = any(len(v) < 4 for v in store.values())
        for q, gl in RANK_QUERIES:
            take(fetch_query(q, "365d" if thin else "21d", gl))
        for it in sorted(pool, key=lambda x: x["pub"]):
            if is_multi(it):
                for code, p in parse_multi(it):
                    added += add(code, p, store, log)
            else:
                for code in PRESIDENTS:
                    p = parse_item(it, code)
                    if p:
                        added += add(code, p, store, log)
    print("\n".join(log))

    for code, polls in store.items():
        polls.sort(key=lambda p: p["date"])
        blk = data["countries"][code]
        blk["polls"] = polls
        blk["avg"] = {"approve": average(polls, "approve"), "disapprove": average(polls, "disapprove")}
    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["method"] = ("Google News coverage of national polls and regional rankings (CB Consultora, "
                      "Mitofsky, AtlasIntel); Gaussian-kernel average, 21-day bandwidth, firm-crowding weights.")
    print(f"{added} new poll(s).")
    if dry:
        return
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
