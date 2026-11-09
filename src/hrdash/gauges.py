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
from .pixels import Px, clamp, to_text

# barrido estilo cuentavueltas: 0% en 225 deg, 100% en -45 deg (pasa por arriba)
A0 = 225.0
SWEEP = 270.0

DIM = "#374151"
LABEL = "#9ca3af"
NEEDLE = "#f8fafc"      # aguja clara: se ve sobre cualquier tramo del arco


def angle_for(frac: float) -> float:
    """Angulo (radianes, sentido antihorario, y arriba) del valor fraccionado."""
    return math.radians(A0 - SWEEP * clamp(frac))


class Gauge:
    """Gauge reutilizable con cache del fondo (arco/ticks/labels/media)."""

    def __init__(self):
        self._sig = None
        self._bg: Px | None = None
        self._geo: tuple | None = None

    # ---- fondo cacheado ----
    def _build_bg(self, w, dh, lo, hi, segments, major, minor, avg) -> Px:
        pw, cx, cy, r, th = self._pxgeo(w, dh)
        px = Px()
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
        px = Px()
        px.d.update(self._bg.d)
        frac = clamp((value - lo) / max(1e-6, hi - lo))
        a = angle_for(frac)
        ca, sa = math.cos(a), math.sin(a)
        px.line(cx - 0.16 * r * ca, cy + 0.16 * r * sa,     # contrapeso
                cx + 0.86 * r * ca, cy - 0.86 * r * sa, 5, NEEDLE, width=3)
        px.dot_circle(cx, cy, 2.2, 6, color)                # cubo central
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
