"""Pista de carreras en celdas braille: ovalo tipo velodromo visto desde arriba.

El fondo (asfalto, bordes, linea de meta y marcas de cuarto de vuelta) se cachea por
geometria, igual que `Gauge`; por frame solo se pinta el marcador de la bici y su
estela. La posicion se resuelve por longitud de arco sobre el centro del trazado,
asf el marcador queda literalmente sobre la pista (no aproximado al ancho).
"""

from __future__ import annotations

import bisect
import math

from rich.text import Text

from .pixels import Px, clamp, to_text

DIM = "#374151"        # asfalto
EDGE = "#9ca3af"       # borde exterior de la pista
INNER = "#6b7280"      # borde interior
META = "#e5e7eb"       # linea de meta
MARK = "#6b7280"       # marcas de cuarto de vuelta
LABEL = "#9ca3af"
TRAIL = "#4b5563"      # estela de las ultimas posiciones
RIDER = "#f8fafc"      # marcador de la bici

MARGIN = 2             # pixels de margen del framebuffer
BAND = 3               # grosor de la banda de asfalto (en pixels)
MIN_STRAIGHT = 8       # recta minima para que parezca un circuito y no un circulo
BACK = "#111827"       # color de fondo para atenuar el costado lejano (vista 3d)


def _mix(color: str, other: str, f: float) -> str:
    """Mezcla dos hex en la proporcion `f` de `other` (para atenuar la distancia)."""
    try:
        r1, g1, b1 = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        r2, g2, b2 = (int(other[i:i + 2], 16) for i in (1, 3, 5))
        return (f"#{int(r1 + (r2 - r1) * f):02x}"
                f"{int(g1 + (g2 - g1) * f):02x}{int(b1 + (b2 - b1) * f):02x}")
    except Exception:  # noqa: BLE001 - color inesperado: dejarlo tal cual
        return color


def _geo(w: int, dh: int):
    """Geometria del ovalo: (pw, pph, ax, bx, cy, r_out, band) o None si no entra."""
    pw, pph = 2 * w, 4 * dh
    r_out = int(min(pph / 2 - MARGIN - 1, pw / 2 - MARGIN - 1))
    if r_out < 9:
        return None
    straight = pw - 2 * MARGIN - 2 * r_out
    if straight < MIN_STRAIGHT:
        return None
    cy = pph / 2.0
    ax = MARGIN + r_out
    return pw, pph, ax, ax + straight, cy, r_out, BAND


def _dist_segment(px, py, ax, bx, cy) -> float:
    """Distencia del punto (px,py) al segmento central del circuito."""
    cx = clamp(px, ax, bx)
    return math.hypot(px - cx, py - cy)


class Track:
    """Pista reutilizable con cache del fondo (asfalto/bordes/meta/marcas)."""

    def __init__(self):
        self._sig = None
        self._bg: Px | None = None
        self._geo = None
        self._cum: list[float] = []        # longitud acumulada de cada punto
        self._pts: list[tuple[float, float]] = []
        self._total = 0.0
        self._persp = False                 # vista 3d: proyecta el ovalo con perspectiva
        self._cx = 0.0
        self._cy = 0.0
        self._r_out = 0.0

    # ---- fondo cacheado ----
    def _build_bg(self, w, dh, lap_km):
        pw, pph, ax, bx, cy, r_out, band = self._geo
        self._cx, self._cy, self._r_out = pw / 2.0, cy, r_out
        r_in = r_out - band
        px = Px()

        def tone(col, y):
            """En vista 3d atenua el costado lejano (lo que queda sobre el eje)."""
            return _mix(col, BACK, 0.5) if (self._persp and y < self._cy) else col

        # polyline del centro del trazado (meta en el medio de la recta de arriba)
        pts = self._path(ax, bx, cy, r_out - band / 2.0)
        # asfalto: eje discontinuo (se ve como pista sin rellenar, para que el
        # marcador no se pierda cuando la terminal no pinta colores)
        for i, (x, y) in enumerate(pts):
            if i % 4 == 0:
                px.put(x, y, 1, tone(DIM, y))
        # bordes: desplazados +/- (band/2 + 1) sobre la normal
        for i, (x, y) in enumerate(pts):
            n = self._normal(x, y, ax, bx, cy)
            d = band / 2.0 + 1
            px.put(x + n[0] * d, y + n[1] * d, 2, tone(EDGE, y))
            px.put(x - n[0] * d, y - n[1] * d, 2, tone(INNER, y))
        # linea de meta cuadriculada en el centro de la recta superior
        mx = (ax + bx) / 2.0
        my = cy - (r_out - band / 2.0)
        for i in range(int(band) + 2):
            yy = my + (i - band / 2.0)
            px.put(mx, yy, 4, tone(META if i % 2 == 0 else "#111827", my))
        # marcas de cuarto de vuelta (1/4, 1/2, 3/4 de la vuelta): la fuente 3x5 no
        # cabe entera en una celda braille de 2x4, asi que solo se dibuja el tick.
        for f in (0.25, 0.5, 0.75):
            s = f * self._total
            idx = bisect.bisect_right(self._cum, s) - 1
            x, y = pts[max(0, idx)]
            n = self._normal(x, y, ax, bx, cy)
            px.line(x, y, x + n[0] * band, y + n[1] * band, 3, tone(MARK, y))
            px.line(x, y, x - n[0] * (band + 3), y - n[1] * (band + 3), 3, tone(LABEL, y))
        return px

    def _path(self, ax, bx, cy, rm):
        """Polyline del centro: meta -> recta derecha -> curva -> recta -> curva -> meta."""
        pts = []
        cum = []
        acc = 0.0
        prev = None

        def add(x, y):
            nonlocal acc, prev
            if self._persp:
                span = max(self._r_out, 1.0)
                k = max(0.5, 1.0 - 0.32 * abs(y - self._cy) / span)
                x, y = self._cx + (x - self._cx) * k, self._cy + (y - self._cy) * k
            if prev:
                acc += math.hypot(x - prev[0], y - prev[1])
            pts.append((x, y))
            cum.append(acc)
            prev = (x, y)

        mx, my = (ax + bx) / 2.0, cy - rm
        add(mx, my)                                  # meta
        for x in range(int(mx), int(bx) + 1):        # recta superior, hacia la curva
            add(x, my)
        for step in range(int(rm * math.pi) + 1):    # curva derecha (arriba -> abajo)
            t = -math.pi / 2 + (step / max(1, rm * math.pi)) * math.pi
            add(bx + rm * math.cos(t), cy + rm * math.sin(t))
        bot = cy + rm
        for x in range(int(bx), int(ax) - 1, -1):    # recta inferior, de vuelta
            add(x, bot)
        for step in range(int(rm * math.pi) + 1):    # curva izquierda (abajo -> arriba)
            t = math.pi / 2 + (step / max(1, rm * math.pi)) * math.pi
            add(ax + rm * math.cos(t), cy + rm * math.sin(t))
        for x in range(int(ax), int(mx) + 1):        # medio tramo superior que falta
            add(x, my)
        self._cum, self._pts, self._total = cum, pts, acc
        return pts

    @staticmethod
    def _normal(x, y, ax, bx, cy):
        """Normal exterior del trazado en el punto (x,y) del polyline central."""
        cx = clamp(x, ax, bx)
        dx, dy = x - cx, y - cy
        ln = math.hypot(dx, dy) or 1.0
        return dx / ln, dy / ln

    def _point_at(self, frac):
        """Punto (x, y) sobre el centro de la pista y su direccion de marcha."""
        if not self._pts:
            return 0.0, 0.0, 0.0, 0.0
        s = clamp(frac, 0.0, 0.9999) * self._total
        i = max(0, bisect.bisect_right(self._cum, s) - 1)
        x, y = self._pts[i]
        j = min(i + 1, len(self._pts) - 1)
        k = max(i - 1, 0)
        dx, dy = self._pts[j][0] - self._pts[k][0], self._pts[j][1] - self._pts[k][1]
        ln = math.hypot(dx, dy) or 1.0
        return x, y, dx / ln, dy / ln

    # ---- frame completo ----
    def render(self, w, h, frac, label, sub, color, trail=(), lap_km=None,
               persp: bool = False) -> Text:
        w, h = int(max(12, w or 12)), int(max(4, h or 4))
        lap_km = lap_km if lap_km else 5.0
        dh = h - 2
        geo = _geo(w, dh)
        if not geo:
            return self._fallback(w, label, sub, color, frac)
        sig = (w, dh, round(lap_km, 2), persp)
        if sig != self._sig:
            self._persp = persp
            self._geo = geo
            self._bg = self._build_bg(w, dh, lap_km)
            self._sig = sig
        px = Px()
        px.d.update(self._bg.d)
        for f in (trail or ())[-14:]:                # estela de las posiciones previas
            x, y, _, _ = self._point_at(f)
            px.put(x, y, 3, TRAIL)
        x, y, tx, ty = self._point_at(frac)
        px.line(x - tx * 4, y - ty * 4, x + tx * 4, y + ty * 4, 5, RIDER, width=2)
        px.cell(int(x // 2), int(y // 4), 6, color)          # la bici: celda braille llena
        t = to_text(px, w, dh)
        t.append("\n" + label.center(w), style=f"bold {color}")
        t.append("\n" + sub[:w].center(w), style=LABEL)
        return t

    @staticmethod
    def _fallback(w, label, sub, color, frac) -> Text:
        """Poca altura: barra de progreso con la meta y el marcador."""
        fill = int(round(clamp(frac) * (w - 4)))
        t = Text(justify="center")
        t.append(label.center(w), style=f"bold {color}")
        t.append("\n")
        t.append("[" + "█" * fill + "●" + "░" * (w - 4 - fill) + "]", style=LABEL)
        t.append("\n" + sub[:w].center(w), style=LABEL)
        return t
