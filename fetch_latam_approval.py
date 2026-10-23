#!/usr/bin/env python3
"""
fetch_latam_approval.py — daily presidential-approval tracker for eleven Latin American countries,
built from the pollsters' own releases and Wikipedia's polling tables (latam_sources.py) plus Google
News (RSS) coverage of published polls. Feeds latam.html.

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

import latam_sources

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
    "BR": ("Brazil", "Luiz Inácio Lula da Silva", ["lula"], "2023-01-01", "BR",
           ["datafolha", "quaest", "genial quaest", "atlas", "atlasintel", "ipec", "poderdata", "parana pesquisas",
            "cnt mda", "mda", "ipespe", "real time big data", "ideia", "futura", "vox populi"]),
    "HN": ("Honduras", "Nasry Asfura", ["asfura"], "2026-01-27", "HN",
           ["eric", "eric sj", "le vote", "paradigma", "tresearch", "unah"]),
}
REGIONAL = ["cb", "atlasintel", "atlas intel", "cid gallup", "cid-gallup", "gallup", "ipsos", "latinobarometro",
            "mitofsky", "comunicaliza", "cb consultora", "cb global"]

QUERIES = ["aprobación {s} encuesta", "aprueba gestión {s} encuesta", "desaprobación {s} encuesta",
           "aprobación {s} sondeo", "popularidad {s} encuesta", "favorabilidad {s} encuesta"]
# headlines that name the pollster are the ones we can attribute: search each country's main firms too
FIRM_QUERIES = {
    "CL": ["Cadem", "Criteria", "Pulso Ciudadano", "CEP", "Panel Ciudadano UDD"],
    "BO": ["Ipsos Ciesmori", "Diagnosis", "Captura Consulting"],
    "PE": ["Ipsos", "Datum", "IEP", "CPI"],
    "CO": ["Invamer", "Guarumo", "CNC", "AtlasIntel", "Opinómetro", "YanHaas", "GAD3", "Datexco", "EcoAnalítica"],
    "VE": ["Datanálisis", "Delphos", "Meganálisis", "AtlasIntel"],
    "MX": ["El Financiero", "Enkoll", "Mitofsky", "Buendía & Márquez", "Demoscopia Digital"],
    "EC": ["Cedatos", "Comunicaliza", "Click Research", "Negocios & Estrategias"],
    "SV": ["CID Gallup", "LPG Datos", "IUDOP"],
    "PA": ["CID Gallup", "Ipsos", "Dichter & Neira", "Prodigious"],
    "GT": ["CID Gallup", "Prodatos", "Fundación Libertad y Desarrollo"],
    "HN": ["CID Gallup", "ERIC", "Le Vote", "Paradigma"],
    "BR": ["Datafolha", "Quaest", "AtlasIntel", "Ipec", "PoderData", "Paraná Pesquisas", "CNT/MDA"],
}
UNNAMED = "Pollster not named (press)"
RSS = "https://news.google.com/rss/search?q={q}+when:{w}&hl=es-419&gl={gl}&ceid={gl}:es-419"
RSS_PT = "https://news.google.com/rss/search?q={q}+when:{w}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
LANG = {"BR": "pt"}
QUERIES_PT = ["aprovação {s} pesquisa", "desaprovação {s} pesquisa", "aprovação governo {s}",
              "avaliação governo {s} pesquisa aprova"]

OFF_TOPIC = re.compile(r"\branking\b|\bimagen\b(?!.{0,40}gesti[oó]n)|alcald|gobernador|intendente|"
                       r"intenci[oó]n de voto|primera vuelta|segunda vuelta|falso|bulo|"
                       r"\bfake\b|desmiente|inten[cç][aã]o de voto|turno|prefeit|governador|\bvotos\b|"
                       r"lidera (?:a )?(?:corrida|disputa)|empate t[eé]cnico|cen[aá]rio", re.I)
ON_TOPIC = re.compile(r"aprob|aprueb|aprov|gesti[oó]n|popularidad|respaldo|favorabilidad", re.I)
PCT = r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%"
GAP_A = r"(?:(?!desaprob|desaprueb|desaprov|reprov|rechaz)[^.;%]){0,40}?"
GAP_D = r"(?:(?!aprob|aprueb|aprov|respald|\by\b|\be\b)[^.;%]){0,40}?"
A_NOUN = r"(?:aprobaci[oó]n|aprova[cç][aã]o|favorabilidad)"
A_VERB = r"(?:aprueban?|aprovam?|respalda)"
D_NOUN = r"(?:desaprobaci[oó]n|desaprova[cç][aã]o|reprova[cç][aã]o|rechazo|desfavorabilidad)"
D_VERB = r"(?:desaprueban?|desaprovam?|reprovam?|rechaza)"
FROM_TO = r"[^.%]{0,60}?\b(?:de(?:l)?|d[eo])\s+\d{1,2}(?:[.,]\d{1,2})?\s*%\s+(?:al?|para)\s+" + PCT   # "cae de 65% a 32%"
APPROVE = [re.compile(r"(?<!des)(?<!re)" + A_NOUN + FROM_TO, re.I),
           re.compile(PCT + GAP_A + r"(?<!des)(?<!re)(?:" + A_VERB + "|" + A_NOUN + ")", re.I),
           re.compile(r"(?<!des)(?<!re)(?:" + A_NOUN + "|" + A_VERB + r")[^.%]{0,60}?" + PCT, re.I)]
DISAPPROVE = [re.compile(D_NOUN + FROM_TO, re.I),
              re.compile(r"(?:" + D_NOUN + "|" + D_VERB + r")[^.%]{0,60}?" + PCT, re.I),
              re.compile(PCT + GAP_D + r"(?:" + D_VERB + "|" + D_NOUN + ")", re.I)]

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
             "negocios estrategias": "Negocios & Estrategias", "maluk": "Maluk Research",
             "atlas": "AtlasIntel", "cb": "CB Consultora (imagen positiva)", "cb consultora": "CB Consultora (imagen positiva)",
             "genial quaest": "Quaest", "cnt mda": "CNT/MDA", "mda": "CNT/MDA", "poderdata": "PoderData",
             "parana pesquisas": "Paraná Pesquisas"}
    return names.get(alias, alias.title())


def find_firm(text, firms):
    t = " " + key(text) + " "
    for f in sorted(firms, key=len, reverse=True):
        if f" {key(f)} " in t:
            return nice_firm(key(f))
    return None


VAGUE = re.compile(r"(?:menos|m[aá]s|mais|cerca|debajo|abaixo|encima|acima|alrededor|casi|quase|supera|superior|inferior)\s+(?:de|del|do|da|al|a)?\s*(?:el\s+|o\s+)?$", re.I)


def first(pats, text):
    """pats[0] is the 'from X% to Y%' form and wins outright; otherwise the tightest keyword-number
    pair wins, so '52,9% desaprovam … e 45,4% aprovam' credits each figure to its own verb."""
    best = None
    for i, p in enumerate(pats):
        for m in p.finditer(text):
            if VAGUE.search(text[max(0, m.start(1) - 25):m.start(1)]):
                continue                      # "menos del 50%" is a threshold, not a reading
            if i == 0:
                return float(m.group(1).replace(",", "."))
            span = m.end() - m.start()
            if best is None or span < best[0]:
                best = (span, float(m.group(1).replace(",", ".")))
    return best[1] if best else None


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
        # still a poll if the headline says so; kept at half weight in the average
        if not re.search(r"encuesta|sondeo|medici[oó]n|estudio|consultora|seg[uú]n", title, re.I):
            return None
        firm = UNNAMED
    if code == "BO" and firm == "Ipsos":
        firm = "Ipsos Ciesmori"
    if re.search(r"favorabilidad|imagen favorable", title, re.I) and not re.search(r"aprob|aprueb|aprov", title, re.I) \
            and "imagen" not in firm:
        firm += " (favorabilidad)"
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
STOPPERS = ["milei", "orsi", "pena", "ortega", "abinader", "chaves", "laura fernandez", "trump",
            "carney", "petro", "boric", "balcazar", "jeri", "boluarte", "arce", "xiomara", "castro", "maduro",
            "takaichi", "meloni", "frederiksen", "trudeau", "putin", "john lee", "macron", "merz",
            "laura fernandez", "noboa padre", "starmer", "sanchez", "modi", "erdogan", "xi jinping"]


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


def fetch_query(q, window, gl, lang="es"):
    import requests
    from urllib.parse import quote_plus
    url = (RSS_PT if lang == "pt" else RSS).format(q=quote_plus(q), w=window, gl=gl)
    try:
        r = requests.get(url, timeout=30, headers=latam_sources.UA)
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
    surname = {"CO": "De la Espriella", "VE": "Delcy Rodríguez", "BR": "Lula"}.get(code, pres.split()[-1])
    lang = LANG.get(code, "es")
    word = "aprovação" if lang == "pt" else "aprobación"
    items = []
    for q in (QUERIES_PT if lang == "pt" else QUERIES):
        items.extend(fetch_query(q.format(s=surname), window, gl, lang))
    for firm in FIRM_QUERIES.get(code, []):
        items.extend(fetch_query(f"{firm} {surname} {word}", window, gl, lang))
    return items


def is_dup(p, polls):
    """Same firm, approval within 1.5 pts, and close in time. Weekly trackers (Cadem, Criteria)
    publish similar numbers a week apart, so exact-dated polls only merge within 4 days."""
    t = datetime.date.fromisoformat(p["date"])
    for q in polls:
        win = 4 if not (p.get("approx") or q.get("approx")) else 12
        if q["firm"] == p["firm"] and abs(q["approve"] - p["approve"]) <= 1.5 and \
           abs((datetime.date.fromisoformat(q["date"]) - t).days) <= win:
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
    base = base * np.array([0.5 if p["firm"] == UNNAMED else 1.0 for p in pts])
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


RANK = {"manual": 3, "pollster": 2, "wikipedia": 2, "news": 1, "news-ranking": 1}


def add(code, p, store, log):
    polls = store[code]
    d = is_dup(p, polls)
    if d is None and p["firm"] == UNNAMED:
        # an unattributed headline about a poll we already hold from a named firm
        t = datetime.date.fromisoformat(p["date"])
        d = next((q for q in polls if abs(q["approve"] - p["approve"]) <= 1.0 and
                  abs((datetime.date.fromisoformat(q["date"]) - t).days) <= 10), None)
    if d is not None:
        if RANK.get(p.get("origin"), 1) > RANK.get(d.get("origin"), 1):
            d.clear(); d.update(p)                 # a primary source replaces a press report
            log.append(f"  ~ {code} {p['firm']} {p['date']}: replaced press copy with {p['origin']}")
        elif d.get("disapprove") is None and p["disapprove"] is not None:
            d["disapprove"] = p["disapprove"]
        return 0
    polls.append(p)
    log.append(f"  + {code} {p['firm']} ≈{p['date']}: {p['approve']}/{p['disapprove']} [{p.get('origin')}]")
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

    # hand-verified polls live in latam_seed.json and are merged on every run, so the scraped
    # history in latam_approval.json is never overwritten when seeds are added
    seed_path = HERE / "latam_seed.json"
    if seed_path.exists():
        for code in store:                      # the seed file is the only home of manual rows
            store[code] = [p for p in store[code] if p.get("origin") != "manual"]
        seed = json.loads(seed_path.read_text(encoding="utf-8")).get("countries", {})
        for code, rows in seed.items():
            if code not in store:
                continue
            for p in rows:
                if p["date"] < PRESIDENTS[code][3]:
                    continue
                same = [q for q in store[code] if q["firm"] == p["firm"] and q["date"] == p["date"]]
                if same:
                    same[0].update(p)
                else:                            # curated rows are trusted as distinct polls
                    store[code].append(dict(p))

    added = 0
    if not rebuild:
        for code in PRESIDENTS:
            since = PRESIDENTS[code][3]
            have = {p.get("source") for p in store[code]}
            for p in latam_sources.run_all(code, since, have, log):
                if p["date"] >= since:
                    added += add(code, p, store, log)
        names = {c: v[2] for c, v in PRESIDENTS.items()}
        have_all = {p.get("source") for v in store.values() for p in v}
        for code, p in latam_sources.run_regional(names, STOPPERS, have_all, log):
            if p["date"] >= PRESIDENTS[code][3]:
                added += add(code, p, store, log)
        pool, seen = [], set()
        def take(items):
            for it in items:
                k = (it["link"] or it["title"])
                if k not in seen:
                    seen.add(k); pool.append(it)
        for code in PRESIDENTS:
            take(fetch(code, "365d"))     # Google News returns ≤100 items per query; dedupe handles overlap
        thin = any(len(v) < 4 for v in store.values())
        for q, gl in RANK_QUERIES:
            take(fetch_query(q, "365d", gl))
        log.append(f"  · Google News: {len(pool)} unique articles read")
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
    # run report committed next to the data, so a thin country can be diagnosed from the repo
    from collections import Counter
    rep = [f"run {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC · {added} new poll(s)", ""]
    for code, polls in store.items():
        c = Counter(p.get("origin", "?") for p in polls)
        rep.append(f"{code} {PRESIDENTS[code][0]:<12} {len(polls):>3} polls  " +
                   ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    rep += ["", "log:"] + [l for l in log if "query failed" not in l][:400]
    fails = sum(1 for l in log if "query failed" in l)
    rep.append(f"  · Google News queries failed: {fails}")
    if not dry:
        (HERE / "latam_log.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")

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
