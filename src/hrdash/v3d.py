"""Escenas 3D de las vistas graficas del dashboard (render tipo OpenGL en terminal).

Cada vista con el toggle 3D (`3` en el dashboard) tiene su escena aca: barras con
profundidad, dial 3D y circuito con perspectiva. Todas devuelven `Text` listo para el
widget Static.

- HR: barras 3D por zona (segundos en zona), una por zona de FC.
- Watts: barras 3D por banda de potencia (kJ acumulado).
- Gauge: dial 3D (cilindro) con arco de zonas, aguja y valor digital.
- Pista: circuito tipo velodromo con proyeccion de perspectiva.
"""

from __future__ import annotations

import math

from rich.text import Text

from .bigfont import render_big
from .gauges import Gauge, angle_for
from .pixels import Px, to_text

DIM = "#374151"
LABEL = "#9ca3af"
NEEDLE = "#f8fafc"


def _shade(color: str, f: float) -> str:
    """Aplica un factor de luminosidad a un color hex (caras traseras de barras 3D)."""
    try:
        r = int(color[1:3], 16) * f
        g = int(color[3:5], 16) * f
        b = int(color[5:7], 16) * f
        return f"#{int(r):02x}{int(g):02x}{int(b):02x}"
    except Exception:  # noqa: BLE001 - colores tolerados a lo malo
        return "#9ca3af"


class Gauge3(Gauge):
    """Dial 3D: mismo dial del gauge 2D pero con cuerpo de cilindro y arco en la cara."""

    def _build_bg(self, w, dh, lo, hi, segments, major, minor, avg) -> Px:
        pw, cx, cy, r, th = self._pxgeo(w, dh)
        px = Px()
        top, bot = cy - r, cy + r
        px.line(cx - r, top, cx - r, bot, 1, DIM)      # lateral izquierdo del cilindro
        px.line(cx + r, top, cx + r, bot, 1, DIM)      # lateral derecho del cilindro
        px.line(cx - r, top, cx + r, top, 1, DIM)      # boquilla del cilindro
        step = 0.35 / 270.0
        for f0, f1, col in segments:
            f = f0
            while f <= f1 + 1e-9:
                a = angle_for(f)
                ca, sa = math.cos(a), math.sin(a)
                for off in range(-th, th + 1):
                    rr = r + off
                    px.put(cx + rr * ca, cy - rr * sa, 1, col)
                f += step
                if f0 == f1:
                    break
        if minor and minor > 0:
            v = math.ceil(lo / minor) * minor
            while v <= hi + 1e-9:
                frac = (v - lo) / (hi - lo)
                is_maj = abs(v / major - round(v / major)) < 1e-9
                a = angle_for(frac)
                ca, sa = math.cos(a), math.sin(a)
                length = 6 if is_maj else 3
                r_in = r - th - 1
                px.line(cx + r_in * ca, cy - r_in * sa,
                        cx + (r_in - length) * ca, cy - (r_in - length) * sa,
                        2, LABEL if is_maj else DIM)
                v += minor
        if r >= 20 and major:
            rl = max(7.0, r - th - 12)
            v = math.ceil(lo / major) * major
            while v <= hi + 1e-9:
                frac = (v - lo) / (hi - lo)
                if 0.02 < frac < 0.985:
                    a = angle_for(frac)
                    ca, sa = math.cos(a), math.sin(a)
                    px.text3(cx + rl * ca, cy - rl * sa, str(int(round(v))), 2, LABEL)
                v += major
        if avg and avg > lo:
            a = angle_for((avg - lo) / (hi - lo))
            ca, sa = math.cos(a), math.sin(a)
            px.line(cx + (r + th + 1) * ca, cy - (r + th + 1) * sa,
                    cx + (r + th + 4) * ca, cy - (r + th + 4) * sa, 3, "#e5e7eb")
        return px


def render_gauge_3d(w, h, value, lo, hi, segments, major, minor,
                   label, sub, color, big=False, avg=None) -> Text:
    """Dial 3D: cilindro con arco de zonas, aguja y valor digital (heredado del Gauge)."""
    return Gauge3().render(w, h, value, lo, hi, segments, major, minor,
                           label, sub, color, big=big, avg=avg)


def render_bars_3d(w, h, items) -> Text:
    """Barras 3D: una por item (etiqueta, valor, color); el valor escala la altura.

    Cada barra se dibuja con dos capas (cuerpo + cara trasera sombreada) para dar
    profundidad 3D. Se devolvemos `Text` listo para el widget Static.
    """
    w, h = int(max(12, w or 12)), int(max(5, h or 5))
    t = Text()
    n = len(items)
    if n == 0:
        t.append(" sin datos ")
        return t
    bw = max(2, min(8, (w - 6) // n))
    gap = 1
    base = h - 1
    top_max = max(1, base - 3)
    maxv = max((v for _l, v, _c in items), default=0) or 1.0
    tops = []
    for i, (label, val, color) in enumerate(items):
        bh = int(round((val / maxv) * (top_max - 1)))
        bh = max(0, min(top_max - 1, bh))
        tops.append(base - bh)
    for r in range(h):
        line = Text()
        col = 0
        for i, (label, val, color) in enumerate(items):
            x0 = 2 + i * (bw + gap)
            top = tops[i]
            if top <= r <= base:
                seg, seg_style = ("█" * bw, _shade(color, 0.55)) if r > top else ("█" * bw, color)
                if x0 - col > 0:
                    line.append(" " * (x0 - col))
                line.append(seg, style=seg_style)
            col = x0 + bw + gap
        if r:
            t.append("\n")
        t.append(line)
    return t