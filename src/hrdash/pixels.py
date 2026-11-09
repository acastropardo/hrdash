"""Framebuffer de pixels (2x4 por celda braille) compartido por gauges y pista.

`Px` guarda (x, y) -> (orden, color) con z-order, asi la pieza movil (aguja o
marcador de la bici) tapa el fondo sin reordenar nada. `to_text()` empaqueta los
pixels en celdas braille, que es lo que Textual muestra. Los modulos de dibujo
cachean el fondo por geometria y solo redibujan la pieza movil por frame.
"""

from __future__ import annotations

import math

from rich.text import Text

# bit braille segun (dx, dy) dentro de la celda de 2x4 pixeles
_BR = {(0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
       (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80}

# fuente minima 3x5 para numeros de ticks
F3 = {
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
    ".": ("000", "000", "000", "000", "100"),
    ",": ("000", "000", "000", "100", "000"),
    "-": ("000", "000", "111", "000", "000"),
    " ": ("000", "000", "000", "000", "000"),
}


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class Px:
    """Framebuffer de pixeles con z-order: (x, y) -> (orden, color)."""

    def __init__(self):
        self.d: dict[tuple[int, int], tuple[int, str]] = {}

    def put(self, x: float, y: float, order: int, color: str) -> None:
        self.d[(int(round(x)), int(round(y)))] = (order, color)

    def cell(self, c: int, r: int, order: int, color: str) -> None:
        """Rellena la celda braille completa (x, y) en coordenadas de celda."""
        for dy in range(4):
            for dx in range(2):
                self.put(2 * c + dx, 4 * r + dy, order, color)

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
            g = F3.get(ch, F3[" "])
            for r, row in enumerate(g):
                for c, bit in enumerate(row):
                    if bit == "1":
                        self.put(x0 + i * 4 + c, int(round(y)) - 2 + r, order, color)


def to_text(px: Px, w: int, h: int) -> Text:
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
