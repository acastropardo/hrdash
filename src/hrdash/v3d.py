"""Escenas 3D de las vistas graficas del dashboard (render tipo OpenGL en terminal).

Cada vista tiene su escena 3D (toggle `3`): cilindro con liquido para FC, cajas
extruidas para potencia, dial eliptico y circuito en perspectiva. Todas devuelven
`Text` listo para el widget Static, con el mismo idioma de `Gauge`/`Track`: framebuffer
`Px` (2x4 px por celda braille) + cara clara al frente y sombreado lateral.

- HR: cilindro 3D con el nivel de FC actual (relleno coloreado por zona).
- Watts: barras 3D extruidas (frente + lateral + tapa) por banda de %FTP (kJ).
- Gauge: `Gauge3`, dial eliptico (circulo visto en angulo) con arco de zonas y aguja.
- Pista: la misma `Track` con `persp=True` (keystone + costado lejano atenuado).
"""

from __future__ import annotations

import math

from rich.text import Text

from .bigfont import render_big
from .gauges import angle_for
from .pixels import Px, clamp, to_text

DIM = "#374151"
LABEL = "#9ca3af"
NEEDLE = "#f8fafc"
EDGE = "#9ca3af"
BACK = "#111827"


def _shade(color: str, f: float) -> str:
    """Aplica un factor de luminosidad a un hex (caras laterales y tapas de las cajas)."""
    try:
        r = min(255, max(0, int(int(color[1:3], 16) * f)))
        g = min(255, max(0, int(int(color[3:5], 16) * f)))
        b = min(255, max(0, int(int(color[5:7], 16) * f)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:  # noqa: BLE001 - color raro: neutro
        return "#9ca3af"


def _ellipse(px, cx, cy, rx, ry, order, color) -> None:
    """Elipse muestreada (tapa del cilindro, aro del dial) con segmentos cortos."""
    steps = max(28, int(rx))
    prev = None
    for i in range(steps + 1):
        a = 2 * math.pi * i / steps
        x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
        if prev:
            px.line(prev[0], prev[1], x, y, order, color)
        prev = (x, y)


def render_cylinder(w, h, frac, color, segs, label, sub) -> Text:
    """Cilindro 3D: relleno hasta `frac`, sombreado lateral y bandas de zona en la pared.

    `segs` son los cortes (f0, f1, color) de las zonas sobre el rango 0..1, igual que
    en los gauges. El numero grande y el subtitle van abajo, como en `Gauge`.
    """
    w, h = int(max(10, w or 10)), int(max(6, h or 6))
    bigrows = render_big(label)
    if h < 11 or len(bigrows[0]) > w - 1:
        bigrows = None
    dig_rows = 5 if bigrows else 2
    dh = max(4, h - dig_rows)
    px = Px()
    pw, pph = 2 * w, 4 * dh
    cx = pw / 2.0
    rx = max(6.0, min(pw / 2.0 - 16, 44.0))
    ry = max(2.0, rx * 0.26)
    top = ry + 2
    bot = pph - 4
    if bot - top < 6:
        t = Text(justify="center")
        t.append(label.center(w), style=f"bold {color}")
        t.append("\n")
        t.append(sub[:w].center(w), style=LABEL)
        return t
    frac = clamp(frac)
    lvl = bot - (bot - top) * frac
    # silueta del tubo
    px.line(cx - rx, top, cx - rx, bot, 1, DIM)
    px.line(cx + rx, top, cx + rx, bot, 1, DIM)
    _ellipse(px, cx, bot, rx, ry, 1, DIM)
    _ellipse(px, cx, top, rx, ry, 1, EDGE)
    # bandas de zona dibujadas sobre la pared (escala de FC)
    for f0, f1, col in segs:
        y0 = bot - (bot - top) * clamp(f0)
        y1 = bot - (bot - top) * clamp(f1)
        if y1 > y0 + 1:
            px.line(cx + rx + 3, y0, cx + rx + 3, y1, 2, col, width=2)
            px.line(cx - rx - 3, y0, cx - rx - 3, y1, 2, _shade(col, 0.6), width=1)
    # liquido: sombreado cilindrico (claro al centro, oscuro en los bordes)
    for xx in range(int(cx - rx), int(cx + rx) + 1):
        d = (xx - cx) / max(1.0, rx)
        px.line(xx, lvl, xx, bot, 3, _shade(color, 1.05 - 0.55 * d * d))
    _ellipse(px, cx, lvl, rx, ry, 5, _shade(color, 1.4))
    t = to_text(px, w, dh)
    if bigrows:
        for row in bigrows:
            t.append("\n" + row[:w].center(w), style=f"bold {color}")
    else:
        t.append("\n" + label.center(w), style=f"bold {color}")
    t.append("\n" + sub[:w].center(w), style=LABEL)
    return t


def render_boxes_3d(w, h, items) -> Text:
    """Barras 3D extruidas: items (etiqueta, valor, color); cada caja tiene 3 caras.

    El valor escala la altura pero siempre queda un zocalo, para que la escena se lea
    3D aunque la sesion este vacia. Encima de cada caja va el valor en fuente 3x5.
    """
    w, h = int(max(12, w or 12)), int(max(5, h or 5))
    px = Px()
    pw, pph = 2 * w, 4 * (h - 1)
    dh = h - 1
    n = len(items)
    if n == 0 or dh < 4:
        t = Text(justify="center")
        t.append(" sin datos ", style=LABEL)
        return t
    maxv = max((v for _l, v, _c in items), default=0) or 1.0
    cell = pw / n
    dx = max(2.0, min(cell * 0.22, pw * 0.03))       # profundidad de la caja
    bw = max(3.0, cell - 2 * dx - 4)                 # ancho del frente
    base = pph - 2
    ceiling = 6
    for i, (label, val, color) in enumerate(items):
        x0 = i * cell + dx + 2
        bh = (val / maxv) * (base - ceiling)
        bh = max(2.0, min(base - ceiling, bh))       # zocalo minimo: siempre se ve la caja
        ytop = base - bh
        for xx in range(int(x0), int(x0 + bw) + 1):
            d = (xx - (x0 + bw / 2.0)) / max(1.0, bw / 2.0)
            px.line(xx, ytop, xx, base, 3, _shade(color, 1.05 - 0.35 * d * d))
        side = _shade(color, 0.55)
        for dd in range(int(dx) + 1):                # cara lateral derecha (inclinada)
            px.line(x0 + bw + dd, ytop - dd * 0.5, x0 + bw + dd, base - dd * 0.5, 2, side)
        lid = _shade(color, 1.35)
        for dd in range(int(dx) + 1):                # tapa superior
            px.line(x0 + dd, ytop - dd * 0.5, x0 + bw + dd, ytop - dd * 0.5, 4, lid)
        px.line(x0, base, x0 + bw, base, 1, BACK)    # sombra en el piso
        if val > 0:
            px.text3(x0 + bw / 2.0 + dx / 2.0, ytop - dx - 3, f"{val:.0f}", 6, _shade(color, 1.5))
    t = to_text(px, w, dh)
    return t


class Gauge3:
    """Dial 3D: circulo visto en angulo (elipse) con arco de zonas, aguja y valor.

    Igual que `Gauge`, cachea el fondo (aro/ticks/labels/media) por geometria y solo
    redibuja la aguja por frame.
    """

    def __init__(self):
        self._sig = None
        self._bg: Px | None = None
        self._geo: tuple | None = None

    def _pxgeo(self, w, dh):
        pw = 2 * w
        pph = 4 * dh
        cx = pw / 2.0
        cy = pph * 0.50
        rx = max(6.0, min(pw / 2.0 - 10, pph / 2.0 / 0.45 - 6))
        ry = max(2.0, rx * 0.40)
        th = 2
        return pw, cx, cy, rx, ry, th

    def _build_bg(self, w, dh, lo, hi, segments, major, minor, avg) -> Px:
        pw, cx, cy, rx, ry, th = self._pxgeo(w, dh)
        px = Px()
        # aro del dial: 270 grados barridos sobre la elipse achatada (trazo continuo)
        step = 1.2 / 270.0
        for f0, f1, col in segments:
            prev = None
            f = f0
            while f <= f1 + 1e-9:
                a = angle_for(f)
                x, y = cx + rx * math.cos(a), cy - ry * math.sin(a)
                if prev:
                    px.line(prev[0], prev[1], x, y, 1, col, width=2 * th + 1)
                prev = (x, y)
                f += step
                if f0 == f1:
                    break
        # borde exterior del tubo (da la sensacion de volumen detras del aro)
        _ellipse(px, cx, cy, rx + th + 2, ry + th + 2, 0, DIM)
        _ellipse(px, cx, cy, rx - th - 2, ry - th - 2, 0, BACK)
        # ticks (mayores/menores) hacia adentro del aro
        if minor and minor > 0:
            v = math.ceil(lo / minor) * minor
            while v <= hi + 1e-9:
                frac = (v - lo) / (hi - lo)
                is_maj = abs(v / major - round(v / major)) < 1e-9
                a = angle_for(frac)
                kx, ky = math.cos(a), math.sin(a)
                length = 6 if is_maj else 3
                r_in, y_in = rx - th - 2, ry - th - 2
                px.line(cx + r_in * kx, cy - y_in * ky,
                        cx + (r_in - length) * kx, cy - (y_in - length * 0.4) * ky,
                        2, LABEL if is_maj else DIM)
                v += minor
        # numeros de ticks mayores (radio interno achatado)
        if rx >= 22 and major:
            rl = max(7.0, rx - th - 12)
            v = math.ceil(lo / major) * major
            while v <= hi + 1e-9:
                frac = (v - lo) / (hi - lo)
                if 0.02 < frac < 0.985:
                    a = angle_for(frac)
                    px.text3(cx + rl * math.cos(a), cy - (rl * 0.4) * math.sin(a),
                             str(int(round(v))), 2, LABEL)
                v += major
        # marcador de media de sesion (puntada externa)
        if avg and avg > lo:
            a = angle_for((avg - lo) / (hi - lo))
            kx, ky = math.cos(a), math.sin(a)
            px.line(cx + (rx + th + 3) * kx, cy - (ry + th + 1) * ky,
                    cx + (rx + th + 6) * kx, cy - (ry + th + 3) * ky, 3, "#e5e7eb")
        return px

    def render(self, w, h, value, lo, hi, segments, major, minor, label, sub,
               color, big=False, avg=None) -> Text:
        w, h = int(max(8, w or 8)), int(max(5, h or 5))
        bigrows = render_big(label) if big else None
        if bigrows and (h < 12 or len(bigrows[0]) > w - 1):
            bigrows = None                       # no entra: cae a texto plano
        dig_rows = 5 if bigrows else 2
        dh = h - dig_rows
        if dh < 4 or self._pxgeo(w, dh)[3] < 6:
            return self._fallback(w, label, sub, color)
        avg_b = round(avg) if avg else None
        sig = (w, dh, round(lo, 1), round(hi, 1), tuple(segments), major, minor, avg_b)
        if sig != self._sig:
            self._geo = self._pxgeo(w, dh)
            self._bg = self._build_bg(w, dh, lo, hi, segments, major, minor, avg)
            self._sig = sig
        pw, cx, cy, rx, ry, th = self._geo
        px = Px()
        px.d.update(self._bg.d)
        frac = clamp((value - lo) / max(1e-6, hi - lo))
        a = angle_for(frac)
        ca, sa = math.cos(a), math.sin(a)
        px.line(cx - 0.16 * rx * ca, cy + 0.16 * ry * sa,     # contrapeso
                cx + 0.86 * rx * ca, cy - 0.86 * ry * sa, 5, NEEDLE, width=3)
        px.dot_circle(cx, cy, 2.2, 6, color)                  # cubo central
        t = to_text(px, w, dh)
        if bigrows:
            for row in bigrows:
                t.append("\n" + row[:w].center(w), style=f"bold {color}")
            t.append("\n" + sub[:w].center(w), style=LABEL)
        else:
            t.append("\n" + label.center(w), style=f"bold {color}")
            t.append("\n" + sub[:w].center(w), style=LABEL)
        return t

    @staticmethod
    def _fallback(w, label, sub, color) -> Text:
        t = Text(justify="center")
        t.append(label.center(w), style=f"bold {color}")
        t.append("\n")
        t.append(sub[:w].center(w), style=LABEL)
        return t