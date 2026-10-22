#!/usr/bin/env python3
"""
fetch_icg.py — keep the Di Tella ICG (headline + five components) up to date automatically.

Runs daily from .github/workflows/update-icg.yml. Each run:
  1. Reads the UTDT ICG page, collecting every month's headline value and the link to that
     month's PDF report.
  2. For every month (since Milei took office) that is missing in data.json -> icg.components,
     downloads that month's PDF and reads the five sub-indices out of its text.
     A month is only accepted if the five values average to the published headline ICG
     (the ICG is the simple mean of its five components), so a misread PDF can never slip in.
  3. Writes the headline series and the components into data.json, re-embeds data.json into
     index.html (offline fallback), and appends any new Milei month to Evolution_ICG.xlsx so
     the by-president charts move too.
Nothing is invented: whatever cannot be fetched or validated is left as it was.

Usage:
    pip install requests beautifulsoup4 lxml pypdf openpyxl
    python fetch_icg.py              # live
    python fetch_icg.py --dry-run    # print what would change
"""

import io, re, sys, json, datetime, pathlib, itertools

HERE = pathlib.Path(__file__).parent
DATA_JSON = HERE / "data.json"
HTML_FILE = HERE / "index.html"
XLSX_FILE = HERE / "Evolution_ICG.xlsx"
ICG_URL = "https://www.utdt.edu//ver_contenido.php?id_contenido=1439&id_item_menu=2964"
BASE = "https://www.utdt.edu/"
FIRST_MONTH = "2023-12-01"          # Milei month 0
UA = {"User-Agent": "Mozilla/5.0 (compatible; arg2027-monitor/1.0; +github-pages)"}

ES_MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
             "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
             "noviembre": 11, "diciembre": 12}

# data.json series keys, in order, with the Spanish label patterns used in the PDF
COMPONENTS = [
    ("General evaluation of government", r"evaluaci[oó]n\s+general"),
    ("Capacity to solve problems",       r"capacidad"),
    ("Honesty of officials",             r"honestidad"),
    ("Efficiency of public spending",    r"eficiencia"),
    ("Concern for the general interest", r"preocupaci[oó]n\s+por\s+el\s+inter[eé]s"),
]
VAL = re.compile(r"(?<![\d,.])([0-4][,.]\d{1,2})(?![\d])")


def get(url, binary=False):
    import requests
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    if binary:
        return r.content
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def num(s):
    return float(s.replace(",", "."))


# ------------------------------------------------------------------ index page
def parse_index(html):
    """Return {YYYY-MM-01: {"value": float|None, "pdf": url|None}}."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    out = {}

    def month_key(text):
        m = re.search(r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
                      r"octubre|noviembre|diciembre)\D{0,12}(20\d\d)", text, re.I)
        if not m:
            return None
        return f"{int(m.group(2)):04d}-{ES_MONTHS[m.group(1).lower()]:02d}-01"

    # PDF links: the label sits in the anchor or in the enclosing block
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download.php" not in href or ".pdf" not in href.lower():
            continue
        url = href if href.startswith("http") else BASE + href.lstrip("/")
        node, key = a, month_key(a.get_text(" "))
        while key is None and node is not None and node.name not in ("body", "html"):
            node = node.parent
            if node is not None and len(node.get_text(" ")) < 600:
                key = month_key(node.get_text(" "))
        if key and key not in out:
            out[key] = {"value": None, "pdf": url}

    # headline values: "El ICG de mayo fue de 1,99 puntos" (newest first, years inferred)
    text = re.sub(r"\s+", " ", soup.get_text(" "))
    pat = re.compile(r"(?:ICG de|medici[oó]n de)\s+([A-Za-zñ]+)\b[^.]*?de\s+(\d,\d{1,2})\s*puntos", re.I)
    found = [(ES_MONTHS[m.group(1).lower()], num(m.group(2)))
             for m in pat.finditer(text) if m.group(1).lower() in ES_MONTHS]
    if found:
        # anchor the year on the newest dated PDF if we have one, else today
        newest = max(out) if out else None
        year = int(newest[:4]) if newest else datetime.date.today().year
        if newest and found[0][0] > int(newest[5:7]):
            year -= 1
        prev = None
        for mon, val in found:
            if prev is not None and mon > prev:
                year -= 1
            k = f"{year:04d}-{mon:02d}-01"
            out.setdefault(k, {"value": None, "pdf": None})["value"] = round(val, 2)
            prev = mon
    return out


# ------------------------------------------------------------------ PDF components
def pdf_text(blob):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(blob))
    pages = [(p.extract_text() or "") for p in reader.pages[:6]]
    return re.sub(r"\s+", " ", " ".join(pages))


def parse_components(text, headline):
    """Pick the five sub-indices whose mean matches the published headline."""
    cands = []
    for _, pat in COMPONENTS:
        vals = []
        for m in re.finditer(pat, text, re.I):
            window = text[m.end(): m.end() + 160]
            for v in VAL.findall(window)[:3]:
                x = num(v)
                if 0.5 <= x <= 4.5 and x not in vals:
                    vals.append(x)
            if len(vals) >= 4:
                break
        if not vals:
            return None
        cands.append(vals[:4])
    if headline is None:
        return None
    first = [c[0] for c in cands]
    if abs(sum(first) / 5 - headline) <= 0.006:
        return first
    hits = {combo for combo in itertools.product(*cands)
            if abs(sum(combo) / 5 - headline) <= 0.006}
    if len(hits) == 1:
        return list(hits.pop())
    # several fits: prefer reading the same column for every component (e.g. "prev | current")
    for j in range(4):
        if all(len(c) > j for c in cands):
            combo = tuple(c[j] for c in cands)
            if combo in hits:
                return list(combo)
    return None


# ------------------------------------------------------------------ writers
def embed_into_html(data):
    if not HTML_FILE.exists():
        return
    html = HTML_FILE.read_text(encoding="utf-8")
    block = "\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    new = re.sub(r"(/\*\s*DATA_START\s*\*/).*?(/\*\s*DATA_END\s*\*/)",
                 lambda m: m.group(1) + block + m.group(2), html, count=1, flags=re.S)
    HTML_FILE.write_text(new, encoding="utf-8")


def months_between(a, b):
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def update_xlsx(index_series, dry):
    """Fill Milei's months-in-office rows in Evolution_ICG.xlsx from the headline series."""
    try:
        import openpyxl
    except ImportError:
        return 0
    wb = openpyxl.load_workbook(XLSX_FILE)
    ws = wb.worksheets[0]
    start, cur = None, None
    rows = {}
    for i in range(2, ws.max_row + 1):
        name = ws.cell(i, 1).value
        if name:
            cur = str(name)
        if cur and "milei" in cur.lower():
            m = ws.cell(i, 2).value
            if m is not None:
                rows[int(m)] = i
    changed = 0
    for p in index_series:
        if p["date"] < FIRST_MONTH:
            continue
        k = months_between(FIRST_MONTH, p["date"])
        if k in rows and ws.cell(rows[k], 3).value in (None, ""):
            ws.cell(rows[k], 3, p["value"])
            changed += 1
    if changed and not dry:
        wb.save(XLSX_FILE)
    return changed


def main():
    dry = "--dry-run" in sys.argv
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    icg = data["icg"]
    try:
        months = parse_index(get(ICG_URL))
    except Exception as e:
        print(f"  ! UTDT page fetch failed ({e}); nothing changed.")
        return

    # headline series
    idx = {p["date"]: p for p in icg["index"]}
    new_heads = 0
    latest = max(idx) if idx else FIRST_MONTH
    for k, v in months.items():
        # only fill gaps; a month must carry a dated PDF link or be newer than what we hold,
        # so a mis-inferred year can never overwrite history
        if v["value"] is not None and k >= FIRST_MONTH and (v["pdf"] or k > latest):
            if k not in idx:
                idx[k] = {"date": k, "value": v["value"]}
                new_heads += 1
    icg["index"] = sorted(idx.values(), key=lambda p: p["date"])
    heads = {p["date"]: p["value"] for p in icg["index"]}

    # components
    comp = icg.setdefault("components", {"months": [], "series": {k: [] for k, _ in COMPONENTS}})
    have = dict(zip(comp["months"], zip(*[comp["series"][k] for k, _ in COMPONENTS]))) \
        if comp["months"] else {}
    added = []
    for k in sorted(months):
        if k < FIRST_MONTH or k in have or not months[k]["pdf"]:
            continue
        try:
            vals = parse_components(pdf_text(get(months[k]["pdf"], binary=True)), heads.get(k))
        except Exception as e:
            print(f"  · {k[:7]}: PDF unreadable ({e})")
            continue
        if vals:
            have[k] = tuple(round(v, 2) for v in vals)
            added.append(k)
            print(f"  ✓ {k[:7]} components: {have[k]}")
        else:
            print(f"  · {k[:7]}: components not validated against headline; skipped")
    order = sorted(have)
    comp["months"] = order
    comp["series"] = {name: [have[m][i] for m in order] for i, (name, _) in enumerate(COMPONENTS)}

    xl = update_xlsx(icg["index"], dry)
    print(f"ICG: {new_heads} headline change(s), {len(added)} component month(s), "
          f"{xl} xlsx cell(s). Latest {icg['index'][-1]}")
    if dry or not (new_heads or added or xl):
        return
    data["meta"]["updated"] = datetime.date.today().isoformat()
    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    embed_into_html(data)


if __name__ == "__main__":
    main()
