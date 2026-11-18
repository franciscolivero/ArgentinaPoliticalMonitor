#!/usr/bin/env python3
"""
make_post_images.py — static images for the Substack posts, for charts Datawrapper cannot draw.

  post_images/ar-prov-small-multiples.png
      Positive image of Milei, Kicillof and Bullrich in each of the 24 districts, one panel per
      province, every CB measurement (provincial_image_CB.csv). Same design as the province grid
      on governors.html: panels ordered by Milei's latest reading, shared 0–70% scale.

Run by the "Substack charts and drafts" workflow; the drafts link the image by its GitHub Pages
address, so it can be pasted into Substack straight from the draft.
"""

import pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

import post_data as P

HERE = pathlib.Path(__file__).parent
OUT = HERE / "post_images"
COL = {"Milei": "#6B3FA0", "Kicillof": "#2F6FA8", "Bullrich": "#C9962B"}
INK, MUTED, GRID, LINE = "#0F2238", "#5B6B7C", "#EEF1F5", "#DDE3EA"
SHORT = {"Buenos Aires": "Buenos Aires (prov.)", "Santiago del Estero": "Sgo. del Estero",
         "Tierra del Fuego": "T. del Fuego"}
MES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def font():
    have = {f.name for f in font_manager.fontManager.ttflist}
    for name in ("Archivo", "Inter", "DejaVu Sans"):
        if name in have:
            return name
    return "sans-serif"


def small_multiples(path):
    data, months = P.provincial_image()
    if not months:
        return False
    last = months[-1]
    provs = sorted({k[2] for k in data if k[0] == last and k[1] == "Milei"},
                   key=lambda p: -data[(last, "Milei", p)][0])
    X = [int(m[5:7]) for m in months]
    plt.rcParams.update({"font.family": font(), "font.size": 10})
    cols, rows = 6, (len(provs) + 5) // 6
    fig, axes = plt.subplots(rows, cols, figsize=(14, 2.55 * rows + 1.5), sharex=True, sharey=True)
    fig.patch.set_facecolor("white")
    for i, ax in enumerate(axes.flat):
        if i >= len(provs):
            ax.axis("off")
            continue
        p = provs[i]
        for who, c in COL.items():
            pts = [(x, data[(m, who, p)][0]) for x, m in zip(X, months) if (m, who, p) in data]
            if not pts:
                continue
            xs, ys = zip(*pts)
            if len(pts) > 1:
                ax.plot(xs, ys, color=c, lw=2.2, marker="o", ms=4, zorder=3)
            else:
                ax.plot(xs, ys, color=c, lw=0, marker="D", ms=7.5, zorder=4)
        ax.set_ylim(0, 70)
        ax.set_xlim(X[0] - 0.6, X[-1] + 0.6)
        ax.set_yticks([0, 20, 40, 60])
        ax.set_yticklabels(["0", "20%", "40%", "60%"])
        ax.set_xticks(X)
        ax.set_xticklabels([MES[x - 1] for x in X])
        ax.grid(axis="y", color=GRID, lw=1)
        ax.set_axisbelow(True)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(LINE)
        ax.tick_params(colors=MUTED, length=0, labelsize=9)
        ax.set_title(SHORT.get(p, p), loc="left", fontsize=11.5, fontweight="bold", color=INK, pad=6)
    first, lastm = months[0], months[-1]
    fig.text(0.012, 0.985, "Imagen positiva de Milei, Kicillof y Bullrich, provincia por provincia",
             fontsize=17, fontweight="bold", color=INK, va="top")
    sub = (f"CB Consultora, {MES[int(first[5:7]) - 1].lower()}. {first[:4]} a {MES[int(lastm[5:7]) - 1].lower()}. "
           f"{lastm[:4]}. Provincias ordenadas por la imagen de Milei en la última medición; Bullrich se midió "
           f"por primera vez en noviembre.")
    fig.text(0.012, 0.952, sub, fontsize=10.5, color=MUTED, va="top")
    x = 0.012
    for who, c in COL.items():
        fig.text(x, 0.925, "■", color=c, fontsize=12, va="top")
        fig.text(x + 0.014, 0.925, {"Milei": "Javier Milei", "Kicillof": "Axel Kicillof",
                                    "Bullrich": "Patricia Bullrich"}[who], color=INK, fontsize=10.5, va="top")
        x += 0.12
    fig.text(0.988, 0.008, "Fuente: CB Consultora · Monitor Argentina 2027 · Francisco M. Olivero",
             ha="right", fontsize=9, color="#8C99A7")
    fig.subplots_adjust(left=0.035, right=0.99, top=0.885, bottom=0.04, hspace=0.42, wspace=0.12)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


def main():
    OUT.mkdir(exist_ok=True)
    if small_multiples(OUT / "ar-prov-small-multiples.png"):
        print("wrote post_images/ar-prov-small-multiples.png")


if __name__ == "__main__":
    main()
