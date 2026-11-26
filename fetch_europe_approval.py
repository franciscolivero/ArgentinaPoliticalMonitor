#!/usr/bin/env python3
"""
fetch_europe_approval.py — daily approval tracker for European heads of government, built the same
way as the Latin America tracker (fetch_latam_approval.py) and sharing its average. Feeds europe.html.
There is no election forecast for Europe: only each leader's approval series and the ranking.

Sources, in order of trust:
  * Wikipedia poll tables (English pages, MediaWiki API): United Kingdom (leader approval), France
    (Macron presidency), Poland (Tusk), Italy (Meloni government), Portugal (Montenegro cabinet).
  * Pollsters' own releases: ARD-DeutschlandTrend (infratest dimap, monthly: share satisfied with the
    Chancellor) and YouGov's monthly EuroTrack (favourable opinion of the leader in Britain, France,
    Germany, Italy, Spain and Denmark; labelled "favourability").
  * Google News, searched daily in each country's language and edition. A headline counts when it
    names the leader and no other tracked leader, talks about approval / satisfaction / popularity
    (not voting intention), and gives a percentage next to an approval word. Headlines that do not
    name a pollster count at half weight. Press reports are placed four days before publication.
  * Multi-country rankings in the press (Morning Consult's Global Leader Approval, YouGov EuroTrack):
    each leader named gets the first percentage that follows the name, before any other leader.

Hand-verified rows live in europe_seed.json (origin=manual) and are merged on every run. A country
with fewer than 6 polls is backfilled from Google News over the last two years, two months at a time.

The average is the Latin America one: Gaussian kernel, 21-day bandwidth, weekly grid, a pollster that
publishes several times a month counts as one voice in that month.

Edit LEADERS when a head of government changes; polls before `since` are ignored for that country,
and a new name starts a fresh series.

Usage:
    pip install requests numpy beautifulsoup4 lxml
    python fetch_europe_approval.py            # scrape + rebuild averages
    python fetch_europe_approval.py --rebuild  # only rebuild averages from the stored polls
    python fetch_europe_approval.py --dry-run  # print what would be added
"""

import time, re, sys, json, html, datetime, pathlib
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import latam_sources as ls
from fetch_latam_approval import key, average, is_dup, add, UNNAMED

HERE = pathlib.Path(__file__).parent
OUT = HERE / "europe_approval.json"
SEED = HERE / "europe_seed.json"
LOG = HERE / "europe_log.txt"

# code: (country, leader, office, name tokens that must appear, in office since, news edition, pollsters)
# `since` for long-serving leaders is the start of the current term.
LEADERS = {
    "GB": ("United Kingdom", "Andy Burnham", "Prime minister", ["burnham"], "2026-07-20", "en-GB",
           ["yougov", "ipsos", "opinium", "more in common", "deltapoll", "savanta", "techne", "bmg",
            "survation", "find out now", "redfield", "lord ashcroft", "focaldata", "jl partners", "verian"]),
    "FR": ("France", "Emmanuel Macron", "President", ["macron"], "2022-05-14", "fr-FR",
           ["ifop", "elabe", "odoxa", "ipsos", "bva", "verian", "harris interactive", "toluna", "opinionway",
            "cluster17", "yougov", "kantar", "odoxa backbone"]),
    "DE": ("Germany", "Friedrich Merz", "Chancellor", ["merz"], "2025-05-06", "de-DE",
           ["infratest dimap", "infratest", "deutschlandtrend", "forschungsgruppe wahlen", "politbarometer",
            "forsa", "insa", "allensbach", "yougov", "civey", "gms", "verian", "ipsos"]),
    "IT": ("Italy", "Giorgia Meloni", "Prime minister", ["meloni"], "2022-10-22", "it-IT",
           ["swg", "ipsos", "demos", "termometro politico", "quorum", "youtrend", "emg", "tecne", "bidimedia",
            "ixe", "euromedia", "noto", "piepoli", "demopolis", "only numbers", "yougov"]),
    "ES": ("Spain", "Pedro Sánchez", "Prime minister", ["sanchez"], "2023-11-17", "es-ES",
           ["cis", "40db", "sigma dos", "gad3", "sociometrica", "nc report", "dym", "gesop", "imop", "invymark",
            "metroscopia", "celeste tel", "simple logica", "key data", "ipsos", "yougov", "opina 360", "target point"]),
    "PL": ("Poland", "Donald Tusk", "Prime minister", ["tusk"], "2023-12-13", "pl-PL",
           ["cbos", "ibris", "united surveys", "opinia24", "ogb", "sw research", "ipsos", "pollster", "ucE research",
            "kantar", "estymator", "research partner"]),
    "NL": ("Netherlands", "Rob Jetten", "Prime minister", ["jetten"], "2026-02-23", "nl-NL",
           ["ipsos i&o", "i&o research", "ipsos", "peil.nl", "maurice de hond", "eenvandaag", "verian",
            "kantar", "scp"]),
    "PT": ("Portugal", "Luís Montenegro", "Prime minister", ["montenegro"], "2025-06-05", "pt-PT",
           ["aximage", "intercampus", "cesop", "catolica", "pitagorica", "ics iscte", "iscte", "consulmark2",
            "duplimetria"]),
    "HU": ("Hungary", "Péter Magyar", "Prime minister", ["magyar peter", "peter magyar", "magyar"], "2026-05-09",
           "hu-HU", ["median", "medián", "republikon", "21 kutatokozpont", "21 kutatóközpont", "publicus",
                     "zavecz", "zavecz research", "idea", "nezopont", "nézőpont", "partizan", "tarki", "závecz"]),
    "CZ": ("Czechia", "Andrej Babiš", "Prime minister", ["babis"], "2025-12-09", "cs-CZ",
           ["stem", "cvvm", "median", "kantar", "nms", "ipsos", "data collect"]),
    "DK": ("Denmark", "Mette Frederiksen", "Prime minister", ["frederiksen"], "2022-12-15", "da-DK",
           ["voxmeter", "epinion", "megafon", "verian", "yougov", "gallup"]),
    "BE": ("Belgium", "Bart De Wever", "Prime minister", ["de wever"], "2025-02-03", "nl-BE",
           ["ipsos", "kantar", "dedicated", "het grote ongenoegen"]),
    "AT": ("Austria", "Christian Stocker", "Chancellor", ["stocker"], "2025-03-03", "de-AT",
           ["ogm", "market", "unique research", "peter hajek", "hajek", "ifdd", "lazarsfeld", "spectra", "ogm",
            "market institut", "foresight"]),
    "IE": ("Ireland", "Micheál Martin", "Taoiseach", ["micheal martin", "taoiseach"], "2025-01-23", "en-IE",
           ["red c", "ireland thinks", "ipsos b&a", "ipsos", "amarach", "behaviour & attitudes"]),
    "GR": ("Greece", "Kyriakos Mitsotakis", "Prime minister", ["mitsotakis", "μητσοτακη"], "2023-06-26", "el-GR",
           ["pulse", "metron analysis", "mrb", "gpo", "marc", "alco", "interview", "opinion poll", "prorata",
            "real polls", "palmos analysis"]),
    "NO": ("Norway", "Jonas Gahr Støre", "Prime minister", ["store", "gahr store"], "2021-10-14", "no-NO",
           ["norstat", "opinion", "respons", "verian", "ipsos", "infact", "nettavisen"]),
}
REGIONAL = ["morning consult", "yougov", "ipsos", "eurobarometer", "ecfr", "gallup"]

# ------------------------------------------------------------------------------- language keywords
# Approval words and their negatives. A percentage is credited to the nearest keyword in its clause;
# negatives are checked first because "unzufrieden" contains "zufrieden", "insatisfaits" "satisfaits"...
A_WORDS = {
    "en": r"approv\w*|favou?rab\w*|popular\w*|positive\w*|doing (?:a )?(?:good|well)|satisf\w*|trust\w*",
    "fr": r"approuv\w*|satisfait\w*|popularit\w*|confiance|bonne opinion|opinions? (?:positives?|favorables?)|"
          r"favorables?|jug\w* favorable\w*|[ée]valu\w* positivement|bon pr[ée]sident|cote",
    "de": r"zufrieden\w*|zustimmung|befürwort\w*|beliebt\w*|positiv\w*|vertrauen",
    "it": r"gradimento|approv\w*|fiducia|favorevol\w*|consenso|promoss\w*",
    "es": r"aprob\w*|aprueb\w*|valoraci[oó]n positiva|buena opini[oó]n|confianza|favorab\w*",
    "pl": r"popier\w*|aprob\w*|dobrze ocen\w*|pozytywn\w*|zadowolen\w*|zaufan\w*|ocen\w* dobrze",
    "nl": r"tevreden\w*|vertrouwen|goedkeur\w*|positie\w*|populair\w*",
    "pt": r"aprova\w*|avalia[cç][aã]o positiva|positiv\w*|confian[cç]a|popularidade",
    "hu": r"elégedett\w*|támogat\w*|népszerű\w*|pozitív\w*|bizalom|bízik",
    "cs": r"důvěř\w*|duver\w*|spokojen\w*|pozitivn\w*|podpor\w*",
    "da": r"tilfreds\w*|godkend\w*|tillid|positiv\w*|populær\w*",
    "el": r"ικανοποι\w*|θετικ\w*|δημοφιλ\w*|εμπιστ\w*",
    "no": r"fornøyd\w*|tillit\w*|godkjen\w*|positiv\w*|populær\w*",
}
D_WORDS = {
    "en": r"disapprov\w*|unfavou?rab\w*|unpopular\w*|negative\w*|dissatisf\w*|distrust\w*|doing (?:a )?(?:bad|badly|poor)",
    "fr": r"désapprouv\w*|desapprouv\w*|insatisfait\w*|mécontent\w*|mecontent\w*|opinions? défavorables?|"
          r"défavorables?|mauvaise opinion|défiance|mauvais pr[ée]sident|jug\w* n[ée]gativement",
    "de": r"unzufrieden\w*|ablehn\w*|negativ\w*|misstrau\w*",
    "it": r"disapprov\w*|sfiducia|contrari\w*|bocciat\w*|bocci\w*|insoddisf\w*",
    "es": r"desaprob\w*|desaprueb\w*|suspend\w*|valoraci[oó]n negativa|mala opini[oó]n|rechazo|desconfianza",
    "pl": r"nie popier\w*|źle ocen\w*|negatywn\w*|niezadowol\w*|nieufn\w*|krytyczn\w*|ocen\w* źle",
    "nl": r"ontevreden\w*|wantrouwen|negatie\w*|afkeur\w*",
    "pt": r"desaprova\w*|reprova\w*|avalia[cç][aã]o negativa|negativ\w*|desconfian[cç]a",
    "hu": r"elégedetlen\w*|elutasít\w*|negatív\w*|bizalmatlan\w*",
    "cs": r"nedůvěř\w*|neduver\w*|nespokojen\w*|negativn\w*",
    "da": r"utilfreds\w*|mistillid|negativ\w*|upopulær\w*",
    "el": r"δυσαρεστ\w*|αρνητικ\w*|δυσπιστ\w*",
    "no": r"misfornøyd\w*|mistillit\w*|negativ\w*|upopulær\w*",
}
POLL_WORDS = (r"poll|survey|tracker|sondage|barom[eè]tre|enqu[eê]te|umfrage|deutschlandtrend|politbarometer|"
              r"sondaggio|sondaggi|encuesta|bar[oó]metro|sonda[zż]|badanie|peiling|onderzoek|sondagem|inqu[eé]rito|"
              r"felmérés|közvélemény|kutatás|průzkum|pruzkum|måling|undersøgelse|δημοσκόπηση|meningsmåling|"
              r"undersøkelse|morning consult|yougov|ipsos")
OFF_TOPIC = re.compile(
    r"voting intention|vote intention|seat projection|\bseats\b|leadership (?:race|contest|election)|"
    r"intention de vote|intentions de vote|1er tour|premier tour|second tour|"
    r"sonntagsfrage|wahlumfrage|kanzlerfrage|direktwahl|"
    r"intenzion[ei] di voto|intención de voto|estimaci[oó]n de voto|esca[ñn]os|"
    r"intencj[ae] g[łl]osowania|mandat[oó]w|zetels|zetelpeiling|inten[cç][aã]o de voto|deputados|"
    r"mandátů|mandát|pártpreferencia|mandátum|mandater|stortingsvalg|folketingsvalg|πρόθεση ψήφου|"
    r"campo largo|centrodestra|centrosinistra|beszéd|speech|"
    r"trump|biden|putin|zelensk|ukrain(?:e|ian) president", re.I)
PCT = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:%|per ?cent|prozent|pour ?cent|por ?ciento|procent|prosent|"
                 r"százalék|procent\w*|τοις εκατό|percent)", re.I)
NEG = re.compile(r"(?:\bne\b|\bn'|\bpas\b|\bnicht\b|\bkein\w*|\bnot\b|\bno\b|\bnon\b|\bnie\b|\bniet\b|\bnão\b|"
                 r"\bikke\b|\bnem\b|\bδεν\b)[^.;,]{0,18}$", re.I)
CHANGE = re.compile(r"(?:verliert|verloren|gewinnt|loses|lost|gains?|gained|drops?|dropped|fell|falls|rose|rises|"
                    r"\bup\b|\bdown\b|\bby\b|\bum\b|minus|plus|perde|guadagna|pierde|gana|perd|gagne)\s*$", re.I)
AFTER_CHANGE = re.compile(r"^\s*(?:points?|pts|percentage|punkte|prozentpunkte|punti|puntos|pontos|points de|"
                          r"százalékpont|procentpunt|punkt\w*|behind|ahead|lead|more|less|weniger|mehr)", re.I)
SUBGROUP = re.compile(r"\bmembers\b|\bvoters of\b|among (?:labour|tory|conservative|reform)|electores del|votantes del|"
                      r"anhänger|wähler der|unionswähler|szavazó|tiszás|elettori d|militants|sympathisants|"
                      r"j[oó]venes|young people|junge|jeunes|iscritti", re.I)
BOUND = re.compile(r"[;!?]|\.\s|\s[–—-]\s|\|")     # clause boundaries (a decimal point is not one)

RSS = "https://news.google.com/rss/search?q={q}+{w}&hl={hl}&gl={gl}&ceid={gl}:{cl}"
QUERIES = {
    "en": ["{s} approval rating poll", "{s} net approval", "{s} favourability poll", "{s} satisfaction poll"],
    "fr": ["{s} popularité sondage", "{s} satisfaits baromètre", "cote de confiance {s}", "{s} sondage approuvent"],
    "de": ["{s} Zufriedenheit Umfrage", "{s} zufrieden Prozent Umfrage", "{s} DeutschlandTrend", "{s} Politbarometer zufrieden"],
    "it": ["{s} gradimento sondaggio", "fiducia governo {s} sondaggio", "{s} approvazione sondaggio"],
    "es": ["{s} aprobación encuesta", "{s} valoración encuesta", "aprueban gestión {s}"],
    "pl": ["{s} sondaż ocena", "{s} sondaż popiera", "rząd {s} ocena sondaż CBOS"],
    "nl": ["{s} peiling tevreden", "kabinet {s} vertrouwen peiling", "{s} populariteit peiling"],
    "pt": ["{s} sondagem avaliação", "{s} sondagem aprovação", "governo {s} avaliação sondagem"],
    "hu": ["{s} közvélemény-kutatás elégedett", "{s} népszerűség felmérés", "{s} kormány támogatottság"],
    "cs": ["{s} důvěra průzkum", "{s} spokojenost průzkum", "vláda {s} důvěra"],
    "da": ["{s} tilfredshed måling", "{s} meningsmåling populær", "{s} Voxmeter tilfreds"],
    "el": ["Μητσοτάκης δημοσκόπηση θετική γνώμη", "Μητσοτάκης δημοσκόπηση ικανοποίηση"],
    "no": ["{s} måling fornøyd", "{s} tillit måling", "regjeringen {s} fornøyd måling"],
}
RANK_QUERIES = [("Morning Consult approval European leaders", "en-GB"), ("Morning Consult leader approval Meloni", "en-GB"),
                ("Morning Consult Merz approval", "en-GB"), ("most popular leader Europe Morning Consult", "en-US"),
                ("YouGov how popular are national leaders in Europe", "en-GB")]
STOPPERS = ["trump", "modi", "sheinbaum", "milei", "lula", "carney", "albanese", "takaichi", "lee jae", "erdogan",
            "putin", "zelensky", "xi jinping", "starmer", "kristersson", "andersson", "orban", "schoof", "fico",
            "plenkovic", "orpo", "nawrocki", "lecornu", "bayrou", "albares", "feijoo", "farage", "badenoch",
            "weidel", "schlein", "le pen", "bardella", "ventura", "parmelin", "bolojan", "dan", "golob"]
BACKFILL_MIN = 6


def lang(code):
    return LEADERS[code][5][:2]


def surname(code):
    return {"HU": "Magyar Péter", "IE": "Micheál Martin", "BE": "De Wever", "GR": "Μητσοτάκης",
            "NO": "Støre", "ES": "Sánchez", "CZ": "Babiš"}.get(code, LEADERS[code][1].split()[-1])


def rss_url(q, window, hl):
    from urllib.parse import quote_plus
    gl = hl.split("-")[1]
    cl = {"en": "en", "pt": "pt-150", "es": "es"}.get(hl[:2], hl[:2])
    return RSS.format(q=quote_plus(q), w=window, hl=hl, gl=gl, cl=cl)


def read_rss(url):
    import requests
    try:
        r = requests.get(url, timeout=30, headers=ls.UA)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  · query failed ({url[:90]}): {e}")
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


def fetch(code, window="when:365d"):
    hl = LEADERS[code][5]
    s = surname(code)
    items = []
    qs = QUERIES[lang(code)]
    if code == "AT":
        qs = ["{s} Zufriedenheit Umfrage", "{s} zufrieden Prozent Umfrage", "Kanzler {s} Umfrage Vertrauen"]
    for q in qs:
        items += read_rss(rss_url(q.format(s=s), window, hl))
        time.sleep(0.2)
    for firm in LEADERS[code][6][:4]:
        items += read_rss(rss_url(f"{firm} {s}", window, hl))
        time.sleep(0.2)
    return items


# --------------------------------------------------------------------------------- headline parser
def has_name(text, toks):
    t = " " + key(text) + " "
    return any(f" {key(x)} " in t for x in toks)


def find_firm(text, firms):
    t = " " + key(text) + " "
    for f in sorted(firms, key=len, reverse=True):
        if f" {key(f)} " in t:
            return nice_firm(f)
    return None


def nice_firm(f):
    names = {"yougov": "YouGov", "ipsos": "Ipsos", "opinium": "Opinium", "more in common": "More in Common",
             "deltapoll": "Deltapoll", "bmg": "BMG Research", "ifop": "Ifop", "elabe": "Elabe", "odoxa": "Odoxa",
             "bva": "BVA", "verian": "Verian", "harris interactive": "Toluna Harris Interactive",
             "toluna": "Toluna Harris Interactive", "opinionway": "OpinionWay", "infratest dimap": "Infratest dimap",
             "infratest": "Infratest dimap", "deutschlandtrend": "Infratest dimap", "politbarometer":
             "Forschungsgruppe Wahlen", "forschungsgruppe wahlen": "Forschungsgruppe Wahlen", "insa": "INSA",
             "forsa": "Forsa", "swg": "SWG", "emg": "EMG", "tecne": "Tecnè", "bidimedia": "BiDiMedia", "ixe": "Ixè",
             "cis": "CIS", "40db": "40dB", "gad3": "GAD3", "dym": "DYM", "imop": "IMOP", "cbos": "CBOS", "ibris": "IBRiS",
             "ogb": "OGB", "sw research": "SW Research", "ipsos i&o": "Ipsos I&O", "i&o research": "Ipsos I&O",
             "peil.nl": "Peil.nl", "maurice de hond": "Peil.nl", "eenvandaag": "EenVandaag", "cesop": "CESOP–UCP",
             "catolica": "CESOP–UCP", "ics iscte": "ICS/ISCTE", "iscte": "ICS/ISCTE", "pitagorica": "Pitagórica",
             "medián": "Medián", "median": "Median", "nézőpont": "Nézőpont", "nezopont": "Nézőpont",
             "21 kutatóközpont": "21 Kutatóközpont", "21 kutatokozpont": "21 Kutatóközpont", "závecz": "Závecz Research",
             "zavecz": "Závecz Research", "zavecz research": "Závecz Research", "stem": "STEM", "cvvm": "CVVM",
             "nms": "NMS", "red c": "Red C", "ireland thinks": "Ireland Thinks", "ipsos b&a": "Ipsos B&A",
             "mrb": "MRB", "gpo": "GPO", "marc": "MARC", "metron analysis": "Metron Analysis", "pulse": "Pulse RC",
             "oge": "OGM", "ogm": "OGM", "market": "Market", "unique research": "Unique Research",
             "peter hajek": "Peter Hajek", "hajek": "Peter Hajek", "ifdd": "IFDD", "morning consult": "Morning Consult",
             "voxmeter": "Voxmeter", "epinion": "Epinion", "megafon": "Megafon", "norstat": "Norstat",
             "respons": "Respons Analyse", "termometro politico": "Termometro Politico", "quorum": "Quorum",
             "youtrend": "YouTrend", "demos": "Demos & Pi", "sigma dos": "Sigma Dos", "aximage": "Aximage",
             "intercampus": "Intercampus", "republikon": "Republikon", "publicus": "Publicus", "united surveys":
             "United Surveys", "opinia24": "Opinia24", "kantar": "Kantar", "savanta": "Savanta", "techne": "Techne",
             "survation": "Survation", "find out now": "Find Out Now", "allensbach": "Allensbach", "civey": "Civey"}
    return names.get(f, f.title())


def readings(text, lg):
    """(approve, disapprove) from free text: each percentage is credited to the nearest approval or
    disapproval word in its clause (within 45 characters); the first credited figure of each wins."""
    a_re, d_re = re.compile(A_WORDS[lg], re.I), re.compile(D_WORDS[lg], re.I)
    words = []
    for m in d_re.finditer(text):
        words.append((m.start(), m.end(), "d"))
    for m in a_re.finditer(text):
        if not any(s <= m.start() < e for s, e, _ in words):
            neg = NEG.search(text[max(0, m.start() - 22):m.start()])     # "ne font pas confiance", "nicht zufrieden"
            words.append((m.start(), m.end(), "d" if neg else "a"))
    got = {}
    for m in PCT.finditer(text):
        before = text[max(0, m.start() - 25):m.start()].lower()
        if re.search(r"(?:less|more|under|over|below|above|moins|plus|weniger|mehr|meno|più|menos|más|ponad|poniżej|"
                      r"minder|meer|mais|menos|alatt|felett|méně|více|under|over)\s+(?:than|de|als|di|del|que|niż|dan|"
                      r"mint|než|end|enn)?\s*$", before):
            continue
        if CHANGE.search(text[max(0, m.start() - 20):m.start()]) or AFTER_CHANGE.search(text[m.end():m.end() + 25]):
            continue
        best = None
        for s, e, k in words:
            gap = text[e:m.start()] if e <= m.start() else text[m.end():s]
            if len(gap) > 45 or BOUND.search(gap) or PCT.search(gap):
                continue
            if best is None or len(gap) < best[0]:
                best = (len(gap), k)
        if best and best[1] not in got:
            got[best[1]] = float(m.group(1).replace(",", "."))
    return got.get("a"), got.get("d")


def parse_item(item, code):
    country, leader, office, toks, since, hl, firms = LEADERS[code]
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    if not has_name(title, toks):
        return None
    if any(has_name(title, v[3]) for c, v in LEADERS.items() if c != code) or OFF_TOPIC.search(title) \
            or SUBGROUP.search(title):
        return None
    if is_multi(item):
        return None
    text = f"{title}. {item['desc']}"
    firm = find_firm(text, firms + REGIONAL)
    if not firm:
        if not re.search(POLL_WORDS, title, re.I):
            return None
        firm = UNNAMED
    ap, dp = readings(title, lang(code))
    if ap is None:
        ap2, dp2 = readings(text, lang(code))
        ap, dp = ap2, (dp if dp is not None else dp2)
    if ap is None or not 3 <= ap <= 97:
        return None
    if dp is not None and (ap + dp > 101.5 or not 2 <= dp <= 97):
        dp = None
    end = item["pub"] - datetime.timedelta(days=4)
    if end.isoformat() < since:
        return None
    return {"date": end.isoformat(), "firm": firm, "approve": ap, "disapprove": dp,
            "approx": True, "source": item["link"], "origin": "news"}


def tracked_in(text):
    return {c for c, v in LEADERS.items() if has_name(text, v[3])}


def is_multi(item):
    text = f"{item['title']}. {item['desc']}"
    return len(tracked_in(item["title"])) > 1 or (
        re.search(r"morning consult|eurotrack|national leaders in europe|european leaders|leaders in europe|"
                  r"world leaders|global leader", text, re.I) and len(tracked_in(text)) > 1)


def parse_multi(item):
    """Ranking articles: each leader gets the first percentage after the name, before any other name."""
    title = re.sub(r"\s+-\s+[^-]{2,60}$", "", item["title"])
    text = f"{title}. {item['desc']}"
    if re.search(r"morning consult", text, re.I):
        firm = "Morning Consult"
    elif re.search(r"yougov", text, re.I):
        firm = "YouGov EuroTrack (favourability)"
    else:
        return []
    names = {c: v[3] for c, v in LEADERS.items()}
    figs = ls.credit_figures(text, names, STOPPERS, win=50, need_pct=True)
    end = item["pub"] - datetime.timedelta(days=4)
    out = []
    for c, v in figs.items():
        if end.isoformat() >= LEADERS[c][4]:
            out.append((c, {"date": end.isoformat(), "firm": firm, "approve": v, "disapprove": None,
                            "approx": True, "source": item["link"], "origin": "news-ranking"}))
    return out


# ------------------------------------------------------------------------------------- Wikipedia
# (page, how): "leader" = columns grouped under the leader's name (Pos./Neg. or Approve/Disapprove);
# a regex = the section heading the table sits under, with plain Approve/Disapprove columns.
WIKI = {
    "GB": [("Leadership_approval_opinion_polling_for_the_next_United_Kingdom_general_election", "leader")],
    "FR": [("Opinion_polling_on_the_Emmanuel_Macron_presidency", "leader")],
    "PL": [("Opinion_polling_on_the_incumbent_Tusk_government", r"^Prime Minister Donald Tusk")],
    "IT": [("Opinion_polling_for_the_next_Italian_general_election", r"^Government approval rating$")],
    "PT": [("Opinion_polling_for_the_next_Portuguese_legislative_election", r"^Cabinet approval")],
}
EN_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _headings(tbl):
    """Texts of the nearest h2, h3 and h4 above a table (the API wraps headings in div.mw-heading)."""
    out, rank = {}, {"h2": 2, "h3": 3, "h4": 4}
    top = 5
    for h in tbl.find_all_previous(["h2", "h3", "h4"]):
        if rank[h.name] < top:                     # only headings that enclose the table
            out[h.name] = h.get_text(" ", strip=True).replace("[edit]", "").strip()
            top = rank[h.name]
        if top == 2:
            break
    return out


def _date(txt, year_hint, today):
    hits = re.findall(r"(\d{1,2})\s*([A-Za-z]{3})[a-z]*\.?\s*(\d{4})?", txt)
    if not hits:
        return None
    day, mon, yr = hits[-1]
    m = EN_MON.get(mon.lower())
    if not m:
        return None
    y = int(yr) if yr else (year_hint or today.year)
    try:
        return datetime.date(y, m, min(int(day), 28 if m == 2 else 30 if m in (4, 6, 9, 11) else 31))
    except ValueError:
        return None


def parse_wiki(html_text, leader, how, today=None):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_text, "lxml")
    today = today or datetime.date.today()
    sur = ls._fold(leader.split()[-1])
    out = []
    for tbl in soup.select("table.wikitable"):
        hd = _headings(tbl)
        if how != "leader" and not any(re.search(how, v) for v in hd.values()):
            continue
        g = ls._grid(tbl)
        heads = [r for h, r in g if h]
        if not heads:
            continue
        width = max(len(r) for r in heads)
        labels = [" ".join(ls._fold(r[i]) for r in heads if i < len(r)) for i in range(width)]

        def col(pat, need=None, avoid=None):
            for i, l in enumerate(labels):
                if re.search(pat, l) and (need is None or need in l) and not (avoid and re.search(avoid, l)):
                    return i
            return None
        need = sur if how == "leader" else None
        if how == "leader" and not any(sur in l for l in labels):
            continue
        ia = col(r"\bpos\b|\bapprove", need, r"disapprove")
        idis = col(r"\bneg\b|disapprove", need)
        ifirm = col(r"pollster|polling firm")
        idate = col(r"date|fieldwork")
        if None in (ia, ifirm, idate):
            continue
        year_hint = None
        for v in (hd.get("h4"), hd.get("h3"), hd.get("h2")):
            if v and re.fullmatch(r"\d{4}", v):
                year_hint = int(v)
                break
        prev = None
        for h, cells in g:
            if h or len(cells) <= max(ia, ifirm, idate, idis or 0):
                continue
            a, d = ls._num(cells[ia]), (ls._num(cells[idis]) if idis is not None else None)
            dt = _date(cells[idate], year_hint, today)
            if dt is None or not ls._ok(a, d):
                continue
            if prev and not re.search(r"\d{4}", cells[idate]) and dt > prev + datetime.timedelta(days=40):
                dt = dt.replace(year=dt.year - 1)          # rows run newest first within a year table
            prev = dt
            if dt > today:
                continue
            firm = re.sub(r"\s*/.*$", "", cells[ifirm]).strip()      # "United Surveys / WP.pl" -> pollster
            out.append({"date": dt.isoformat(), "firm": firm, "approve": a, "disapprove": d,
                        "approx": False, "origin": "wikipedia"})
    return out


def src_wikipedia(code, log):
    polls = []
    for page, how in WIKI.get(code, []):
        try:
            rows = parse_wiki(ls._wiki_html(page), LEADERS[code][1], how)
        except Exception as e:
            log.append(f"  · {code} Wikipedia {page}: {e}")
            continue
        for r in rows:
            r["source"] = "https://en.wikipedia.org/wiki/" + page
        polls += rows
        log.append(f"  · {code} Wikipedia {page}: {len(rows)} rows")
    return polls


# ----------------------------------------------------------------------- pollsters' own releases
DE_MON = ["januar", "februar", "maerz", "april", "mai", "juni", "juli", "august", "september", "oktober",
          "november", "dezember"]
DE_MON_TXT = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
              "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}


def src_deutschlandtrend(since, have, log):
    """ARD-DeutschlandTrend monthly pages (infratest dimap): share satisfied with the Chancellor."""
    out = []
    d = datetime.date.fromisoformat(since).replace(day=1)
    today = datetime.date.today()
    while d <= today:
        url = f"https://www.infratest-dimap.de/umfragen-analysen/bundesweit/ard-deutschlandtrend/{d.year}/{DE_MON[d.month - 1]}/"
        nxt = (d + datetime.timedelta(days=32)).replace(day=1)
        if url in have:
            d = nxt
            continue
        try:
            from bs4 import BeautifulSoup
            txt = BeautifulSoup(ls._get(url), "lxml").get_text(" ", strip=True)
        except Exception as e:
            log.append(f"  · DeutschlandTrend {d:%Y-%m}: {e}")
            d = nxt
            continue
        m = re.search(r"(?:Leistungen|Arbeit) (?:des|von) (?:Bundes)?[Kk]anzlers?(?: Merz| Friedrich Merz)?[^.]{0,40}?"
                      r"(\d{1,2}) Prozent", txt) or \
            re.search(r"Bundeskanzler(?:s)?(?: Friedrich)? Merz[^.]{0,80}?(\d{1,2}) Prozent[^.]{0,30}zufrieden", txt)
        f = re.search(r"(\d{1,2})\.\s*(?:[A-Za-zä]+\s+)?bis\s+(\d{1,2})\.\s*([A-Za-zä]+)\s+(\d{4})", txt)
        if m:
            end = d.replace(day=15)
            if f and f.group(3).lower() in DE_MON_TXT:
                try:
                    end = datetime.date(int(f.group(4)), DE_MON_TXT[f.group(3).lower()], int(f.group(2)))
                except ValueError:
                    pass
            a = float(m.group(1))
            if ls._ok(a, None) and end.isoformat() >= since:
                out.append({"date": end.isoformat(), "firm": "Infratest dimap", "approve": a, "disapprove": None,
                            "approx": not bool(f), "source": url, "origin": "pollster"})
        d = nxt
        time.sleep(0.3)
    log.append(f"  · DeutschlandTrend: {len(out)} new monthly readings")
    return out


YG_COUNTRY = {"united kingdom": "GB", "britain": "GB", "france": "FR", "germany": "DE", "italy": "IT",
              "spain": "ES", "denmark": "DK"}


def src_eurotrack(have, log):
    """YouGov EuroTrack, 'How popular are national leaders in Europe? <Month YYYY>': favourable and
    unfavourable shares for each country's leader. Articles are found through Google News."""
    items = read_rss(rss_url('"how popular are national leaders in Europe"', "when:400d", "en-GB"))
    out, seen = [], set()
    for it in items:
        m = re.search(r"national leaders in Europe\?\s*([A-Za-z]+)\s+(\d{4})", it["title"])
        if not m or m.group(0) in seen:
            continue
        seen.add(m.group(0))
        mon = EN_MON.get(m.group(1).lower()[:3])
        if not mon:
            continue
        date = datetime.date(int(m.group(2)), mon, 22)
        try:
            from bs4 import BeautifulSoup
            import requests
            r = requests.get(it["link"], headers=ls.UA, timeout=40, allow_redirects=True)
            txt = BeautifulSoup(r.text, "lxml").get_text(" ", strip=True)
        except Exception as e:
            log.append(f"  · EuroTrack {m.group(1)} {m.group(2)}: {e}")
            continue
        n = 0
        for code, v in LEADERS.items():
            if code not in YG_COUNTRY.values():
                continue
            sn = re.escape(v[1].split()[-1])
            g = re.search(sn + r"[^.%]{0,120}?(\d{1,2})%\s*(?:have a |of [^.]{0,20})?favou?rable[^.%]{0,60}?(\d{1,2})%", txt)
            if g and date.isoformat() >= v[4]:
                a, d = float(g.group(1)), float(g.group(2))
                if ls._ok(a, d):
                    out.append((code, {"date": date.isoformat(), "firm": "YouGov EuroTrack (favourability)",
                                       "approve": a, "disapprove": d, "approx": True, "source": it["link"],
                                       "origin": "pollster"}))
                    n += 1
        log.append(f"  · EuroTrack {m.group(1)} {m.group(2)}: {n} leaders")
    return out


def _safe(fn, label, log, *a):
    try:
        return fn(*a)
    except Exception as e:
        log.append(f"  ! {label} crashed: {type(e).__name__}: {e}")
        return []


# ------------------------------------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    dry, rebuild = "--dry-run" in args, "--rebuild" in args
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"countries": {}}
    store, log = {}, []
    for code, (country, leader, office, _, since, _, _) in LEADERS.items():
        blk = data["countries"].setdefault(code, {"polls": []})
        if blk.get("leader") and blk["leader"] != leader:
            blk["polls"] = []                         # new head of government: fresh series
        blk.update({"country": country, "leader": leader, "office": office, "since": since})
        store[code] = [p for p in blk["polls"] if p["date"] >= since and p.get("origin") != "manual"]
    for code in list(data["countries"]):
        if code not in LEADERS:
            del data["countries"][code]

    # hand-verified rows (europe_seed.json) replace scraped copies of the same poll
    if SEED.exists():
        seed = json.loads(SEED.read_text(encoding="utf-8")).get("countries", {})
        for code, rows in seed.items():
            if code not in store:
                continue
            for p in rows:
                if p["date"] < LEADERS[code][4]:
                    continue
                fk = re.sub(r"[^a-z0-9]", "", key(p["firm"]))
                t = datetime.date.fromisoformat(p["date"])
                store[code] = [q for q in store[code] if q.get("origin") == "manual" or not (
                    (q["firm"] == UNNAMED or re.sub(r"[^a-z0-9]", "", key(q["firm"])) == fk) and
                    abs(q["approve"] - p["approve"]) <= 1.5 and
                    abs((datetime.date.fromisoformat(q["date"]) - t).days) <= 14)]
                store[code].append(dict(p, origin="manual"))

    added = 0
    if not rebuild:
        have = {p.get("source") for v in store.values() for p in v}
        for code in LEADERS:
            for p in _safe(src_wikipedia, f"{code} Wikipedia", log, code, log):
                if p["date"] >= LEADERS[code][4]:
                    added += add(code, p, store, log)
        for p in _safe(src_deutschlandtrend, "DeutschlandTrend", log, LEADERS["DE"][4], have, log):
            added += add("DE", p, store, log)
        for code, p in _safe(src_eurotrack, "EuroTrack", log, have, log):
            added += add(code, p, store, log)

        pool, seen = [], set()

        def take(items):
            for it in items:
                k = it["link"] or it["title"]
                if k not in seen:
                    seen.add(k)
                    pool.append(it)
        today = datetime.date.today()
        for code in LEADERS:
            take(fetch(code))
            if len(store[code]) < BACKFILL_MIN:       # thin country: read two years, two months at a time
                start = max(datetime.date.fromisoformat(LEADERS[code][4]), today - datetime.timedelta(days=730))
                a, n0 = start, len(pool)
                while a < today:
                    b = min(a + datetime.timedelta(days=61), today + datetime.timedelta(days=1))
                    take(fetch(code, f"after:{a.isoformat()}+before:{b.isoformat()}"))
                    a = b
                log.append(f"  · backfill Google News {code}: {len(pool) - n0} new headlines since {start}")
        for q, hl in RANK_QUERIES:
            take(read_rss(rss_url(q, "when:365d", hl)))
        log.append(f"  · Google News: {len(pool)} unique articles read")
        for it in sorted(pool, key=lambda x: x["pub"]):
            if is_multi(it):
                for code, p in parse_multi(it):
                    added += add(code, p, store, log)
                continue
            for code in LEADERS:
                p = parse_item(it, code)
                if p:
                    added += add(code, p, store, log)

    print("\n".join(log))
    from collections import Counter
    rep = [f"run {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d %H:%M} UTC · {added} new poll(s)", ""]
    for code, polls in store.items():
        c = Counter(p.get("origin", "?") for p in polls)
        rep.append(f"{code} {LEADERS[code][0]:<15} {len(polls):>3} polls  " +
                   ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    rep += ["", "log:"] + [l for l in log if "query failed" not in l][:400]
    if not dry:
        LOG.write_text("\n".join(rep) + "\n", encoding="utf-8")

    for code, polls in store.items():
        polls.sort(key=lambda p: p["date"])
        blk = data["countries"][code]
        blk["polls"] = polls
        blk["avg"] = {"approve": average(polls, "approve"), "disapprove": average(polls, "disapprove")}
    data["updated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["method"] = ("Wikipedia poll tables, pollster releases (ARD-DeutschlandTrend, YouGov EuroTrack), press "
                      "articles in each country's language and multi-country rankings (Morning Consult, YouGov); "
                      "Gaussian-kernel average, 21-day bandwidth, firm-crowding weights.")
    print(f"{added} new poll(s).")
    if not dry:
        OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
