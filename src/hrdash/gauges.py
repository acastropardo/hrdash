"""Gauges tipo tablero de auto dibujados con glifos braille (2x4 px por celda).

Un `Gauge` renderiza un arco de 270 grados con segmentos de color (zonas FC o
bandas de %FTP), ticks con numeros en fuente 3x5, aguja y valor digital
(center o `bigfont`). El fondo (arco+ticks+labels+media) se cachea por
geometria; solo la aguja se redibuja en cada frame de animacion.
"""

from __future__ import annotations

import math

from rich.text import Text

from .bigfont import render_big

# barrido estilo cuentavueltas: 0% en 225 deg, 100% en -45 deg (pasa por arriba)
A0 = 225.0
SWEEP = 270.0

# bit braille segun (dx, dy) dentro de la celda de 2x4 pixeles
_BR = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
       (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}

# fuente minima 3x5 para numeros de ticks
_F3 = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "011", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
    " ": ("000", "000", "000", "000", "000"),
}

DIM = "#374151"
LABEL = "#9ca3af"
NEEDLE = "#f8fafc"      # aguja clara: se ve sobre cualquier tramo del arco


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def angle_for(frac: float) -> float:
    """Angulo (radianes, sentido antihorario, y arriba) del valor fraccionado."""
    return math.radians(A0 - SWEEP * _clamp(frac))


class _Px:
    """Framebuffer de pixeles con z-order: (x, y) -> (orden, color)."""

    def __init__(self):
        self.d: dict[tuple[int, int], tuple[int, str]] = {}

    def put(self, x: float, y: float, order: int, color: str) -> None:
        self.d[(int(round(x)), int(round(y)))] = (order, color)

    def line(self, x0, y0, x1, y1, order, color, width=1) -> None:
        n = max(int(abs(x1 - x0)), int(abs(y1 - y0)), 1)
        offs = range(-(width // 2), width // 2 + 1) if width > 1 else (0,)
        dx, dy = x1 - x0, y1 - y0
        ln = math.hypot(dx, dy) or 1.0
        px_, py_ = -dy / ln, dx / ln          # perpendicular
        for i in range(n + 1):
            f = i / n
            x, y = x0 + dx * f, y0 + dy * f
            for o in offs:
                self.put(x + px_ * o, y + py_ * o, order, color)

    def dot_circle(self, cx, cy, rad, order, color) -> None:
        for yy in range(int(cy - rad), int(cy + rad) + 1):
            for xx in range(int(cx - rad), int(cx + rad) + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= rad ** 2:
                    self.put(xx, yy, order, color)

    def text3(self, x, y, s, order, color) -> None:
        """Escribe `s` con fuente 3x5 (x,y = centro del texto)."""
        w = len(s) * 4 - 1
        x0 = int(round(x - w / 2))
        for i, ch in enumerate(s):
            g = _F3.get(ch, _F3[" "])
            for r, row in enumerate(g):
                for c, bit in enumerate(row):
                    if bit == "1":
                        self.put(x0 + i * 4 + c, int(round(y)) - 2 + r, order, color)


def _to_text(px: _Px, w: int, h: int) -> Text:
    """Convierte el framebuffer (en pixeles) a `h` filas de celdas braille."""
    cells: dict[tuple[int, int], list] = {}
    for (x, y), (order, color) in px.d.items():
        c, r = x // 2, y // 4
        if not (0 <= r < h and 0 <= c < w):
            continue
        cur = cells.get((r, c))
        bit = _BR[(x % 2, y % 4)]
        if cur is None:
            cells[(r, c)] = [bit, order, color]
        else:
            cur[0] |= bit
            if order >= cur[1]:
                cur[1], cur[2] = order, color
    t = Text()
    for r in range(h):
        for c in range(w):
            cur = cells.get((r, c))
            if cur:
                t.append(chr(0x2800 + cur[0]), style=cur[2])
            else:
                t.append(" ")
        if r < h - 1:
            t.append("\n")
    return t


class Gauge:
    """Gauge reutilizable con cache del fondo (arco/ticks/labels/media)."""

    def __init__(self):
        self._sig = None
        self._bg: _Px | None = None
        self._geo: tuple | None = None

    # ---- fondo cacheado ----
    def _build_bg(self, w, dh, lo, hi, segments, major, minor, avg) -> _Px:
        pw, cx, cy, r, th = self._pxgeo(w, dh)
        px = _Px()
        # arco segmentado (banda de grosor 2*th+1 centrada en r)
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
        # ticks (mayores/menores) hacia adentro del arco
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
        # numeros en ticks mayores (solo si hay radio suficiente para rl)
        if r >= 20 and major:
            rl = max(7.0, r - th - 12)
            v = math.ceil(lo / major) * major
            while v <= hi + 1e-9:
                frac = (v - lo) / (hi - lo)
                if 0.02 < frac < 0.985:      # evitar aglomerar en las esquinas de abajo
                    a = angle_for(frac)
                    px.text3(cx + rl * math.cos(a), cy - rl * math.sin(a),
                             str(int(round(v))), 2, LABEL)
                v += major
        # marcador de media de sesion (puntada externa, como manometro turbo)
        if avg and avg > lo:
            a = angle_for((avg - lo) / (hi - lo))
            ca, sa = math.cos(a), math.sin(a)
            px.line(cx + (r + th + 1) * ca, cy - (r + th + 1) * sa,
                    cx + (r + th + 4) * ca, cy - (r + th + 4) * sa, 3, "#e5e7eb")
        return px

    def _pxgeo(self, w, dh):
        pw = 2 * w
        pph = 4 * dh
        cx = pw / 2.0
        # pivote a 60%: equilibra el radio disponible arriba (r) y abajo (0.707r del arco)
        cy = pph * 0.60
        th = 2
        r = int(min(cy - th - 4, (pph - cy) / 0.71 - th - 4, pw / 2 - th - 4)) - 1
        if r < 18:
            th = 1
            r = int(min(cy - th - 4, (pph - cy) / 0.71 - th - 4, pw / 2 - th - 4)) - 1
        return pw, cx, cy, r, th

    # ---- frame completo ----
    def render(self, w, h, value, lo, hi, segments, major, minor,
               label, sub, color, big=False, avg=None) -> Text:
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
        pw, cx, cy, r, th = self._geo
        px = _Px()
        px.d.update(self._bg.d)
        frac = _clamp((value - lo) / max(1e-6, hi - lo))
        a = angle_for(frac)
        ca, sa = math.cos(a), math.sin(a)
        px.line(cx - 0.16 * r * ca, cy + 0.16 * r * sa,     # contrapeso
                cx + 0.86 * r * ca, cy - 0.86 * r * sa, 5, NEEDLE, width=3)
        px.dot_circle(cx, cy, 2.2, 6, color)                # cubo central
        t = _to_text(px, w, dh)
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
