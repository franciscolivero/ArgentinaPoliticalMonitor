#!/usr/bin/env python3
"""
draft_posts.py — writes this week's two Substack drafts from the monitor's data:

    posts/AAAA-MM-DD-argentina.md   every indicator in the monitor, governors included
    posts/AAAA-MM-DD-latam.md       presidential ranking, then one chart + paragraph per country

Every number and every week-on-week / month-on-month change is filled in from the repo. Each chart
line is the Datawrapper address from datawrapper_charts.json: in Substack, paste it on its own line
and the live chart appears. The wording follows the author's house style (see the note above
argentina()): numbers and changes only, one interpretive sentence at most, link to the monitor.

Usage:  python draft_posts.py [--date AAAA-MM-DD]
"""

import sys, json, datetime, pathlib
import post_data as P

HERE = pathlib.Path(__file__).parent
STATE = HERE / "datawrapper_charts.json"
SIGN = "F.M.O."


def embed(key, state):
    s = state.get(key)
    if s and s.get("published") and s.get("embed"):
        return s["embed"]
    return f"[gráfico {key}: todavía no publicado en Datawrapper; ver datawrapper_log.txt]"


def trend_word(d, small=1.0):
    if d is None:
        return "sin comparación"
    if abs(d) < small:
        return "estable"
    return "en alza" if d > 0 else "en baja"


MONITOR_AR = "https://franciscolivero.github.io/ArgentinaPoliticalMonitor/index.html"
MONITOR_LATAM = "https://franciscolivero.github.io/ArgentinaPoliticalMonitor/latam.html"

# House style (from the author's edits to the first edition):
#   * plain title, no date in it and no "Actualizado…" line; the date goes in the sign-off
#   * state the number, the change and nothing else: no methodological caveats, no
#     rhetorical flourishes ("todavía no tiene dueño", "conviene leerlo como un techo")
#   * one interpretive sentence is allowed where the data invite it (a technical tie in the runoff)
#   * close with the link to the monitor and "F.M.O., <fecha>"


def argentina(today, state):
    L = []
    w = L.append
    t30 = P.ago(30, today)
    _, ap = P.approval()
    A = ap["avg"]
    a_now, d_now = A["approve"][-1], A["disapprove"][-1]
    a_30 = P.at(A["dates"], A["approve"], t30)
    new_polls = sorted([p for p in ap["polls"] if p["date"] >= t30], key=lambda p: p["date"])
    _, ro = P.runoff()
    m_now, k_now = ro["milei"][-1], ro["kicillof"][-1]
    m_30, k_30 = P.at(ro["dates"], ro["milei"], t30), P.at(ro["dates"], ro["kicillof"], t30)
    _, es = P.espacio(["La Libertad Avanza"])
    sp = {k: v["avg"] for k, v in es["spaces"].items()}

    def esv(name, when=None):
        a = sp.get(name, {"dates": [], "values": []})
        if when is None:
            return a["values"][-1] if a["values"] else None
        return P.at(a["dates"], a["values"], when)
    _, _, icg = P.icg()
    idx = icg["index"]
    i_now, i_prev = idx[-1], idx[-2]
    comp = icg["components"]
    _, reg = P.icg_votes(i_now["value"])
    _, pres, maxm = P.icg_by_president()
    at_m = {P.PRES_ES.get(k, k): v.get(maxm) for k, v in pres.items() if v.get(maxm) is not None}
    _, cands = P.polymarket()
    _, govs, gmonth = P.governors()

    w("# Monitor Electoral Argentina 2027")
    w("")
    w("## La foto en cinco números")
    w("")
    w(f"- Aprobación de Milei: {P.pct(a_now, 1)} ({P.delta(a_now, a_30)} en 30 días); desaprobación {P.pct(d_now, 1)}.")
    w(f"- Ballotage Milei–Kicillof: {P.pct(m_now, 1)} a {P.pct(k_now, 1)} en el promedio de encuestas.")
    w(f"- Primera vuelta por espacio: LLA {P.pct(esv('La Libertad Avanza'), 1)}, peronismo "
      f"{P.pct(esv('Peronismo'), 1)}, indecisos {P.pct(esv('Sin definir'), 1)}.")
    w(f"- Confianza en el gobierno (UTDT): {P.num(i_now['value'])} en {P.fmonth(i_now['date'])} "
      f"({P.delta(i_now['value'], i_prev['value'], 2, '')} contra el mes anterior).")
    if cands:
        w(f"- Polymarket: {cands[0]['name']} {P.pct(cands[0]['points'][-1][1])} de probabilidad de ganar en 2027.")
    w("")

    w("## La aprobación de Milei")
    w("")
    w(f"El promedio ubica la aprobación de la gestión en {P.pct(a_now, 1)} y la desaprobación en "
      f"{P.pct(d_now, 1)}, un saldo neto de {P.num(a_now - d_now, 1)} puntos. Hace un mes la aprobación era "
      f"{P.pct(a_30, 1)}: la serie está {trend_word(a_now - a_30 if a_30 is not None else None)}.")
    w("")
    w(embed("ar-approval", state))
    w("")
    if new_polls:
        lst = "; ".join(f"{p['firm']} ({P.pct(p['approve'])}"
                        + (f"/{P.pct(p['disapprove'])}" if p.get("disapprove") is not None else "") + ")"
                        for p in new_polls[-6:])
        n = len(new_polls)
        w(f"{'Entró 1 encuesta' if n == 1 else f'Entraron {n} encuestas'} en los últimos 30 días: {lst}.")
        w("")

    w("## Milei contra Kicillof")
    w("")
    gap = m_now - k_now
    txt = (f"En un ballotage hipotético el promedio da {P.pct(m_now, 1)} a {P.pct(k_now, 1)}, una diferencia de "
           f"{P.num(abs(gap), 1)} puntos. Hace un mes era {P.pct(m_30, 1)} a {P.pct(k_30, 1)}.")
    if abs(gap) < 3:
        txt += (" Con una brecha de este tamaño, la diferencia entre el oficialismo y la oposición está en un "
                "empate técnico.")
    w(txt)
    w("")
    w(embed("ar-runoff", state))
    w("")

    w("## La primera vuelta, por espacio")
    w("")
    und, und30 = esv("Sin definir"), esv("Sin definir", t30)
    verb = "suben" if (und is not None and und30 is not None and und > und30 + 0.05) else (
        "bajan" if (und is not None and und30 is not None and und < und30 - 0.05) else "se ubican")
    w(f"Agrupando por espacio, La Libertad Avanza promedia {P.pct(esv('La Libertad Avanza'), 1)} "
      f"({P.delta(esv('La Libertad Avanza'), esv('La Libertad Avanza', t30))}) y el peronismo "
      f"{P.pct(esv('Peronismo'), 1)} ({P.delta(esv('Peronismo'), esv('Peronismo', t30))}). Los indecisos {verb} "
      f"{'en ' if verb == 'se ubican' else 'a '}{P.pct(und, 1)} ({P.delta(und, und30)}).")
    w("")
    w(embed("ar-espacio", state))
    w("")
    w(f"Si se suma el PRO, el bloque LLA + PRO llega a {P.pct(esv('Alianza LLA+PRO'), 1)}.")
    w("")
    w(embed("ar-alianza", state))
    w("")

    w("## Confianza en el gobierno")
    w("")
    w(f"El Índice de Confianza en el Gobierno de la Di Tella fue de {P.num(i_now['value'])} en "
      f"{P.fmonth(i_now['date'])}, contra {P.num(i_prev['value'])} el mes anterior.")
    w("")
    w(embed("ar-icg", state))
    w("")
    parts = []
    for k, v in comp["series"].items():
        parts.append((P.COMP_ES.get(k, k), v[-1], v[-1] - v[-2] if len(v) > 1 else None))
    parts.sort(key=lambda x: x[2] if x[2] is not None else 0)
    w("Por componentes: " + "; ".join(
        f"{n} {P.num(v)} ({'sin cambios' if d is None or abs(d) < 0.005 else ('+' if d > 0 else '−') + P.num(abs(d))})"
        for n, v, d in parts) + ".")
    w("")
    w(embed("ar-icg-comp", state))
    w("")
    others = {k: v for k, v in at_m.items() if k != "Milei"}
    above = sorted(k for k, v in others.items() if v > at_m.get("Milei", 0))
    below = sorted(set(others) - set(above))
    w(f"Comparado con sus antecesores en el mismo mes de gestión (el {maxm}), Milei está en "
      f"{P.num(at_m.get('Milei'))}: por encima de {', '.join(below) or 'ninguno'} y por debajo de "
      f"{', '.join(above) or 'ninguno'}.")
    w("")
    w(embed("ar-icg-pres", state))
    w("")
    alliance = esv("Alianza LLA+PRO")
    refs = [x for x in (alliance, a_now) if x is not None]
    if refs and all(abs(reg["fitted"] - x) <= 4 for x in refs):
        cmp = ("Números similares a lo que muestra el agregado de intención de voto por espacio (LLA+PRO) "
               "y la aprobación presidencial.")
    elif refs and all(reg["fitted"] > x for x in refs):
        cmp = (f"Por encima de lo que muestran el agregado LLA+PRO ({P.pct(alliance, 1)}) y la aprobación "
               f"presidencial ({P.pct(a_now, 1)}).")
    else:
        cmp = (f"Para comparar, el agregado LLA+PRO está en {P.pct(alliance, 1)} y la aprobación presidencial "
               f"en {P.pct(a_now, 1)}.")
    w(f"¿Cuánto vale ese número en votos? Desde 2005, el ICG del mes de la elección multiplicado por 20 "
      f"anticipa razonablemente el voto del oficialismo (r = {P.num(reg['r'])} en once elecciones). Con el ICG "
      f"actual, el voto implícito es {P.pct(reg['implied'], 1)} y la recta de ajuste lo lleva a "
      f"{P.pct(reg['fitted'], 1)}. {cmp}")
    w("")
    w(embed("ar-icg-votes", state))
    w("")

    if cands:
        w("## Lo que dicen los mercados")
        w("")
        bits = []
        for c in cands:
            pts = c["points"]
            old = P.at([p[0] for p in pts], [p[1] for p in pts], t30)
            bits.append(f"{c['name']} {P.pct(pts[-1][1])} ({P.delta(pts[-1][1], old, 0)})")
        w("En Polymarket: " + "; ".join(bits) + ". Es un mercado chico, así que los saltos de un día dicen "
          "poco; importa la tendencia.")
        w("")
        w(embed("ar-polymarket", state))
        w("")

    if govs:
        top, bot = govs[:3], govs[-3:]
        ch = sorted([(g, g["pos"] - g["prev"]) for g in govs if g["prev"] is not None], key=lambda x: x[1])
        avg = sum(g["pos"] for g in govs) / len(govs)
        withprev = [g for g in govs if g["prev"] is not None]
        pavg = sum(g["prev"] for g in withprev) / len(withprev) if withprev else None
        kic = next((g for g in govs if "Kicillof" in g["gov"]), None)
        w("## Los gobernadores")
        w("")
        w(f"En el ranking de CB de {str(gmonth).lower().replace(' 20', ' de 20')}, la imagen positiva promedio de los "
          f"{len(govs)} gobernadores es {P.pct(avg, 1)} ({P.delta(avg, pavg)}). Arriba están " +
          ", ".join(f"{g['gov']} ({g['prov']}, {P.pct(g['pos'], 1)})" for g in top) + "; abajo, " +
          ", ".join(f"{g['gov']} ({g['prov']}, {P.pct(g['pos'], 1)})" for g in reversed(bot)) + ".")
        w("")
        w(embed("ar-governors", state))
        w("")
        line = ""
        if ch:
            line = (f"La mayor suba fue la de {ch[-1][0]['gov']} ({P.delta(ch[-1][0]['pos'], ch[-1][0]['prev'])}) "
                    f"y la mayor caída, la de {ch[0][0]['gov']} ({P.delta(ch[0][0]['pos'], ch[0][0]['prev'])}).")
        if kic:
            line += (f" Kicillof, el principal candidato opositor en las encuestas nacionales, tiene "
                     f"{P.pct(kic['pos'], 1)} de imagen positiva en su propia provincia, puesto "
                     f"{govs.index(kic) + 1} de {len(govs)}.")
        if line:
            w(line.strip())
            w("")

    w(f"Los datos completos, con cada encuesta y su fuente, están en [el monitor]({MONITOR_AR}).")
    w("")
    w(f"F.M.O., {P.fdate(today.isoformat())} de {today.year}")
    return "\n".join(L)


def latam(today, state):
    rows = P.latam()
    L = []
    w = L.append
    w("# Monitor de Aprobación Presidencial en América Latina")
    w("")
    w("## El ranking")
    w("")
    w(embed("latam-ranking", state))
    w("")
    if rows:
        best, worst = rows[0], rows[-1]
        pos_ar = next((i + 1 for i, r in enumerate(rows) if r["code"] == "AR"), None)
        w(f"{best['president']} encabeza el ranking con {P.pct(best['approve'])} y {worst['president']} lo "
          f"cierra con {P.pct(worst['approve'])}."
          + (f" Milei está en el puesto {pos_ar} de {len(rows)}." if pos_ar else ""))
        w("")
    w("## País por país")
    w("")
    for r in rows:
        if r["code"] == "AR":
            continue
        lp = r["last"]
        w(f"### {r['country']}")
        w("")
        w(embed(f"latam-{r['code'].lower()}", state))
        w("")
        txt = (f"{r['president']} promedia {P.pct(r['approve'])} de aprobación"
               + (f" y {P.pct(r['disapprove'])} de desaprobación" if r["disapprove"] is not None else "")
               + (f" ({P.delta(r['approve'], r['approve_30'])} en 30 días)" if r["approve_30"] is not None else "")
               + f". La última medición es de {lp['firm']} ({P.fdate(lp['date'])}): {P.pct(lp['approve'])}"
               + (f" aprueba y {P.pct(lp['disapprove'])} desaprueba" if lp.get("disapprove") is not None else "")
               + ".")
        if r["n_30"] == 0:
            txt += " No hubo encuestas nuevas en los últimos 30 días."
        w(txt)
        w("")
    w(f"Los datos completos, con cada encuesta y su fuente, están en [el monitor]({MONITOR_LATAM}).")
    w("")
    w(f"F.M.O., {P.fdate(today.isoformat())} de {today.year}")
    return "\n".join(L)


def main():
    args = sys.argv[1:]
    today = (datetime.date.fromisoformat(args[args.index("--date") + 1]) if "--date" in args
             else datetime.date.today())
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    out = HERE / "posts"
    out.mkdir(exist_ok=True)
    for name, fn in (("argentina", argentina), ("latam", latam)):
        p = out / f"{today.isoformat()}-{name}.md"
        p.write_text(fn(today, state) + "\n", encoding="utf-8")
        print(f"wrote {p.relative_to(HERE)}")


if __name__ == "__main__":
    main()
