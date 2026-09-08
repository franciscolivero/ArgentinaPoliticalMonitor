#!/usr/bin/env python3
"""
fetch_encuestar_espacio.py — snapshot EncuestAR's NATIONAL VOTE-BY-SPACE series and write it
into espacio_history.json, which the dashboard reads for the "by space" and "LLA+PRO alliance"
charts.

Sibling of fetch_encuestar.py (ballotage) and fetch_encuestar_firstround.py. Those scrape the
head-to-head rows; this one reads the per-space race at
    https://encuestar.netlify.app/carrera/voto-nacional-espacio/
where every poll is already normalised by the aggregator to seven canonical spaces plus the
"Alianza LLA+PRO" line (which is the day-by-day SUM of the LLA and PRO averages — a sum, not a
measured coalition). We consume that normalised series rather than re-deriving it from each
pollster's idiosyncratic categories.

The page renders every point as a tooltip string of the exact form
    <Consultora> — <Espacio>: <valor>% (fin de campo <YYYY-MM-DD>)
so we regex those out of the served HTML directly, which is robust to the surrounding markup
(the same string turns up whether it sits in an SVG <title>, an aria-label or a text node).
It also carries a headline block of current averages, which we keep as a cross-check snapshot.

For each space we build a per-poll series plus a Gaussian-smoothed poll-average (same kernel as
the other scrapers) and write espacio_history.json. Nothing is invented: if the fetch or parse
fails the file is left untouched; if too few points match it is left untouched and we exit 0
(a normal "nothing to update" day, not a workflow failure).

Usage:
    pip install requests beautifulsoup4 lxml numpy
    python fetch_encuestar_espacio.py                    # live scrape -> espacio_history.json
    python fetch_encuestar_espacio.py --list-espacios    # just print the spaces + point counts
    python fetch_encuestar_espacio.py --from-file p.txt   # parse a saved copy of the page
    python fetch_encuestar_espacio.py --out other.json    # write somewhere else (testing)
"""

import re, sys, json, datetime, pathlib
from collections import Counter, defaultdict

SRC_URL = "https://encuestar.netlify.app/carrera/voto-nacional-espacio/"
OUT = "espacio_history.json"

# The seven canonical spaces the aggregator normalises to. "Alianza LLA+PRO" is its day-by-day
# sum of LLA and PRO (a sum, not a measurement). Order here is the display order we prefer.
SPACES = ["La Libertad Avanza", "Peronismo", "PRO", "Izquierda",
          "Sin definir", "Resto", "Alianza LLA+PRO"]
ALLIANCE = "Alianza LLA+PRO"

# One tooltip point: "<Consultora> — <Espacio>: <valor>% (fin de campo <YYYY-MM-DD>)".
# The character classes stop at '<'/'>' so a match never crosses an HTML tag boundary.
POINT_RE = re.compile(
    r"([^<>—]+?)\s*—\s*([^<>:]+?):\s*([\d]+(?:[.,]\d+)?)\s*%\s*"
    r"\(fin de campo\s*(\d{4}-\d{2}-\d{2})\)")

# Headline current-average block: "<Espacio><valor>%" (no separator on the page).
HEAD_RE = re.compile(
    r"(Alianza LLA\+PRO|La Libertad Avanza|Peronismo|Sin definir|Izquierda|Resto|PRO)\s*"
    r"([\d]+(?:[.,]\d+)?)\s*%")

BW_DAYS   = 16    # Gaussian kernel bandwidth for the smoothed average (matches sibling scrapers)
STEP_DAYS = 7     # one average point per week


def fetch_html():
    import requests
    r = requests.get(SRC_URL, headers={"User-Agent": "arg2027-monitor/1.0 (+github-pages)"},
                     timeout=40)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def _num(s):
    s = s.strip()
    return float(s.replace(".", "").replace(",", ".")) if ("," in s) else float(s)


def parse_points(text):
    """Return {space: [(date, firm, value), ...]} from every tooltip string in the page."""
    series = defaultdict(list)
    for firm, space, val, date in POINT_RE.findall(text):
        space = re.sub(r"\s+", " ", space).strip()
        firm  = re.sub(r"\s+", " ", firm).strip(" ·-–—")
        if space not in SPACES:
            continue
        try:
            datetime.date.fromisoformat(date)
            v = _num(val)
        except (ValueError, TypeError):
            continue
        series[space].append((date, firm, v))
    return series


def parse_headline(text):
    """Return {space: value} from the current-average block, best-effort."""
    out = {}
    for space, val in HEAD_RE.findall(text):
        out.setdefault(space.strip(), _num(val))   # first hit wins (the summary block)
    return out


def smoothed_average(points):
    """points: list of (date, firm, value). Returns {dates, values} on a weekly grid."""
    import numpy as np
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) < 2:
        return {"dates": [], "values": []}
    d0 = datetime.date.fromisoformat(pts[0][0])
    d1 = datetime.date.fromisoformat(pts[-1][0])
    xs = np.array([(datetime.date.fromisoformat(p[0]) - d0).days for p in pts], float)
    ys = np.array([p[2] for p in pts], float)
    grid, d = [], d0
    while d <= d1:
        grid.append(d); d += datetime.timedelta(days=STEP_DAYS)
    if grid[-1] != d1:
        grid.append(d1)
    dates, vals = [], []
    for g in grid:
        w = np.exp(-0.5 * ((xs - (g - d0).days) / BW_DAYS) ** 2)
        s = w.sum()
        if s <= 0:
            continue
        dates.append(g.isoformat())
        vals.append(round(float((w * ys).sum() / s), 1))
    return {"dates": dates, "values": vals}


def build(series, headline):
    """Assemble the espacio_history.json payload from parsed per-space points."""
    spaces_out = {}
    for space in SPACES:
        pts = series.get(space, [])
        if not pts:
            continue
        # de-dupe on (firm, date), keep last
        seen, uniq = set(), []
        for date, firm, v in pts:
            key = (firm.lower(), date)
            if key in seen:
                continue
            seen.add(key); uniq.append((date, firm, v))
        uniq.sort(key=lambda p: p[0])
        spaces_out[space] = {
            "polls": [{"date": d, "firm": f, "value": v} for d, f, v in uniq],
            "avg": smoothed_average(uniq),
        }
    return {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": SRC_URL,
        "race": "Intención de voto nacional por espacio",
        "attribution": "EncuestAR — promedio de encuestas de terceros, con atribución",
        "order": [s for s in SPACES if s in spaces_out],
        "headline": headline,
        "spaces": spaces_out,
    }


def get_text(args):
    if "--from-file" in args:
        return pathlib.Path(args[args.index("--from-file") + 1]).read_text(encoding="utf-8")
    return fetch_html()


def main():
    args = sys.argv[1:]
    out_path = args[args.index("--out") + 1] if "--out" in args else OUT

    try:
        text = get_text(args)
    except Exception as e:
        print(f"  ! fetch/parse failed ({e}); leaving {out_path} untouched.", file=sys.stderr)
        sys.exit(1)

    series = parse_points(text)
    headline = parse_headline(text)

    if "--list-espacios" in args:
        print("Spaces parsed (points):")
        for s in SPACES:
            print(f"  {len(series.get(s, [])):>3}  {s}")
        tot = sum(len(v) for v in series.values())
        print(f"\nTotal points: {tot}. Headline: "
              + ", ".join(f"{k} {v}" for k, v in headline.items()))
        return

    total = sum(len(v) for v in series.values())
    if total < 3:
        print(f"  · only {total} point(s) parsed; nothing to update, leaving {out_path} "
              f"untouched. Run --list-espacios to inspect.", file=sys.stderr)
        return

    data = build(series, headline)
    pathlib.Path(out_path).write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def tip(space):
        a = data["spaces"].get(space, {}).get("avg", {})
        return f"{a['values'][-1]}%" if a.get("values") else "n/a"
    print(f"wrote {out_path}: {len(data['spaces'])} spaces, {total} points. "
          f"Latest smoothed — LLA {tip('La Libertad Avanza')}, "
          f"Peronismo {tip('Peronismo')}, Alianza {tip(ALLIANCE)}")


if __name__ == "__main__":
    main()
