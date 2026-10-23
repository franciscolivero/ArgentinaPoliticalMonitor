#!/usr/bin/env python3
"""
draft_posts.py — writes this week's two Substack drafts from the monitor's data:

    posts/AAAA-MM-DD-argentina.md   every indicator in the monitor, governors included
    posts/AAAA-MM-DD-latam.md       presidential ranking, then one chart + paragraph per country

Every number and every week-on-week / month-on-month change is filled in from the repo. Each chart
line is the Datawrapper address from datawrapper_charts.json: in Substack, paste it on its own line
and the live chart appears. Lines marked ✍️ are where the author adds the context the data cannot
give (what happened that week); delete the marker before publishing.

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


def argentina(today, state):
    L = []
    w = L.append
    t30 = P.ago(30, today)
    # --- numbers
    _, ap = P.approval()
    A = ap["avg"]
    a_now, d_now = A["approve"][-1], A["disapprove"][-1]
    a_30, d_30 = P.at(A["dates"], A["approve"], t30), P.at(A["dates"], A["disapprove"], t30)
    new_polls = sorted([p for p in ap["polls"] if p["date"] >= t30], key=lambda p: p["date"])
    _, ro = P.runoff()
    m_now, k_now = ro["milei"][-1], ro["kicillof"][-1]
    m_30, k_30 = P.at(ro["dates"], ro["milei"], t30), P.at(ro["dates"], ro["kicillof"], t30)
    _, es = P.espacio(["La Libertad Avanza"])
    sp = {k: v["avg"] for k, v in es["spaces"].items()}
    def esv(name, when=None):
        a = sp.get(name, {"dates": [], "values": []})
        return a["values"][-1] if when is None and a["values"] else P.at(a["dates"], a["values"], when or "9999")
    _, _, icg = P.icg()
    idx = icg["index"]
    i_now, i_prev = idx[-1], idx[-2]
    comp = icg["components"]
    _, reg = P.icg_votes(i_now["value"])
    _, pres, maxm = P.icg_by_president()
    at_m = {P.PRES_ES.get(k, k): v.get(maxm) for k, v in pres.items() if v.get(maxm) is not None}
    _, cands = P.polymarket()
    _, govs, gmonth = P.governors()

    # --- topline
    w(f"# Monitor Argentina 2027: actualización del {P.fdate(today.isoformat())}")
    w("")
    w(f"*Actualizado el {P.fdate(today.isoformat())} de {today.year}.* ✍️ Una o dos oraciones sobre lo que más se movió "
      "esta semana y por qué.")
    w("")
    w("**La foto en cinco números**")
    w("")
    w(f"- **Aprobación de Milei:** {P.pct(a_now, 1)} ({P.delta(a_now, a_30)} en 30 días); desaprobación {P.pct(d_now, 1)}.")
    w(f"- **Ballotage Milei–Kicillof:** {P.pct(m_now, 1)} a {P.pct(k_now, 1)} en el promedio de encuestas.")
    w(f"- **Primera vuelta por espacio:** LLA {P.pct(esv('La Libertad Avanza'), 1)}, peronismo {P.pct(esv('Peronismo'), 1)}, "
      f"indecisos {P.pct(esv('Sin definir'), 1)}.")
    w(f"- **Confianza en el gobierno (UTDT):** {P.num(i_now['value'])} en {P.fmonth(i_now['date'])} "
      f"({P.delta(i_now['value'], i_prev['value'], 2, '')} contra el mes anterior).")
    if cands:
        w(f"- **Polymarket:** {cands[0]['name']} {P.pct(cands[0]['points'][-1][1])} de probabilidad de ganar en 2027.")
    w("")
    # --- approval
    w("## La aprobación de Milei")
    w("")
    w(f"El promedio ubica la aprobación de la gestión en **{P.pct(a_now, 1)}** y la desaprobación en "
      f"**{P.pct(d_now, 1)}**, un saldo neto de {P.num(a_now - d_now, 1)} puntos. Hace un mes la aprobación era "
      f"{P.pct(a_30, 1)}: la serie está {trend_word(a_now - a_30 if a_30 else None)}.")
    w("")
    w(embed("ar-approval", state))
    w("")
    if new_polls:
        lst = "; ".join(f"{p['firm']} ({P.pct(p['approve'])}"
                        + (f"/{P.pct(p['disapprove'])}" if p.get("disapprove") is not None else "") + ")"
                        for p in new_polls[-6:])
        w(f"Entraron {len(new_polls)} encuestas en los últimos 30 días: {lst}. El promedio pondera por tamaño de "
          "muestra, por antigüedad y por el historial de cada encuestadora, y descuenta el sesgo sistemático "
          "de cada casa, así que una medición aislada mueve poco la línea. ✍️ Qué explica el movimiento.")
    w("")
    # --- runoff
    w("## Milei contra Kicillof")
    w("")
    w(f"En un ballotage hipotético el promedio da **{P.pct(m_now, 1)} a {P.pct(k_now, 1)}**, una diferencia de "
      f"{P.num(m_now - k_now, 1)} puntos. Hace un mes era {P.pct(m_30, 1)} a {P.pct(k_30, 1)}. Con una brecha de "
      "este tamaño, la carrera está dentro del margen en el que una sola encuesta puede dar vuelta el resultado.")
    w("")
    w(embed("ar-runoff", state))
    w("")
    # --- by space
    w("## La primera vuelta, por espacio")
    w("")
    w(f"Agrupando por espacio, La Libertad Avanza promedia **{P.pct(esv('La Libertad Avanza'), 1)}** "
      f"({P.delta(esv('La Libertad Avanza'), esv('La Libertad Avanza', t30))}) y el peronismo **{P.pct(esv('Peronismo'), 1)}** "
      f"({P.delta(esv('Peronismo'), esv('Peronismo', t30))}). Los indecisos suben a {P.pct(esv('Sin definir'), 1)} "
      f"({P.delta(esv('Sin definir'), esv('Sin definir', t30))}): buena parte de la elección todavía no tiene dueño.")
    w("")
    w(embed("ar-espacio", state))
    w("")
    w(f"Si se suma el PRO, el bloque LLA + PRO llega a **{P.pct(esv('Alianza LLA+PRO'), 1)}**. Es una suma de "
      "promedios, no una medición de una lista conjunta: cuando dos fuerzas se unen el voto no se transfiere "
      "entero, así que conviene leerlo como un techo.")
    w("")
    w(embed("ar-alianza", state))
    w("")
    # --- ICG
    w("## Confianza en el gobierno")
    w("")
    w(f"El Índice de Confianza en el Gobierno de la Di Tella fue de **{P.num(i_now['value'])}** en "
      f"{P.fmonth(i_now['date'])}, contra {P.num(i_prev['value'])} el mes anterior.")
    w("")
    w(embed("ar-icg", state))
    w("")
    keys = list(comp["series"])
    parts = []
    for k in keys:
        v = comp["series"][k]
        parts.append((P.COMP_ES.get(k, k), v[-1], v[-1] - v[-2] if len(v) > 1 else None))
    parts.sort(key=lambda x: x[2] if x[2] is not None else 0)
    w("Por componentes: " + "; ".join(f"{n} {P.num(v)} ({P.delta(v, v - d if d is not None else None, 2, '')})"
                                      for n, v, d in parts) + ".")
    w("")
    w(embed("ar-icg-comp", state))
    w("")
    others = {k: v for k, v in at_m.items() if k != "Milei"}
    above = [k for k, v in others.items() if v > at_m.get("Milei", 0)]
    w(f"Comparado con sus antecesores en el mismo mes de gestión (el {maxm}), Milei está en "
      f"{P.num(at_m.get('Milei'))}: por encima de {', '.join(sorted(set(others) - set(above))) or 'nadie'} y por "
      f"debajo de {', '.join(sorted(above)) or 'nadie'}.")
    w("")
    w(embed("ar-icg-pres", state))
    w("")
    w(f"¿Cuánto vale ese número en votos? Desde 2005, el ICG del mes de la elección multiplicado por 20 "
      f"anticipa razonablemente el voto del oficialismo (r = {P.num(reg['r'])} en once elecciones). Con el ICG "
      f"actual, el voto implícito es {P.pct(reg['implied'], 1)} y la recta de ajuste lo lleva a "
      f"**{P.pct(reg['fitted'], 1)}**. Es una regularidad, no un pronóstico: 2007 y 2011 quedaron muy por encima.")
    w("")
    w(embed("ar-icg-votes", state))
    w("")
    # --- markets
    if cands:
        w("## Lo que dicen los mercados")
        w("")
        bits = []
        for c in cands:
            pts = c["points"]
            old = P.at([p[0] for p in pts], [p[1] for p in pts], t30)
            bits.append(f"{c['name']} {P.pct(pts[-1][1])} ({P.delta(pts[-1][1], old, 0)})")
        w("En Polymarket: " + "; ".join(bits) + ". Es un mercado chico, así que los saltos de un día "
          "dicen poco; importa la tendencia. ✍️ Contexto si hubo un movimiento grande.")
        w("")
        w(embed("ar-polymarket", state))
        w("")
    # --- governors
    if govs:
        top, bot = govs[:3], govs[-3:]
        ch = sorted([(g, g["pos"] - g["prev"]) for g in govs if g["prev"] is not None], key=lambda x: x[1])
        avg = sum(g["pos"] for g in govs) / len(govs)
        pavg = (sum(g["prev"] for g in govs if g["prev"] is not None) /
                max(1, sum(1 for g in govs if g["prev"] is not None)))
        kic = next((g for g in govs if "Kicillof" in g["gov"]), None)
        w("## Los gobernadores")
        w("")
        w(f"En el ranking de CB de {str(gmonth).lower().replace(' 20', ' de 20')}, la imagen positiva promedio de los 24 gobernadores es "
          f"{P.pct(avg, 1)} ({P.delta(avg, pavg)}). Arriba están " +
          ", ".join(f"{g['gov']} ({g['prov']}, {P.pct(g['pos'], 1)})" for g in top) + "; abajo, " +
          ", ".join(f"{g['gov']} ({g['prov']}, {P.pct(g['pos'], 1)})" for g in reversed(bot)) + ".")
        w("")
        w(embed("ar-governors", state))
        w("")
        if ch:
            w(f"La mayor suba fue la de {ch[-1][0]['gov']} ({P.delta(ch[-1][0]['pos'], ch[-1][0]['prev'])}) y la mayor "
              f"caída, la de {ch[0][0]['gov']} ({P.delta(ch[0][0]['pos'], ch[0][0]['prev'])}).")
        if kic:
            L[-1] += (f" Kicillof, el principal candidato opositor en las encuestas nacionales, tiene "
              f"{P.pct(kic['pos'], 1)} de imagen positiva en su propia provincia, puesto {govs.index(kic) + 1} de 24.")
        w("")
    w("---")
    w("")
    w("Los datos completos, con cada encuesta y su fuente, están en el monitor. ✍️ Link al monitor.")
    w("")
    w(f"***{SIGN}, {P.fdate(today.isoformat())} de {today.year}***")
    return "\n".join(L)


def latam(today, state):
    rows = P.latam()
    L = []
    w = L.append
    w(f"# Aprobación presidencial en América Latina: {P.fdate(today.isoformat())}")
    w("")
    w(f"*Actualizado el {P.fdate(today.isoformat())} de {today.year}.* Promedio de las encuestas publicadas en cada "
      "país, con datos de las encuestadoras, de la prensa y de los rankings regionales. ✍️ Una oración sobre lo "
      "más importante de la semana.")
    w("")
    w("## El ranking")
    w("")
    w(embed("latam-ranking", state))
    w("")
    if rows:
        best, worst = rows[0], rows[-1]
        w(f"{best['president']} sigue arriba con {P.pct(best['approve'])}; {worst['president']} cierra la tabla "
          f"con {P.pct(worst['approve'])}. Milei aparece en el puesto "
          f"{next((i + 1 for i, r in enumerate(rows) if r['code'] == 'AR'), '—')} de {len(rows)}.")
        w("")
    w("## País por país")
    w("")
    for r in rows:
        if r["code"] == "AR":
            continue
        d = None if r["approve_30"] is None else r["approve"] - r["approve_30"]
        lp = r["last"]
        w(f"### {r['country']}")
        w("")
        w(embed(f"latam-{r['code'].lower()}", state))
        w("")
        txt = (f"{r['president']} promedia **{P.pct(r['approve'])}** de aprobación"
               + (f" y {P.pct(r['disapprove'])} de desaprobación" if r["disapprove"] is not None else "")
               + (f", {P.delta(r['approve'], r['approve_30'])} en el último mes" if d is not None else "") + ". "
               f"La última medición es de {lp['firm']} ({P.fdate(lp['date'])}, {P.pct(lp['approve'])}).")
        if r["n_30"] == 0:
            txt += " No hubo encuestas nuevas en los últimos 30 días."
        w(txt + " ✍️ Qué pasó.")
        w("")
    w("---")
    w("")
    w(f"***{SIGN}, {P.fdate(today.isoformat())} de {today.year}***")
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
