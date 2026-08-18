#!/usr/bin/env python3
"""
update_data.py — monthly refresh for the Argentina 2027 Forecast Monitor.

What it does
------------
1. ICG (Di Tella)      ... fully automatic. Scrapes the monthly index list off the
                           UTDT page and rebuilds the full time series.
2. Head-to-head        ... NO LONGER handled here. The Milei-vs-Kicillof runoff series
                           (Chart 02) is scraped daily by fetch_encuestar.py into
                           encuestas_history.json. The headToHead2026 block below is kept
                           only as the dashboard's offline fallback; this script leaves it
                           untouched.
3. ESPOP (UdeSA)       ... assisted. ESPOP figures live inside monthly PDFs / press
                           notes, so the script asks you to confirm/add the latest
                           approval + image points (press Enter to keep what's there).
   NOTE: head-to-head polls (headToHead2026.polls), the LOESS average, and the
   ICG sub-indices are curated by hand from the published poll table / monthly PDF.
4. Writes data.json and re-embeds the data block into
   argentina-2027-monitor.html between the DATA_START / DATA_END markers.

Usage
-----
    pip install requests beautifulsoup4
    python update_data.py                 # interactive
    python update_data.py --icg-only      # only refresh the ICG series, no prompts

Network note: needs outbound access to utdt.edu and wikipedia.org.
No value is ever invented — anything the script cannot fetch is left untouched.
"""

import re, json, sys, datetime, pathlib

HERE = pathlib.Path(__file__).parent
DATA_JSON = HERE / "data.json"
HTML_FILE = HERE / "index.html"
if not HTML_FILE.exists():
    HTML_FILE = HERE / "argentina-2027-monitor.html"

ICG_URL = "https://www.utdt.edu//ver_contenido.php?id_contenido=1439&id_item_menu=2964"
WIKI_URL = "https://en.wikipedia.org/wiki/2027_Argentine_general_election"

ES_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _get(url):
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (compatible; arg2027-monitor/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


# ---------------------------------------------------------------- ICG
def fetch_icg(existing):
    """Scrape the monthly ICG values off the Di Tella page.

    The page lists each month as e.g. 'El ICG de mayo fue de 1,99 puntos' or
    'La medición de junio del ICG fue de 2,34 puntos'. We capture (month, value)
    and infer the year from the running sequence (newest first on the page).
    """
    try:
        html = _get(ICG_URL)
    except Exception as e:
        print(f"  ! ICG fetch failed ({e}); keeping existing ICG series.")
        return existing["icg"]["index"]

    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("\xa0", " ")
    # match: <month> ... de  X,XX  puntos
    pat = re.compile(
        r"(?:ICG de|medici[oó]n de)\s+([A-Za-zñ]+)\b[^.]*?de\s+(\d,\d{1,2})\s*puntos",
        re.IGNORECASE,
    )
    found = []  # (month_num, value) in page order (newest -> oldest)
    for m in pat.finditer(text):
        mon = ES_MONTHS.get(m.group(1).lower())
        if not mon:
            continue
        val = float(m.group(2).replace(",", "."))
        found.append((mon, val))

    if len(found) < 6:
        print(f"  ! ICG parse looked thin ({len(found)} pts); keeping existing series.")
        return existing["icg"]["index"]

    # assign years walking newest->oldest; year rolls back when month increases
    series = []
    today = datetime.date.today()
    year = today.year
    prev_mon = None
    for mon, val in found:
        if prev_mon is not None and mon > prev_mon:
            year -= 1
        series.append({"date": f"{year:04d}-{mon:02d}-01", "value": round(val, 2)})
        prev_mon = mon

    series = sorted({s["date"]: s for s in series}.values(), key=lambda s: s["date"])
    print(f"  ✓ ICG: {len(series)} monthly points "
          f"({series[0]['date'][:7]} → {series[-1]['date'][:7]}), "
          f"latest = {series[-1]['value']}")
    return series


# ---------------------------------------------------------------- head-to-head (superseded)
def fetch_wikipedia(existing):
    """No-op kept for backwards compatibility.

    The head-to-head runoff series now comes from fetch_encuestar.py (daily) into
    encuestas_history.json. The embedded headToHead2026 block is only the dashboard's
    offline fallback, so this simply preserves whatever is already in data.json.
    """
    print("  · head-to-head is scraped by fetch_encuestar.py now; keeping the embedded "
          "fallback as-is.")
    return existing.get("headToHead2026", {}), existing.get("votingSnapshot", {})


# ---------------------------------------------------------------- ESPOP (assisted)
def ask_float(prompt, current):
    raw = input(f"  {prompt} [{current}]: ").strip()
    if raw == "":
        return current
    try:
        return float(raw)
    except ValueError:
        print("    (not a number — keeping current)")
        return current


def refresh_espop(existing, interactive):
    espop = existing["espop"]
    if not interactive:
        return espop
    print("\n  ESPOP latest wave — press Enter to keep the stored value.")
    ym = input("  New wave year-month (YYYY-MM, blank to skip): ").strip()
    if ym:
        date = ym + "-01"
        appr = ask_float("approval (gestión) %", "")
        disa = ask_float("disapproval %", "")
        img = ask_float("positive image %", "")
        if appr != "":
            espop["approval"].append({"date": date, "value": appr,
                                      "disapproval": (disa if disa != "" else None),
                                      "firm": "ESPOP (UdeSA)"})
            espop["approval"].sort(key=lambda d: d["date"])
        if img != "":
            espop["image"].append({"date": date, "value": img, "firm": "ESPOP (UdeSA)"})
            espop["image"].sort(key=lambda d: d["date"])
        print("    ✓ ESPOP point added.")
    return espop


# ---------------------------------------------------------------- write-out
def embed_into_html(data):
    if not HTML_FILE.exists():
        print("  ! HTML file not found; wrote data.json only.")
        return
    html = HTML_FILE.read_text(encoding="utf-8")
    block = "\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    new = re.sub(
        r"(/\*\s*DATA_START\s*\*/).*?(/\*\s*DATA_END\s*\*/)",
        lambda m: m.group(1) + block + m.group(2),
        html, count=1, flags=re.S,
    )
    HTML_FILE.write_text(new, encoding="utf-8")
    print(f"  ✓ re-embedded data into {HTML_FILE.name}")


def main():
    interactive = "--icg-only" not in sys.argv
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))

    print("Refreshing Argentina 2027 monitor …")
    data["icg"]["index"] = fetch_icg(data)
    data["headToHead2026"], data["votingSnapshot"] = fetch_wikipedia(data)
    data["espop"] = refresh_espop(data, interactive)
    data["meta"]["updated"] = datetime.date.today().isoformat()

    DATA_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ wrote {DATA_JSON.name}")
    embed_into_html(data)
    print(f"\nDone. Updated as of {data['meta']['updated']}. "
          f"Open {HTML_FILE.name} in a browser.")


if __name__ == "__main__":
    main()
