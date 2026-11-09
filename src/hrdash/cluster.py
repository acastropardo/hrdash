"""Cluster 3D de la vista tablero: los mismos 4 diales + panel digital, a pantalla llena.

La geometria se construye en **espacio panel** (x = derecha, y = alto, z = hacia el
piloto) y `Mesh.panel()` la voltea al mundo, asi los diales miran de frente a la camara.
Los valores van en **7 segmentos** y los textos en fuente pixel **5x7 fusionando las
rayas de cada fila** (un glifo cuesta ~5 cajas, no 35): sin atlas de fuente y sin
dependencias nuevas.

Contenido equivalente a la vista `g` de la TUI:

- Dial FC (grande, izq.) con zonas Z1..Z5, redline, aguja, digitos y etiquetas.
- Dial de potencia (grande, der.) con bandas de %FTP, redline, aguja y digitos.
- Dial de velocidad y dial de cadencia (medianos, abajo a los costados).
- Barra LED vertical de FC en el centro.
- Tira digital: arriba tiempo/kcal/distancia; al pie zona, muestras y ritmo; en el
  medio FC med/max/% y W med/max/FTP/en zona (como el panel de a bordo 2D).
- Pilotos de zona FC y de potencia, con alarma pulsante sobre 95 %FCmax.
- Self-test al abrir (barrido de las 4 agujas) y balanceo sutil de camara.
"""

from __future__ import annotations

import math

from .glview import Mesh, _dial, _hex

# ---------------------------------------------------------------- tipografia
FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("11110", "00001", "00010", "00100", "01000", "10000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10011", "01111"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "01100", "01100", "00100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "%": ("11001", "11010", "00100", "00100", "01011", "10011", "00000"),
    "(": ("00110", "01000", "10000", "10000", "10000", "01000", "00110"),
    ")": ("01100", "00010", "00001", "00001", "00001", "00010", "01100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
}

# segmentos por digito: a arriba, b/c derecha, d abajo, e/f izquierda, g centro
SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
       "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
       "-": "g", " ": ""}

INK = (0.60, 0.66, 0.72)          # texto secundario
AMBER = (0.99, 0.83, 0.38)        # promedios
GREEN = (0.45, 0.90, 0.55)        # kcal / FTP
CYAN = (0.36, 0.85, 0.93)         # distancia / velocidad
RED = (0.95, 0.40, 0.34)          # maximmos
WHITE = (0.90, 0.93, 0.97)


def _charw(h: float) -> float:
    return h * (5.0 / 7.0 + 0.30)


def text_w(s: str, h: float) -> float:
    """Ancho de una cadena en la fuente 5x7 (para centrar)."""
    return len(s) * _charw(h) if s else 0.0


def draw_text(mesh, x, y, z, h, s, col, light=0.0, align="l", depth=0.03) -> None:
    """Escribe `s` en fuente pixel 5x7, fusionando la corrida de cada fila en una caja."""
    if align == "c":
        x -= text_w(s, h) / 2.0
    elif align == "r":
        x -= text_w(s, h)
    px = h / 7.0
    cw = _charw(h)
    for i, ch in enumerate(s.upper()):
        g = FONT.get(ch)
        if not g:
            continue
        gx = x + i * cw
        for row in range(7):
            bits = g[row]
            c = 0
            while c < 5:
                if bits[c] != "1":
                    c += 1
                    continue
                run = 1
                while c + run < 5 and bits[c + run] == "1":
                    run += 1
                mesh.box(gx + (c + run / 2.0 - 0.5) * px, y + (3.0 - row) * px, z,
                         run * px * 0.9, px * 0.9, depth, col, light=light)
                c += run


def draw_digits(mesh, x, y, z, h, s, col, light=0.9) -> None:
    """Valor en digitos de 7 segmentos (7 cajas por digito, como un cuadro de auto)."""
    w = h * 0.58
    t = h * 0.14
    step = w + h * 0.28
    for i, ch in enumerate(s):
        gx = x + i * step
        if ch == ":":                                # los dos puntos van como dos puntos
            mesh.box(gx + w / 2, y + h * 0.17, z, t, t, t, col, light=light)
            mesh.box(gx + w / 2, y - h * 0.17, z, t, t, t, col, light=light)
            continue
        on = SEG.get(ch, "")
        if "a" in on:
            mesh.box(gx + w / 2, y + h / 2, z, w, t, t, col, light=light)
        if "g" in on:
            mesh.box(gx + w / 2, y, z, w, t, t, col, light=light)
        if "d" in on:
            mesh.box(gx + w / 2, y - h / 2, z, w, t, t, col, light=light)
        if "f" in on:
            mesh.box(gx, y + h / 4, z, t, h / 2, t, col, light=light)
        if "b" in on:
            mesh.box(gx + w, y + h / 4, z, t, h / 2, t, col, light=light)
        if "e" in on:
            mesh.box(gx, y - h / 4, z, t, h / 2, t, col, light=light)
        if "c" in on:
            mesh.box(gx + w, y - h / 4, z, t, h / 2, t, col, light=light)


# ---------------------------------------------------------------- dial
def _brighten(col, f=1.35):
    """Color al alza para los palitos encendidos (que se lean como LEDs, no como pintura)."""
    return (min(1.0, col[0] * f), min(1.0, col[1] * f), min(1.0, col[2] * f))


def dial_static(m, r, lo, hi, segs, major, minor, label, unit, big=True):
    """Cara, bisel, banda de zonas, palitos, numeros y etiquetas.

    Estructura de tacometro: banda de zonas en la corona, palitos por dentro, numeros
    todavia mas adentro, lectura digital en el hueco de las 12 y nombre/unidad abajo.
    """
    m.disc(0, 0, 0.0, r * 1.06, (0.115, 0.128, 0.150), segs=44)   # corona externa
    m.disc(0, 0, 0.014, r, (0.205, 0.225, 0.255), segs=44)              # cara
    m.ring(0, 0, 0.026, r, r * 1.06, (0.40, 0.45, 0.52), segs=44, light=0.2)
    m.ring(0, 0, 0.03, r * 0.50, r * 0.515, (0.30, 0.34, 0.40), segs=40)
    for f0, f1, chex in segs:                        # banda de zonas, ancha y legible
        col = _hex(chex)
        prev = None
        g = f0
        while g <= f1 + 1e-9:
            a = _dial(g)
            p = (math.cos(a) * r * 0.885, math.sin(a) * r * 0.885)
            if prev:
                mid = ((prev[0] + p[0]) / 2, (prev[1] + p[1]) / 2)
                ln = math.hypot(p[0] - prev[0], p[1] - prev[1])
                m.box(mid[0], mid[1], 0.05, ln + r * 0.03, r * 0.13, 0.05,
                      col, yaw=math.atan2(p[1] - prev[1], p[0] - prev[0]), light=0.65)
            prev = p
            g += 0.006
            if f0 == f1:
                break
    if minor and hi > lo and major:
        rr = r * 0.62
        # paso de etiqueta: se salta mayores si sus numeros no caben uno al lado del otro
        a0 = _dial(0.5)
        a1 = _dial(0.5 + major / max(1e-6, hi - lo))
        chord = math.hypot(rr * (math.cos(a1) - math.cos(a0)), rr * (math.sin(a1) - math.sin(a0)))
        need = text_w("888", r * 0.12) * 1.40
        k = max(1, int(math.ceil(need / max(1e-6, chord))))
        v = math.ceil(lo / minor) * minor
        while v <= hi + 1e-9:
            frac = (v - lo) / (hi - lo)
            if (abs(v / major - round(v / major)) < 1e-9 and 0.05 < frac < 0.95
                    and int(round(v / major)) % k == 0):
                a = _dial(frac)
                s = str(int(round(v)))
                draw_text(m, math.cos(a) * rr - text_w(s, r * 0.12) / 2,
                          math.sin(a) * rr - r * 0.06, 0.05, r * 0.12, s, INK, light=0.45)
            v += minor
    # unidad y nombre en el hueco de las 6 en punto (ahi no llegan palitos ni numeros)
    draw_text(m, -text_w(unit, r * 0.17) / 2, -r * 0.62, 0.05, r * 0.17, unit,
              (0.58, 0.64, 0.70), light=0.5)
    if big:
        draw_text(m, -text_w(label, r * 0.12) / 2, -r * 0.80, 0.05, r * 0.12, label,
                  INK, light=0.45)


def dial_frame(m, r, value, lo, hi, major, minor, redline, col_now, alarm):
    """Capa por frame: palitos (iluminados hasta el valor) + aguja con contrapeso."""
    frac = max(0.0, min(1.0, (value - lo) / max(1e-6, hi - lo)))
    if minor and hi > lo:
        v = math.ceil(lo / minor) * minor
        while v <= hi + 1e-9:
            f = (v - lo) / (hi - lo)
            maj = bool(major) and abs(v / major - round(v / major)) < 1e-9
            a = _dial(f)
            lit = f <= frac
            over = redline is not None and f >= redline
            if lit and over:
                col = (0.98, 0.30, 0.30)
            elif lit:
                col = _brighten(col_now)
            else:
                col = (0.42, 0.47, 0.54)
            ln = r * (0.17 if maj else 0.09)
            base = r * 0.80 - ln / 2.0
            m.box(math.cos(a) * base, math.sin(a) * base, 0.06,
                  ln, r * (0.062 if maj else 0.030), r * 0.06,
                  col, yaw=a, light=1.0 if lit else 0.25)
            v += minor
    a = _dial(frac)
    ca, sa = math.cos(a), math.sin(a)
    nc = (0.98, 0.34, 0.26) if alarm else (0.97, 0.98, 1.0)
    m.box(ca * r * 0.42, sa * r * 0.42, 0.15, r * 0.86, r * 0.055, r * 0.06,
          nc, yaw=a, light=0.45 if alarm else 0.25)
    m.box(-ca * r * 0.14, -sa * r * 0.14, 0.15, r * 0.28, r * 0.09, r * 0.065,
          nc, yaw=a, light=0.3)
    m.box(0, 0, 0.12, r * 0.19, r * 0.19, r * 0.11, (0.20, 0.23, 0.28))
    m.ring(0, 0, 0.185, r * 0.065, r * 0.105, col_now, segs=16, light=0.95)


def dial_values(m, r, digits, dh, alarm, avg=None):
    """Capa por snapshot: lectura digital en las 6 en punto + media (cambia a 1 Hz)."""
    h = r * dh
    step = h * 0.86                                # ancho por digito en 7-seg
    total = (len(digits) - 1) * step + h * 0.58
    draw_digits(m, -total / 2, -r * 0.10, 0.22, h, digits, RED if alarm else AMBER, light=0.95)
    if avg is not None:
        s = "%3d" % round(avg) if avg >= 10 else "%04.1f" % avg
        hh = r * 0.13
        tot = (len(s) - 1) * hh * 0.86 + hh * 0.58
        draw_text(m, -text_w("MED", hh) - tot / 2 - hh, -r * 0.80, 0.06, hh, "MED",
                  (0.52, 0.58, 0.64), light=0.4)
        draw_digits(m, -tot / 2, -r * 0.80, 0.06, hh, s, (0.75, 0.80, 0.86), light=0.6)


# ---------------------------------------------------------------- cluster
BIG_R, SMALL_R = 1.18, 0.78
DIALS = (                      # (clave, centro x, centro z, radio, etiqueta, unidad)
    ("hr", -2.30, 0.72, BIG_R, "FRECUENCIA", "BPM"),
    ("w", 2.30, 0.72, BIG_R, "POTENCIA", "%FTP"),
    ("v", -3.60, -2.08, SMALL_R, "VELOCIDAD", "KM/H"),
    ("c", 3.60, -2.08, SMALL_R, "CADENCIA", "RPM"),
)
HALF_X, HALF_Z = 4.70, 2.95     # extensores del cluster: enmarcan a pantalla llena
ZCOLORS_FALLBACK = ["#6b7280", "#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]


class Cluster:
    """Arma el cluster en 3 capas: estatica (raro), valores (1 Hz) y agujas (por frame)."""

    def __init__(self):
        self._cache = {}

    def build(self, snap, anim, aspect):
        """([(nombre, malla_o_None)], camara); None en una capa = conservar su VBO."""
        cfg = self._scales(snap)
        layers = []
        if cfg != self._cache.get("cfg"):
            self._cache["cfg"] = cfg
            layers.append(("static", self._static(cfg)))
        else:
            layers.append(("static", None))
        vsig = (snap.get("version"), round(cfg["hr"][1]), round(cfg["w"][1]))
        if vsig != self._cache.get("vsig"):
            self._cache["vsig"] = vsig
            layers.append(("values", self._values(snap, cfg)))
        else:
            layers.append(("values", None))
        layers.append(("frame", self._frame(snap, anim, cfg)))
        return layers, _cam(anim, aspect)

    # ---- estatica ----
    def _static(self, cfg):
        m = Mesh()
        m.panel_backplate(-HALF_X + 0.30, HALF_X - 0.30, -3.08, 2.80, 0.35, 0.16)
        m.floor(6.6, 0.6, (0.13, 0.15, 0.19), z0=-3.40)
        for which, cx, cz, r, label, unit in DIALS:
            lo, hi, segs, major, minor = cfg[which]
            one = Mesh()
            one.panel(cx, cz)
            dial_static(one, r, lo, hi, segs, major, minor, label, unit, big=r >= 0.9)
            m.merge(one)
        return m

    # ---- valores (1 Hz): digitos de cada dial, barra LED, pilotos y tiras ----
    def _values(self, snap, cfg):
        m = Mesh()
        vals = {"hr": float(snap.get("hr") or 0), "w": float(snap.get("watts") or 0),
                "v": float(snap.get("speed") or 0), "c": float(snap.get("cadence") or 0)}
        alarm = bool(snap.get("alarm"))
        avgs = {"v": float(snap.get("avg_speed") or 0), "c": float(snap.get("avg_cadence") or 0)}
        for which, cx, cz, r, _label, _unit in DIALS:
            one = Mesh()
            one.panel(cx, cz)
            dial_values(one, r, _digits_for(which, vals[which]), 0.24,
                        alarm and which == "hr", avg=avgs.get(which))
            m.merge(one)
        zc = _hex(snap.get("zone_color") or "#ef4444")
        _led_bar(m, float(snap.get("hr") or 0), cfg["hr"][0], cfg["hr"][1], zc)
        _lamps(m, snap)
        _strips(m, snap)
        return m

    # ---- por frame: palitos iluminados y agujas con inercia ----
    def _frame(self, snap, anim, cfg):
        m = Mesh()
        vals = {"hr": float(snap.get("hr") or 0), "w": float(snap.get("watts") or 0),
                "v": float(snap.get("speed") or 0), "c": float(snap.get("cadence") or 0)}
        cols = {"hr": _hex(snap.get("zone_color") or "#ef4444"),
                "w": _hex(snap.get("wzone_color") or "#facc15"),
                "v": CYAN, "c": (0.66, 0.55, 0.98)}
        alarm = bool(snap.get("alarm"))
        sweep = _sweep(anim)
        for which, cx, cz, r, _label, _unit in DIALS:
            lo, hi, _segs, major, minor = cfg[which]
            if sweep is None:
                prev = anim.get(which, 0.0)
                k = 0.5 if vals[which] > prev else 0.14    # inercia asimetria de la TUI
                anim[which] = prev + (vals[which] - prev) * k
            else:
                anim[which] = sweep * hi                   # self-test: barrido 0 -> max -> 0
            one = Mesh()
            one.panel(cx, cz)
            dial_frame(one, r, anim[which], lo, hi, major, minor,
                       0.90 if which == "hr" else (0.86 if which == "w" else None),
                       cols[which], alarm and which == "hr")
            m.merge(one)
        if alarm:                                    # halo rojo pulsante sobre el dial de FC
            p = 0.45 + 0.55 * math.sin(anim["t"] * 7.0)
            _w, cx, cz, r = DIALS[0][0], DIALS[0][1], DIALS[0][2], DIALS[0][3]
            one = Mesh()
            one.panel(cx, cz)
            one.ring(0, 0, 0.03, r * 1.14, r * 1.21, (0.98, 0.30, 0.24), segs=36,
                     light=0.5 + 0.5 * p)
            m.merge(one)
        return m

    # ---- escalas (mismas formulas que el tablero 2D de la TUI) ----
    @staticmethod
    def _scales(snap):
        hrmax = max(60.0, float(snap.get("hrmax") or 190))
        lo_h, hi_h = 40.0, float(math.ceil(max(hrmax + 10, float(snap.get("max_hr") or 0) + 10) / 10) * 10)
        segs_h = []
        for z in (snap.get("zones") or []):
            f0 = max(0.0, min(1.0, (z[0] / 100.0 * hrmax - lo_h) / (hi_h - lo_h)))
            f1 = max(0.0, min(1.0, (min(z[1], 100.0) / 100.0 * hrmax - lo_h) / (hi_h - lo_h)))
            if f1 > f0:
                segs_h.append((round(f0, 4), round(f1, 4), z[2]))
        ftp = max(50.0, float(snap.get("ftp") or 240))
        hi_w = float(math.ceil(max(ftp * 1.25, float(snap.get("max_watts") or 0) + 25, 100) / 25) * 25)
        segs_w = []
        for b in (snap.get("bands") or []):
            f0 = max(0.0, min(1.0, b[0] / 100.0 * ftp / hi_w))
            f1 = max(0.0, min(1.0, min(b[1], 900.0) / 100.0 * ftp / hi_w))
            if f1 > f0:
                segs_w.append((round(f0, 4), round(f1, 4), b[2]))
        hi_v = float(math.ceil(max(40.0, float(snap.get("max_speed") or 0) + 5) / 10) * 10)
        hi_c = 160.0 if float(snap.get("max_cadence") or 0) > 130 else 140.0
        return {"hr": (lo_h, hi_h, segs_h, 20, 5),
                "w": (0.0, hi_w, segs_w, 50 if hi_w >= 200 else 25,
                      (50 if hi_w >= 200 else 25) // 2),
                "v": (0.0, hi_v, [(0.0, 1.0, "#22d3ee")], 10, 5),
                "c": (0.0, hi_c, [(0.0, 1.0, "#a78bfa")], 20, 10)}

def _digits_for(which, v):
    if which in ("hr", "w"):
        return "%3d" % round(v) if v else "--"
    if which == "v":
        return "%04.1f" % v if v else "--.-"
    return "%3d" % round(v) if v else "--"


def _sweep(anim):
    """Self-test del tablero: 0 -> max -> 0 en 1.1 s al abrir (None despues)."""
    t = anim.get("t", 0.0)
    if t >= 1.1:
        return None
    return math.sin(t / 1.1 * math.pi)


def _cam(anim, aspect):
    """Framing por aspect (el cluster llena la ventana) con balanceo sutil ±6 grados.

    Casi de frente: con mucha pitch el panel se ve trapecio y los diales se aplastan.
    """
    fovy = 0.62
    tan = math.tan(fovy / 2.0)
    d = max(HALF_Z / tan, HALF_X / (max(0.35, aspect) * tan)) * 1.10
    a = 0.062 * math.sin(anim["t"] * 0.70)                      # ±3.5 grados de balanceo
    pitch = 0.012 * math.sin(anim["t"] * 0.45)
    eye = (math.sin(a) * d, -math.cos(a) * d, (0.06 + pitch) * d)
    return eye, (0.0, 0.0, -0.14), (0.0, 0.0, 1.0), fovy


def _led_bar(m, hr, lo, hi, zc):
    """Columna LED de FC entre los dos diales grandes: encendida hasta el valor."""
    x, z0, z1, n = 0.0, -0.44, 1.62, 20
    frac = max(0.0, min(1.0, (hr - lo) / max(1e-6, hi - lo))) if hr else 0.0
    m.box(x, 0.08, (z0 + z1) / 2, 0.42, 0.10, z1 - z0 + 0.30, (0.12, 0.14, 0.18))
    for i in range(n):
        f = i / (n - 1)
        lit = f <= frac
        col = (0.98, 0.28, 0.28) if (lit and f > 0.88) else (zc if lit else (0.22, 0.25, 0.30))
        m.box(x, -0.02, z0 + (z1 - z0) * f, 0.30, 0.10, (z1 - z0) / n * 0.62,
              col, light=0.95 if lit else 0.1)
    draw_text(m, -text_w("FC", 0.14) / 2, 0.0, z1 + 0.22, 0.14, "FC", INK, light=0.35)


def _lamps(m, snap):
    """Pilotos de zona FC y de banda de potencia: el activo encendido, el resto apagado."""
    zs = snap.get("zcolors") or ZCOLORS_FALLBACK
    ws = snap.get("wcolors") or ZCOLORS_FALLBACK
    zi, wi = int(snap.get("zone_idx") or 0), int(snap.get("wband") or 0)
    for z, (cols, idx, lab) in enumerate(((zs, zi, "ZONA FC"), (ws, wi, "BANDA W"))):
        zz = -2.36 - z * 0.30
        draw_text(m, -1.42, 0.0, zz, 0.13, lab, (0.46, 0.52, 0.58), light=0.3, align="r")
        for i, chex in enumerate(cols):
            lit = i == idx
            m.box(-1.20 + i * 0.50, 0.0, zz, 0.36, 0.10, 0.16,
                  _hex(chex) if lit else (0.19, 0.21, 0.25), light=0.95 if lit else 0.05)


def _strips(m, snap):
    """Tiras digitales: arriba tiempo/kcal/dist, al centro promedios, al pie el estado."""
    h = 0.17
    for i, (lab, val, col) in enumerate((
            ("TIEMPO", _clock(snap.get("elapsed_s") or 0), WHITE),
            ("KCAL", "%4.0f" % float(snap.get("total_kcal") or 0), GREEN),
            ("DIST KM", "%04.1f" % float(snap.get("dist_km") or 0), CYAN))):
        x = -3.95 + i * 2.60
        draw_text(m, x, 0.0, 2.44, h * 0.66, lab, (0.62, 0.68, 0.74), light=0.55)
        draw_digits(m, x, 0.0, 2.18, h, val, col, light=0.95)

    for i, (lab, val, col) in enumerate((
            ("FC MED", "%3d" % round(float(snap.get("avg_hr") or 0)), AMBER),
            ("FC MAX", "%3d" % int(snap.get("max_hr") or 0), RED),
            ("%FCMAX", "%2.0f" % float(snap.get("pct_max") or 0), WHITE),
            ("EST", "%3d" % int(snap.get("hrmax") or 0), INK))):
        x = -2.30 + i * 1.55
        draw_text(m, x, 0.0, -0.82, h * 0.58, lab, (0.62, 0.68, 0.74), light=0.5)
        draw_digits(m, x, 0.0, -1.08, h * 0.92, val, col, light=0.9)
    for i, (lab, val, col) in enumerate((
            ("W MED", "%3d" % round(float(snap.get("avg_watts") or 0)), AMBER),
            ("W MAX", "%3d" % int(snap.get("max_watts") or 0), RED),
            ("FTP", "%3d" % int(snap.get("ftp") or 0), GREEN),
            ("EN ZONA", _clock(snap.get("zone_secs") or 0), WHITE))):
        x = -2.30 + i * 1.55
        draw_text(m, x, 0.0, -1.44, h * 0.58, lab, (0.62, 0.68, 0.74), light=0.5)
        draw_digits(m, x, 0.0, -1.70, h * 0.92, val, col, light=0.9)

    draw_text(m, 0.0, 0.0, -2.94, 0.12,
              "Z%d %s · %d MUESTRAS · %2.1f KCAL/MIN · %2.1f W/KG · %dA %dKG %s" % (
                  int(snap.get("zone_idx") or 0),
                  (snap.get("zone_name") or "")[:8].upper(),
                  int(snap.get("n") or 0), float(snap.get("kcal_min") or 0),
                  float(snap.get("wkg") or 0),
                  int(snap.get("age") or 0), round(float(snap.get("weight") or 0)),
                  (snap.get("sex") or "M")),
              INK, light=0.45, align="c")


def _clock(secs) -> str:
    """H:MM:SS o MM:SS para la tira digital; guiones si no hay dato."""
    try:
        s = int(secs)
    except Exception:  # noqa: BLE001
        return "--:--"
    h, rem = divmod(max(0, s), 3600)
    mnt, sec = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, mnt, sec) if h else "%02d:%02d" % (mnt, sec))


CLUSTER = Cluster()