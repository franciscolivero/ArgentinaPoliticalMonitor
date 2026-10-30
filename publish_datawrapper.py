#!/usr/bin/env python3
"""
publish_datawrapper.py — keeps one Datawrapper chart per monitor indicator up to date, so the
charts embedded in the Substack posts refresh on their own.

How it works
  * The first run creates each chart in your Datawrapper account (title, type, colours, source
    line) and stores its id in datawrapper_charts.json. Later runs never touch the styling, so
    anything you adjust by hand in Datawrapper's editor is kept: they only upload the new data
    and republish.
  * Embed in Substack: paste the chart's address  https://datawrapper.dwcdn.net/<ID>/  on its own
    line in the post editor. That address always points to the latest published version, so a
    post written today shows next week's data too. (Interactive on the web and in the app; email
    readers get a static image of the version current when the email went out.)
  * Needs a Datawrapper API token with chart read/write and publish rights, stored as the
    repository secret DATAWRAPPER_TOKEN. Without it the script only writes the CSVs to
    charts_data/ (useful to check or upload by hand) and exits.

Usage:
    python publish_datawrapper.py            # build tables, create/update/publish charts
    python publish_datawrapper.py --dry-run  # only write charts_data/*.csv
"""

import os, sys, json, pathlib
import post_data as P

HERE = pathlib.Path(__file__).parent
STATE = HERE / "datawrapper_charts.json"
OUTDIR = HERE / "charts_data"
API = "https://api.datawrapper.de/v3"
SOURCE = "Monitor Argentina 2027 · Francisco M. Olivero"

C = {"milei": "#6D3FD8", "kicillof": "#2A8BD0", "pro": "#C98A1B", "izq": "#C8443A", "gris": "#9AA5B1",
     "pos": "#24875A", "neg": "#C8443A", "ns": "#B7C1CC", "ink": "#0F2238", "celeste": "#75AADB"}


STYLE_VERSION = 5   # bump to re-apply titles/colours to charts that already exist


def tint(hex_color, k=0.55):
    """Lighter version of a colour for the individual-poll dots."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02X}{:02X}{:02X}".format(*(round(c + (255 - c) * k) for c in (r, g, b)))


def lines_meta(colors, suffix="%", y_range=None, dots=True):
    """Smooth average lines in `colors`; with dots=True each series also gets a '<name> · encuestas'
    column drawn as dots only (invisible line), in a lighter shade of the same colour."""
    cols = dict(colors)
    lines = {k: {"symbols": {"enabled": False}, "directLabel": True} for k in colors}
    if dots:
        for k, c in colors.items():
            dk = P.dot_label(k)
            cols[dk] = tint(c)
            lines[dk] = {"width": "invisible", "directLabel": False, "colorKey": False,
                         "valueLabels": {"enabled": False},
                         "symbols": {"enabled": True, "on": "every", "style": "fill", "size": 4}}
    vis = {"custom-colors": cols, "lines": lines,
           "label-colors": True, "show-tooltips": True, "interpolation": "monotone-x",
           "y-grid": "on", "x-grid": "off"}
    if y_range:
        vis["custom-range-y"] = y_range
    return {"visualize": vis,
            "describe": {"source-name": SOURCE, "number-append": suffix, "number-format": "0.0"}}


def build_specs():
    """[(key, title, type, csv, metadata, post)] — post is 'ar' or 'latam'."""
    specs = []
    t, _ = P.runoff()
    specs.append(("ar-runoff", "Milei vs. Kicillof: intención de voto en un ballotage", "d3-lines", t,
                  lines_meta({"Milei": C["milei"], "Kicillof": C["kicillof"]}), "ar"))
    t, _ = P.espacio(["La Libertad Avanza", "Peronismo", "PRO", "Izquierda", "Sin definir"])
    specs.append(("ar-espacio", "Intención de voto nacional por espacio", "d3-lines", t,
                  lines_meta({"LLA": C["milei"], "Peronismo": C["kicillof"], "PRO": C["pro"],
                              "Izquierda": C["izq"], "Indecisos": C["gris"]}, y_range=["0", ""]), "ar"))
    t, _ = P.espacio(["Alianza LLA+PRO", "Peronismo", "Izquierda", "Sin definir"])
    specs.append(("ar-alianza", "Si La Libertad Avanza y el PRO fueran juntos", "d3-lines", t,
                  lines_meta({"LLA + PRO": C["milei"], "Peronismo": C["kicillof"], "Izquierda": C["izq"],
                              "Indecisos": C["gris"]}, y_range=["0", ""]), "ar"))
    t, _ = P.approval()
    specs.append(("ar-approval", "Aprobación de Javier Milei, promedio de encuestas", "d3-lines", t,
                  lines_meta({"Aprueba": C["pos"], "Desaprueba": C["neg"]}, y_range=["25", "70"]), "ar"))
    t1, t2, icg = P.icg()
    specs.append(("ar-icg", "Índice de Confianza en el Gobierno (UTDT)", "d3-lines", t1,
                  lines_meta({"ICG": C["ink"]}, suffix="", y_range=["1.5", "3"], dots=False), "ar"))
    specs.append(("ar-icg-comp", "Componentes del ICG", "d3-lines", t2,
                  lines_meta({"Evaluación general": C["neg"], "Capacidad para resolver problemas": "#2F6FA8",
                              "Honestidad": C["pos"], "Eficiencia del gasto": C["pro"],
                              "Preocupación por el interés general": C["milei"]}, suffix="", y_range=["1.3", "3.1"], dots=False),
                  "ar"))
    t, _, _ = P.icg_by_president()
    pres_cols = {"N. Kirchner": "#8C8C8C", "CFK I": "#27AAE1", "CFK II": "#3E78B2", "Macri": "#F2B600",
                 "A. Fernández": "#1F3C5F", "Milei": C["milei"]}
    specs.append(("ar-icg-pres", "Confianza en el gobierno por presidente, según mes de gestión", "d3-lines", t,
                  lines_meta(pres_cols, suffix="", y_range=["0", "4"], dots=False), "ar"))
    latest_icg = icg["index"][-1]["value"]
    t, _ = P.icg_votes(latest_icg)
    specs.append(("ar-icg-votes", "Confianza en el gobierno y voto del oficialismo, 2005–2025", "d3-scatter-plot", t,
                  {"axes": {"x": "Voto implícito (ICG×20)", "y": "Voto del oficialismo", "labels": "Año",
                            "color": "Tipo"},
                   "visualize": {"regression": True, "show-labels": True, "x-grid": "on", "y-grid": "on",
                                 "color-category": {"map": {"Presidencial": "#2F6FA8", "Legislativa": C["pro"]}}},
                   "describe": {"source-name": SOURCE + " · UTDT, resultados oficiales"}}, "ar"))
    t, _ = P.polymarket()
    specs.append(("ar-polymarket", "Polymarket: probabilidad de ganar la presidencia en 2027", "d3-lines", t,
                  lines_meta({"Javier Milei": C["milei"], "Axel Kicillof": C["kicillof"], "Sergio Massa": C["gris"],
                              "Patricia Bullrich": "#A88BE6"}, dots=False), "ar"))
    t, _, month = P.governors()
    specs.append(("ar-governors", f"Imagen de los gobernadores, {month or ''}".strip(", "), "d3-bars-stacked", t,
                  {"visualize": {"custom-colors": {"Positiva": C["pos"], "Negativa": C["neg"], "NS/NC": C["ns"]},
                                 "show-values": True, "sort-bars": False},
                   "describe": {"source-name": SOURCE + " · CB Consultora", "number-append": "%"}}, "ar"))
    t, F = P.forecast_ar()
    if t:
        specs.append(("ar-forecast", "Chance de que Milei gane la elección presidencial de 2027", "d3-lines", t,
                      {"visualize": {"custom-colors": {"Modelo de simulación": C["milei"], "Modelo combinado": "#2F6FA8",
                                                       "Polymarket": C["gris"]},
                                     "lines": {"Modelo de simulación": {"width": "style3", "directLabel": True},
                                               "Modelo combinado": {"width": "style1", "directLabel": True},
                                               "Polymarket": {"width": "style0", "dash": "style2", "directLabel": False}},
                                     "custom-range-y": ["0", "100"], "y-grid": "on", "label-colors": True,
                                     "show-tooltips": True},
                       "describe": {"source-name": SOURCE + " · modelos de simulación y combinado", "number-append": "%",
                                    "number-format": "0"}}, "ar"))
    t, F = P.forecast_latam()
    if t:
        specs.append(("latam-forecast", "Chance de que el oficialismo gane la próxima elección presidencial",
                      "d3-bars", t,
                      {"visualize": {"custom-colors": {}, "show-values": True, "sort-bars": True,
                                     "base-color": C["celeste"]},
                       "describe": {"source-name": SOURCE + " · modelo entrenado con Latinobarómetro",
                                    "number-append": "%", "number-format": "0"}}, "latam"))
    rows = P.latam()
    specs.append(("latam-ranking", "Aprobación presidencial en América Latina", "d3-bars", P.latam_ranking_table(rows),
                  {"visualize": {"custom-colors": {f"{r['president']} ({r['country']})":
                                                   (C["ink"] if r["code"] == "AR" else C["celeste"]) for r in rows},
                                 "show-values": True, "sort-bars": True},
                   "describe": {"source-name": SOURCE, "number-append": "%", "number-format": "0"}}, "latam"))
    for r in rows:
        if r["code"] == "AR" or not r["avg"]:
            continue
        specs.append((f"latam-{r['code'].lower()}", f"{r['country']}: aprobación de {r['president']}", "d3-lines",
                      P.latam_country_table(r),
                      lines_meta({"Aprueba": C["pos"], "Desaprueba": C["neg"]}, y_range=["0", "100"]), "latam"))
    return specs


def api(method, path, token, **kw):
    import requests
    h = {"Authorization": f"Bearer {token}"}
    h.update(kw.pop("headers", {}))
    r = requests.request(method, API + path, headers=h, timeout=60, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:300]}")
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else None


def main():
    dry = "--dry-run" in sys.argv
    OUTDIR.mkdir(exist_ok=True)
    specs = build_specs()
    for key, title, typ, csv_text, meta, post in specs:
        (OUTDIR / f"{key}.csv").write_text(csv_text, encoding="utf-8")
    print(f"wrote {len(specs)} tables to {OUTDIR.name}/")
    token = os.environ.get("DATAWRAPPER_TOKEN")
    if dry or not token:
        print("no DATAWRAPPER_TOKEN (or --dry-run): charts not published")
        return
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    report, ok = [], 0
    for key, title, typ, csv_text, meta, post in specs:
        st = state.get(key, {})
        try:
            cid = st.get("id")
            if cid:
                try:                                   # the chart may have been deleted by hand
                    api("GET", f"/charts/{cid}", token)
                except RuntimeError as e:
                    if " 404 " in str(e):
                        cid = None
                    else:
                        raise
            if not cid:
                cid = api("POST", "/charts", token, json={"title": title, "type": typ, "language": "es-ES",
                                                          "metadata": meta})["id"]
                st = {"id": cid, "title": title, "post": post, "style_version": STYLE_VERSION, "type": typ}
                report.append(f"  + created {key} → {cid}")
            if st.get("style_version", 1) < STYLE_VERSION or st.get("type") != typ:
                api("PATCH", f"/charts/{cid}", token, json={"title": title, "type": typ, "metadata": meta})
                st["style_version"], st["type"] = STYLE_VERSION, typ
                report.append(f"  ~ restyled {key}")
            state[key] = st
            api("PUT", f"/charts/{cid}/data", token, data=csv_text.encode("utf-8"),
                headers={"Content-Type": "text/csv; charset=utf-8"})
            api("POST", f"/charts/{cid}/publish", token)
            info = api("GET", f"/charts/{cid}", token) or {}
            public = info.get("publicUrl") or ""
            # publicUrl ends in /<version>/ ; the bare /<id>/ address always serves the latest one
            st["embed"] = f"https://datawrapper.dwcdn.net/{cid}/"
            st["public_url"] = public
            st["published"] = bool(public)
            ok += bool(public)
            report.append(f"  {'✓' if public else '?'} {key}: {public or 'publish returned no public URL'}")
        except Exception as e:
            st["published"] = False
            state[key] = st
            report.append(f"  ! {key}: {e}")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    report.insert(0, f"{ok}/{len(specs)} charts published")
    (HERE / "datawrapper_log.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()
