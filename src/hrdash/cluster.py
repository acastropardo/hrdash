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
    "%": ("11010", "11010", "00100", "01001", "01011", "10011", "00000"),
    "(": ("00110", "01000", "10000", "10000", "10000", "01000", "00110"),
    ")": ("01100", "00010", "00001", "00001", "00001", "00010", "01100"),
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    # simbolos habituales en nombres de dispositivos ("Rhythm+", "KICKR_1234", "H10 #2")
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    "?": ("01110", "10001", "00001", "00110", "00100", "00000", "00100"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "#": ("01010", "01010", "11111", "01010", "11111", "01010", "01010"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "*": ("00000", "00100", "10101", "01110", "10101", "00100", "00000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
}

# segmentos por digito: a arriba, b/c derecha, d abajo, e/f izquierda, g centro
SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
       "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
       "-": "g", " ": ""}

INK = (0.78, 0.83, 0.88)          # texto secundario (mas claro: se leia como humo)
DIM = (0.46, 0.52, 0.58)          # etiquetas de las tiras
UNLIT = (0.32, 0.36, 0.42)        # pilotos y segmentos apagados: grises con presencia
AMBER = (0.99, 0.83, 0.38)        # promedios
GREEN = (0.45, 0.90, 0.55)        # kcal / FTP
CYAN = (0.36, 0.85, 0.93)         # distancia / velocidad
RED = (0.95, 0.40, 0.34)          # maximmos
WHITE = (0.90, 0.93, 0.97)


def _charw(h: float) -> float:
    return h * (5.0 / 7.0 + 0.42)     # avance holgado: con trazos gordos las letras se pegan


def digits_w(s: str, h: float) -> float:
    """Ancho real de un valor en 7 segmentos (punto y dos puntos avanzan menos)."""
    step = h * 0.86
    tot = 0.0
    for i, ch in enumerate(s):
        if ch == ".":
            tot += step * 0.45
        elif ch == ":":
            tot += step * 0.55
        else:
            tot += step
    return tot - (step - h * 0.58) if s else 0.0


def text_w(s: str, h: float) -> float:
    """Avance total de una cadena en la fuente 5x7 (incluye el hueco tras la ultima letra)."""
    return len(s) * _charw(h) if s else 0.0


def ink_w(s: str, h: float) -> float:
    """Ancho del trazo real: sin el hueco final y con el ancho de los trazos gordos.

    Centrar con `text_w` desplaza el texto ~0,2 h hacia la izquierda (el hueco tras la
    ultima letra cuenta como texto); el trazo de la primera columna sale 0,59 px a la izquierda
    de `gx` y el de la ultima 4,59 px a la derecha de su `gx`.
    """
    return ((len(s) - 1) * _charw(h) + 5.18 * h / 7.0) if s else 0.0


def draw_text(mesh, x, y, z, h, s, col, light=0.0, align="l", depth=0.03) -> None:
    """Escribe `s` en fuente pixel 5x7 con trazos gordos y alivio poco profundo.

    `y` es el centro vertical de las letras. `align` "c"/"r" centra o alinea a la derecha
    el trazo real (`ink_w`); con "l" `x` es el origen de la primera celda, como siempre. Con
    `depth` = 0 cada trazo es un quad plano en vez de una caja (para texto visto de frente).
    """
    px = h / 7.0
    if align == "c":
        x += -ink_w(s, h) / 2.0 + 0.59 * px
    elif align == "r":
        x += -ink_w(s, h) + 0.59 * px
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
                cx, cy = gx + (c + run / 2.0 - 0.5) * px, y + (3.0 - row) * px
                if depth > 0:
                    mesh.box(cx, cy, z, run * px + px * 0.18, px * 0.82, depth, col, light=light)
                else:                      # plano: solo se ve de frente (menu), 10x mas barato
                    hw, hh = (run * px + px * 0.18) / 2.0, px * 0.41
                    mesh.quad((cx - hw, cy - hh, z), (cx + hw, cy - hh, z), (cx + hw, cy + hh, z),
                              (cx - hw, cy + hh, z), col, (0.0, 0.0, 1.0), light)
                c += run


def digits_extent(s: str, h: float):
    """(izquierda, derecha) del trazo real de un valor en 7 segmentos, respecto de `x`.

    Un `1` solo pinta el lado derecho de su celda: centrar por celdas deja un hueco a la
    izquierda en "148". Aqui se mide el trazo de cada caracter.
    """
    w, t = h * 0.58, h * 0.14
    step = w + h * 0.28
    gx, lo, hi = 0.0, None, None
    for ch in s:
        if ch == ".":
            a, b, adv = gx, gx + t, step * 0.45
        elif ch == ":":
            a, b, adv = gx, gx + t, step * 0.55
        elif ch == " " or ch not in SEG:
            a = b = None
            adv = step
        elif SEG[ch] == "bc":
            a, b, adv = gx + w - t / 2, gx + w + t / 2, step
        else:
            a, b, adv = gx - t / 2, gx + w + t / 2, step
        if a is not None:
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        gx += adv
    return (lo or 0.0, hi or 0.0)


def draw_digits(mesh, x, y, z, h, s, col, light=0.9) -> None:
    """Valor en digitos de 7 segmentos; el punto y los dos puntos avanzan medio ancho."""
    w = h * 0.58
    t = h * 0.14
    step = w + h * 0.28
    gx = x
    for ch in s:
        if ch == ".":                                # punto: mitad de avance
            mesh.box(gx + t / 2, y - h / 2, z, t, t, t, col, light=light)
            gx += step * 0.45
            continue
        if ch == ":":                                # dos puntos: dos puntos suspensos
            mesh.box(gx + t / 2, y + h * 0.18, z, t, t, t, col, light=light)
            mesh.box(gx + t / 2, y - h * 0.18, z, t, t, t, col, light=light)
            gx += step * 0.55
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
        gx += step


# ---------------------------------------------------------------- dial
def _brighten(col, f=1.35):
    """Color al alza para los palitos encendidos (que se lean como LEDs, no como pintura)."""
    return (min(1.0, col[0] * f), min(1.0, col[1] * f), min(1.0, col[2] * f))


# ---- disposicion interior de un dial (todo en unidades de radio: r = 1) ----
# La cara tiene un arco de 270 grados con palitos entre 0,63 y 0,80 y la banda de zonas hasta
# 0,95; abajo queda una cuña libre (de -135 a -45 grados) que las agujas nunca barren. Ahi van
# la lectura digital, la unidad y el nombre/media, apilados; los numeros de la escala van por
# dentro de los palitos. Cada texto se ajusta (`_fit`) para que quepa en la cara y no pise
# palitos ni los extremos del arco: se achica solo lo necesario.
FACE = 0.93                       # radio maximo de cualquier texto (la cara llega a 1,0)
TICK_IN = 0.60                    # radio interior de los palitos menos margen
WEDGE = (math.radians(-127.0), math.radians(-53.0))   # cuña libre (los extremos estan a -135/-45)
READ_Y, READ_H = -0.385, 0.18     # lectura digital: centro y alto preferido
UNIT_Y, UNIT_H = -0.605, 0.115    # unidad (BPM, KM/H...)
ROW3_Y, ROW3_H = -0.795, 0.08     # nombre del dial (grandes) o media "MED nn" (chicos)
NUM_GAP = 0.09                    # aire minimo entre numeros de la escala
NUM_H = 0.12                      # numeros de la escala


def _box_ok(cx, cy, w, h) -> bool:
    """La caja (centro, ancho, alto; r = 1) cabe en la cara y, si llega a la zona de palitos,
    queda dentro de la cuña libre de abajo."""
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            x, y = cx + sx * w, cy + sy * h
            rad = math.hypot(x, y)
            if rad > FACE:
                return False
            if rad > TICK_IN and not (WEDGE[0] <= math.atan2(y, x) <= WEDGE[1]):
                return False
    return True


def _fit(cy, h_pref, width, cx=0.0, floor=0.45) -> float:
    """Mayor alto <= h_pref (a pasos de 7 %) con el que `width(h)` cabe en `cy`; si ni con
    `floor * h_pref` cabe, devuelve ese minimo (la prueba de diseno lo marcaria)."""
    h = h_pref
    while h > h_pref * floor and not _box_ok(cx, cy, width(h), h):
        h *= 0.93
    return h


def _scale_labels(lo, hi, major, minor):
    """[(texto, angulo, alto, radio)] de los numeros de la escala, sin solaparse entre si.

    El radio sale de pedir que la caja del numero entre en TICK_IN (no solo su centro: un
    "160" a la derecha es ancho y antes se montaba sobre los palitos); si no cabe sin
    acercarse al eje, se achica.
    """
    if not (minor and hi > lo and major):
        return []
    cands = []
    v = math.ceil(lo / minor) * minor
    while v <= hi + 1e-9:
        frac = (v - lo) / (hi - lo)
        if abs(v / major - round(v / major)) < 1e-9 and 0.05 < frac < 0.95:
            cands.append((str(int(round(v))), _dial(frac), int(round(v / major))))
        v += minor
    for k in range(1, len(cands) + 1):
        boxes = []
        # solo los multiplos de k mayores: salen numeros redondos (80, 120, 160), no 60, 100, 140
        for txt, a, _i in [cd for cd in cands if cd[2] % k == 0]:
            h = NUM_H
            while True:
                w = ink_w(txt, h)
                # radio mas grande con el que las 4 esquinas siguen dentro de TICK_IN (la esquina
                # lejana tiene ademas componente perpendicular: proyectar solo en radial no basta)
                rr = TICK_IN - 0.5 * (abs(math.cos(a)) * w + abs(math.sin(a)) * h)
                while rr >= 0.30 and any(
                        math.hypot(rr * math.cos(a) + sx * w / 2, rr * math.sin(a) + sy * h / 2) > TICK_IN
                        for sx in (-1, 1) for sy in (-1, 1)):
                    rr -= 0.004
                if rr >= 0.30 or h < NUM_H * 0.5:
                    break
                h *= 0.93
            boxes.append((txt, a, h, rr))
        ok = all(abs(p[3] * math.cos(p[1]) - q[3] * math.cos(q[1])) >= (ink_w(p[0], p[2]) + ink_w(q[0], q[2])) / 2 + NUM_GAP
                 or abs(p[3] * math.sin(p[1]) - q[3] * math.sin(q[1])) >= (p[2] + q[2]) / 2 + NUM_GAP
                 for i, p in enumerate(boxes) for q in boxes[i + 1:])
        if ok:
            return boxes
    return []


def dial_static(m, r, lo, hi, segs, major, minor, label, unit, big=True):
    """Cara, bisel, banda de zonas, palitos, numeros y etiquetas.

    Estructura de tacometro: banda de zonas en la corona, palitos por dentro, numeros
    todavia mas adentro y, en la cuña libre de abajo, lectura digital (capa de valores),
    unidad y nombre. La disposicion y los tamanos salen de `_fit` (ver arriba).
    """
    m.disc(0, 0, 0.0, r * 1.06, (0.115, 0.128, 0.150), segs=76)   # corona externa
    m.disc(0, 0, 0.014, r, (0.205, 0.225, 0.255), segs=76)              # cara
    m.ring(0, 0, 0.026, r, r * 1.06, (0.40, 0.45, 0.52), segs=76, light=0.2)
    m.ring(0, 0, 0.03, r * 0.50, r * 0.515, (0.30, 0.34, 0.40), segs=72)
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
            g += 0.004
            if f0 == f1:
                break
    for txt, a, h, rr in _scale_labels(lo, hi, major, minor):
        draw_text(m, math.cos(a) * rr * r, math.sin(a) * rr * r, 0.06, r * h, txt, INK,
                  light=0.9, align="c")
    # unidad y nombre en la cuña de abajo (ahi no llegan palitos, numeros ni agujas)
    hu = _fit(UNIT_Y, UNIT_H, lambda h: ink_w(unit, h))
    draw_text(m, 0.0, UNIT_Y * r, 0.06, r * hu, unit, INK, light=0.9, align="c")
    if big:
        hl = _fit(ROW3_Y, ROW3_H, lambda h: ink_w(label, h))
        draw_text(m, 0.0, ROW3_Y * r, 0.06, r * hl, label, DIM, light=0.8, align="c")


_TICKS: dict = {}


def _tick_fracs(lo, hi, major, minor):
    """[(fraccion, es_mayor)] de los palitos de una escala (cacheado: solo depende de la escala)."""
    key = (lo, hi, major, minor)
    got = _TICKS.get(key)
    if got is None:
        got = []
        if minor and hi > lo:
            v = math.ceil(lo / minor) * minor
            while v <= hi + 1e-9:
                got.append(((v - lo) / (hi - lo),
                            bool(major) and abs(v / major - round(v / major)) < 1e-9))
                v += minor
        _TICKS[key] = got
    return got


def dial_frame(m, r, value, lo, hi, major, minor, redline, col_now, alarm, cache=None):
    """Capa por frame: palitos (iluminados hasta el valor) + aguja con contrapeso.

    Los palitos solo cambian cuando la aguja cruza uno (el numero de encendidos), asi que su
    geometria se cachea en `cache` por (panel, escala, encendidos, color): reconstruir ~80
    cajas en Python por frame y por escena era la mitad del coste de CPU de la vista mixta.
    La aguja y el cubo si se rehacen cada frame.
    """
    frac = max(0.0, min(1.0, (value - lo) / max(1e-6, hi - lo)))
    ticks = _tick_fracs(lo, hi, major, minor)
    n_lit = sum(1 for f, _m in ticks if f <= frac)
    key = (m.t, r, lo, hi, major, minor, redline, tuple(col_now), n_lit)
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        m.v.extend(cached)
    else:
        start = len(m.v)
        for f, maj in ticks:
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
        if cache is not None:
            if len(cache) > 600:
                cache.clear()
            cache[key] = m.v[start:]
    a = _dial(frac)
    ca, sa = math.cos(a), math.sin(a)
    nc = (0.98, 0.34, 0.26) if alarm else (0.97, 0.98, 1.0)
    m.box(ca * r * 0.42, sa * r * 0.42, 0.15, r * 0.86, r * 0.055, r * 0.06,
          nc, yaw=a, light=0.45 if alarm else 0.25)
    m.box(-ca * r * 0.14, -sa * r * 0.14, 0.15, r * 0.28, r * 0.09, r * 0.065,
          nc, yaw=a, light=0.3)
    m.box(0, 0, 0.12, r * 0.19, r * 0.19, r * 0.11, (0.20, 0.23, 0.28))
    m.ring(0, 0, 0.185, r * 0.065, r * 0.105, col_now, segs=28, light=0.95)


def dial_values(m, r, digits, alarm, avg=None):
    """Capa por snapshot: lectura digital bajo el eje (cambia a 1 Hz) y, en los diales chicos,
    la media ("MED nn") en la fila de abajo.

    La lectura va en la cuña libre de abajo, no encima del eje: ahi la cruzaba la aguja y los
    numeros de la escala. Se centra por el trazo real (`digits_extent`: un `1` inicial solo pinta
    el lado derecho de su celda) y se achica si el valor es ancho (1034 W, 63,7 km/h).
    """
    txt = digits.strip()
    h = _fit(READ_Y, READ_H, lambda hh: (lambda e: e[1] - e[0])(digits_extent(txt, hh)))
    lo, hi = digits_extent(txt, h * r)
    # Misma profundidad que el resto del texto del dial (0,06): a 0,22 la perspectiva lo corria
    # hacia abajo en los diales de la parte baja de la pantalla y se pegaba a la unidad.
    draw_digits(m, -(lo + hi) / 2.0, READ_Y * r, 0.06, h * r, txt, RED if alarm else AMBER,
                light=0.95)
    if avg is not None:
        sv = "%d" % round(avg) if avg >= 10 else "%.1f" % avg
        gap = 0.75                                    # espacio entre "MED" y el valor, en alto
        def width(hh):
            vl, vh = digits_extent(sv, hh)
            return ink_w("MED", hh) + gap * hh + (vh - vl)
        hh = _fit(ROW3_Y, 0.095, width)
        vl, vh = digits_extent(sv, hh * r)
        tot = width(hh) * r
        x0 = -tot / 2.0
        draw_text(m, x0 + 0.59 * hh * r / 7.0, ROW3_Y * r, 0.06, hh * r, "MED", DIM, light=0.85)
        draw_digits(m, x0 + ink_w("MED", hh * r) + gap * hh * r - vl, ROW3_Y * r, 0.06, hh * r, sv,
                    (0.75, 0.80, 0.86), light=0.85)


# ---------------------------------------------------------------- cluster
BIG_R, SMALL_R = 1.18, 0.78
DIALS = (                      # (clave, centro x, centro z, radio, etiqueta, unidad)
    ("hr", -2.30, 0.72, BIG_R, "FRECUENCIA", "BPM"),
    ("w", 2.30, 0.72, BIG_R, "POTENCIA", "WATTS"),
    ("v", -3.60, -2.08, SMALL_R, "VELOCIDAD", "KM/H"),
    ("c", 3.60, -2.08, SMALL_R, "CADENCIA", "RPM"),
)
HALF_X, HALF_Z = 4.70, 2.95     # extensores del cluster: enmarcan a pantalla llena
ZCOLORS_FALLBACK = ["#6b7280", "#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]


# ---------------------------------------------------------------- plan de entrenamiento (fase 4)
# Colores del estado frente al objetivo del bloque: dentro, por debajo, por encima, sin lectura.
TGT_COL = {"ok": (0.30, 0.95, 0.45), "low": (0.35, 0.68, 1.0), "high": (1.0, 0.36, 0.30),
           None: (0.92, 0.94, 0.98)}
TGT_WORD = {"ok": "EN RANGO", "low": "SUBE", "high": "BAJA", None: ""}


def dial_target(m, r, lo, hi, tlo, thi, status):
    """Arco del objetivo del bloque sobre el bisel de un dial: de `tlo` a `thi` (None = hasta el
    final de la escala) en el color del estado. Va en la capa de valores (cambia con el bloque
    y con el estado, nunca por frame)."""
    span = max(1e-6, hi - lo)
    f0 = max(0.0, min(1.0, (tlo - lo) / span))
    f1 = 1.0 if thi is None else max(0.0, min(1.0, (thi - lo) / span))
    if f1 <= f0:
        f1 = min(1.0, f0 + 0.012)
    col = TGT_COL.get(status, TGT_COL[None])
    n = max(2, int(math.ceil((f1 - f0) / 0.012)))
    prev = None
    for i in range(n + 1):
        a = _dial(f0 + (f1 - f0) * i / n)
        p = (math.cos(a) * r * 1.035, math.sin(a) * r * 1.035)
        if prev:
            mid = ((prev[0] + p[0]) / 2, (prev[1] + p[1]) / 2)
            ln = math.hypot(p[0] - prev[0], p[1] - prev[1])
            m.box(mid[0], mid[1], 0.075, ln + r * 0.02, r * 0.075, 0.05, col,
                  yaw=math.atan2(p[1] - prev[1], p[0] - prev[0]), light=1.0)
        prev = p


def _target_dial(snap):
    """("hr" | "w", objetivo) del bloque actual, o (None, None)."""
    t = (snap.get("workout") or {}).get("target")
    if not t or t.get("lo") is None:
        return None, None
    return ("w" if t.get("unit") == "w" else "hr"), t


def _range_text(t) -> str:
    unit = "W" if t["unit"] == "w" else "BPM"
    if t.get("hi") is None:
        return "%d+ %s" % (t["lo"], unit)
    return "%d-%d %s" % (t["lo"], t["hi"], unit)


def plan_hud(snap, anim, aspect, top=False):
    """Tira del plan (nombre del bloque, cuenta atras, objetivo, siguiente y barra del plan) como
    capa `hud_plan`; [] si no hay plan. Se reconstruye a 1 Hz con el snapshot: el destello del
    cambio de bloque son los primeros 4 s con el fondo alternando cada segundo."""
    w = snap.get("workout")
    if not w:
        return []
    sig = (snap.get("version"), round(aspect, 3), top)
    if anim.get("plan_sig") == sig:
        return [("hud_plan", None)]
    anim["plan_sig"] = sig
    from .menuview import BAND_COLORS
    from .workouts import band as _band
    s = _hud_layout(aspect)[0]
    m = Mesh()
    m.panel(0.0, 1.0 - (0.115 if top else 0.285) * s)
    done = bool(w.get("done"))
    pct = int(w.get("pct") or 50)
    bcol = BAND_COLORS[_band(pct)] if not done else GREEN
    flash = (not done and int(w.get("i") or 0) > 0 and int(w.get("elapsed") or 0) < 4
             and int(w.get("elapsed") or 0) % 2 == 0)
    bg = tuple(0.55 * c for c in bcol) if flash else (0.07, 0.08, 0.10)
    m.box(0.0, 0.0, 0.0, 1.50 * s, 0.17 * s, 0.01, bg, light=0.2)
    m.box(-0.744 * s, 0.0, 0.0, 0.012 * s, 0.17 * s, 0.012, bcol, light=0.9)
    z = 0.02
    if done:
        draw_text(m, 0.0, 0.035 * s, z, 0.040 * s, "PLAN COMPLETO", GREEN, light=0.95, align="c", depth=0)
        draw_text(m, 0.0, -0.020 * s, z, 0.026 * s,
                  "SESION LIBRE +" + _clock(w.get("over") or 0), INK, light=0.9, align="c", depth=0)
    else:
        name = str(w.get("bname") or "")[:15]
        rep = str(w.get("rep") or "")
        draw_text(m, -0.72 * s, 0.040 * s, z, 0.034 * s, (name + " " + rep).strip(), bcol,
                  light=0.95, depth=0)
        nx = w.get("next")
        sub = ("SIGUE " + str(nx["name"])[:8].upper() + " " + _clock(nx["secs"])) if nx else "ULTIMO BLOQUE"
        draw_text(m, -0.72 * s, -0.012 * s, z, 0.021 * s, sub, DIM, light=0.9, depth=0)
        left = _clock(w.get("left") or 0)
        h = 0.075 * s
        draw_digits(m, -digits_w(left, h) / 2.0, 0.012 * s, 0.03, h, left, WHITE, light=0.95)
        t = w.get("target")
        if t and t.get("lo") is not None:
            col = TGT_COL.get(t.get("status"), TGT_COL[None])
            draw_text(m, 0.72 * s, 0.040 * s, z, 0.034 * s, _range_text(t), col, light=0.95, align="r", depth=0)
            word = TGT_WORD.get(t.get("status"), "")
            if word:
                draw_text(m, 0.72 * s, -0.012 * s, z, 0.026 * s, word, col, light=0.95, align="r", depth=0)
        else:
            draw_text(m, 0.72 * s, 0.040 * s, z, 0.030 * s, "SIN OBJETIVO", DIM, light=0.9, align="r", depth=0)
    # barra del plan: un tramo por bloque (ancho = duracion, color = banda) y el marcador del momento
    prof = w.get("profile") or []
    tot = sum(p[0] for p in prof) or 1
    x0, wid, y = -0.70 * s, 1.40 * s, -0.064 * s
    x = x0
    for secs, pc in prof:
        ww = wid * secs / tot
        m.box(x + ww / 2.0, y, 0.004, max(0.002, ww - 0.003 * s), 0.016 * s, 0.01,
              BAND_COLORS[_band(int(pc))], light=0.7)
        x += ww
    frac = min(1.0, float(w.get("progress") or 0.0))
    m.box(x0 + wid * frac, y, 0.012, 0.007 * s, 0.032 * s, 0.012, WHITE, light=1.0)
    return [("hud_plan", m)]


class Cluster:
    """Arma el cluster en 3 capas: estatica (raro), valores (1 Hz) y agujas (por frame)."""

    def build(self, snap, anim, aspect):
        """([(nombre, malla_o_None)], camara); None en una capa = conservar su VBO.

        La cache de "ya subido" vive en `anim`, que es de cada ventana/vista: con una cache
        global, al volver al tablero (flechas o `g`) la vista nueva no tenia VBOs pero la
        cache decia que si, y el panel salia sin fondo ni escalas.
        """
        cache = anim.setdefault("cluster_cache", {})
        cfg = self._scales(snap)
        layers = []
        if cfg != cache.get("cfg"):
            cache["cfg"] = cfg
            layers.append(("static", self._static(cfg)))
        else:
            layers.append(("static", None))
        vsig = (snap.get("version"), round(cfg["hr"][1]), round(cfg["w"][1]))
        if vsig != cache.get("vsig"):
            cache["vsig"] = vsig
            layers.append(("values", self._values(snap, cfg)))
        else:
            layers.append(("values", None))
        layers.append(("frame", self._frame(snap, anim, cfg)))
        return layers, _cam(anim, aspect, bool(snap.get("workout")))

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
        lab = Mesh()
        lab.panel(0.0, 0.0)                        # etiquetas de las tiras: no cambian nunca
        _labels(lab)
        m.merge(lab)
        return m

    # ---- valores (1 Hz): digitos de cada dial, barra LED, pilotos y tiras ----
    def _values(self, snap, cfg):
        m = Mesh()
        vals = {"hr": float(snap.get("hr") or 0), "w": float(snap.get("watts") or 0),
                "v": float(snap.get("speed") or 0), "c": float(snap.get("cadence") or 0)}
        alarm = bool(snap.get("alarm"))
        avgs = {"v": float(snap.get("avg_speed") or 0), "c": float(snap.get("avg_cadence") or 0)}
        tdial, tgt = _target_dial(snap)
        for which, cx, cz, r, _label, _unit in DIALS:
            one = Mesh()
            one.panel(cx, cz)
            dial_values(one, r, _digits_for(which, vals[which]),
                        alarm and which == "hr", avg=avgs.get(which))
            if which == tdial:
                dial_target(one, r, cfg[which][0], cfg[which][1], tgt["lo"], tgt["hi"], tgt.get("status"))
            m.merge(one)
        zc = _hex(snap.get("zone_color") or "#ef4444")
        sub = Mesh()
        sub.panel(0.0, 0.0)                        # espacio panel: y alto, z hacia el piloto
        _led_bar(sub, float(snap.get("hr") or 0), cfg["hr"][0], cfg["hr"][1], zc)
        _lamps(sub, snap)
        _strips(sub, snap)
        m.merge(sub)
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
                       cols[which], alarm and which == "hr",
                       cache=anim.setdefault("tick_cache", {}))
            m.merge(one)
        if alarm:                                    # halo rojo pulsante sobre el dial de FC
            p = 0.45 + 0.55 * math.sin(anim["t"] * 7.0)
            _w, cx, cz, r = DIALS[0][0], DIALS[0][1], DIALS[0][2], DIALS[0][3]
            one = Mesh()
            one.panel(cx, cz)
            one.ring(0, 0, 0.03, r * 1.14, r * 1.21, (0.98, 0.30, 0.24), segs=64,
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
        return "%3.1f" % v if v else "-.-"
    return "%3d" % round(v) if v else "--"


def _sweep(anim):
    """Self-test del tablero: 0 -> max -> 0 en 1.1 s al abrir (None despues)."""
    t = anim.get("t", 0.0)
    if t >= 1.1:
        return None
    return math.sin(t / 1.1 * math.pi)


def _cam(anim, aspect, plan=False):
    """Framing por aspect (el cluster llena la ventana) con balanceo sutil ±6 grados.

    Casi de frente: con mucha pitch el panel se ve trapecio y los diales se aplastan.
    """
    fovy = 0.62
    tan = math.tan(fovy / 2.0)
    d = max(HALF_Z / tan, HALF_X / (max(0.35, aspect) * tan)) * (1.24 if plan else 1.10)
    a = 0.062 * math.sin(anim["t"] * 0.70)                      # ±3.5 grados de balanceo
    pitch = 0.012 * math.sin(anim["t"] * 0.45)
    eye = (math.sin(a) * d, -math.cos(a) * d, (0.06 + pitch) * d)
    return eye, (0.0, 0.0, -0.14 + (0.42 if plan else 0.0)), (0.0, 0.0, 1.0), fovy


def _labels(m):
    """Etiquetas fijas de las tiras: van en la capa estatica (no cambian nunca)."""
    h = 0.18
    for i, lab in enumerate(("TIEMPO", "KCAL", "DIST KM", "ATLETA")):
        x = 1.72 if lab == "ATLETA" else -3.62 + i * 1.78
        draw_text(m, x, 2.46, 0.05, h * 0.70, lab, DIM, light=0.9)
    for i, lab in enumerate(("FC MED", "FC MAX", "%FCMAX", "EST")):
        draw_text(m, -2.58 + i * 1.36, -0.80, 0.05, h * 0.76, lab, DIM, light=0.9)
    for i, lab in enumerate(("W MED", "W MAX", "FTP", "EN ZONA")):
        draw_text(m, -2.58 + i * 1.36, -1.42, 0.05, h * 0.76, lab, DIM, light=0.9)
    for z, lab in enumerate(("ZONA FC", "BANDA W")):
        draw_text(m, -1.35, -2.30 - z * 0.32, 0.05, 0.15, lab, DIM, light=0.9, align="r")
    draw_text(m, 1.42, -2.24, 0.05, 0.17 * 0.78, "W/KG", DIM, light=0.9)
    draw_text(m, -text_w("FC", 0.17) / 2, 1.62 + 0.26, 0.05, 0.17, "FC", INK, light=0.9)


def _led_bar(m, hr, lo, hi, zc):
    """Columna LED de FC entre los dos diales grandes: encendida hasta el valor.

    Se dibuja en espacio panel: y = alto, z = hacia el piloto.
    """
    x, y0, y1, n = 0.0, -0.44, 1.62, 20
    frac = max(0.0, min(1.0, (hr - lo) / max(1e-6, hi - lo))) if hr else 0.0
    m.box(x, (y0 + y1) / 2, -0.08, 0.46, y1 - y0 + 0.30, 0.10, (0.17, 0.19, 0.24))
    for i in range(n):
        f = i / (n - 1)
        lit = f <= frac
        col = (0.98, 0.28, 0.28) if (lit and f > 0.88) else (zc if lit else UNLIT)
        m.box(x, y0 + (y1 - y0) * f, 0.02, 0.32, (y1 - y0) / n * 0.64, 0.10,
              col, light=0.95 if lit else 0.3)


def _lamps(m, snap):
    """Pilotos de zona FC y de banda de potencia + valor W/kg (etiquetas van fijas)."""
    zs = snap.get("zcolors") or ZCOLORS_FALLBACK
    ws = snap.get("wcolors") or ZCOLORS_FALLBACK
    zi, wi = int(snap.get("zone_idx") or 0), int(snap.get("wband") or 0)
    for z, (cols, idx) in enumerate(((zs, zi), (ws, wi))):
        yy = -2.30 - z * 0.32
        for i, chex in enumerate(cols):
            lit = i == idx
            m.box(-1.05 + i * 0.44, yy, 0.02, 0.32, 0.17, 0.12,
                  _hex(chex) if lit else UNLIT, light=0.95 if lit else 0.35)
    h = 0.17
    draw_digits(m, 1.42, -2.52, 0.05, h * 0.92, "%3.1f" % float(snap.get("wkg") or 0),
                WHITE, light=0.9)


def _strips(m, snap):
    """Valores de las tiras digitales (las etiquetas viven en la capa estatica).

    En espacio panel (y = alto, z = hacia el piloto) y centrados en el hueco entre los
    diales chicos, para que nada se meta debajo de vel o cadencia.
    """
    h = 0.18
    for i, (val, col) in enumerate((
            (_clock(snap.get("elapsed_s") or 0), WHITE),
            ("%4.0f" % float(snap.get("total_kcal") or 0), GREEN),
            ("%04.1f" % float(snap.get("dist_km") or 0), CYAN))):
        x = -3.62 + i * 1.78
        draw_digits(m, x, 2.18, 0.05, h * 0.92, val, col, light=0.95)
    atleta = "%dA %dKG %s" % (int(snap.get("age") or 0), round(float(snap.get("weight") or 0)),
                              (snap.get("sex") or "M"))
    draw_text(m, 1.72, 2.16, 0.05, h * 0.72, atleta, WHITE, light=0.9)

    for i, (val, col) in enumerate((
            ("%3d" % round(float(snap.get("avg_hr") or 0)), AMBER),
            ("%3d" % int(snap.get("max_hr") or 0), RED),
            ("%2.0f" % float(snap.get("pct_max") or 0), WHITE),
            ("%3d" % int(snap.get("hrmax") or 0), INK))):
        draw_digits(m, -2.58 + i * 1.36, -1.06, 0.05, h * 0.92, val, col, light=0.9)
    for i, (val, col) in enumerate((
            ("%3d" % round(float(snap.get("avg_watts") or 0)), AMBER),
            ("%3d" % int(snap.get("max_watts") or 0), RED),
            ("%3d" % int(snap.get("ftp") or 0), GREEN),
            (_clock(snap.get("zone_secs") or 0), WHITE))):
        draw_digits(m, -2.58 + i * 1.36, -1.68, 0.05, h * 0.92, val, col, light=0.9)

    estado = "Z%d %s  %d MUESTRAS  %2.1f KCAL/MIN" % (
        int(snap.get("zone_idx") or 0),
        (snap.get("zone_name") or "")[:8].upper(),
        int(snap.get("n") or 0), float(snap.get("kcal_min") or 0))
    draw_text(m, 0.0, -2.96, 0.05, 0.105, estado, INK, light=0.9, align="c")


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

# ---------------------------------------------------------------- HUD de la pista
# Los mismos diales, en las esquinas de la vista del ciclista como en un juego de carreras:
# velocimetro grande abajo a la derecha con el "cuentarrevoluciones" (cadencia) al lado, FC
# grande abajo a la izquierda con la potencia, y una tira arriba con tiempo, distancia,
# vuelta del velodromo y kcal. Coordenadas de pantalla: x en [-aspecto, aspecto], y en
# [-1, 1]; glview las dibuja con una camara ortografica despues de la escena 3D.
HUD_BIG, HUD_SMALL = 0.27, 0.17
HUD_REF_ASPECT = 1.5                # por debajo de este aspecto el HUD se encoge (vistas partidas)
HUD_LAP_M = 250.0                   # la vuelta del velodromo 3D


def _hud_layout(aspect):
    """(escala, {dial: (cx, cy, r, etiqueta, unidad)}) para este aspecto de ventana."""
    a = max(0.6, aspect)
    s = min(1.0, a / HUD_REF_ASPECT)
    big, small = HUD_BIG * s, HUD_SMALL * s
    return s, {
        "hr": (-a + 0.34 * s, -1.0 + 0.34 * s, big, "FRECUENCIA", "BPM"),
        "w": (-a + 0.86 * s, -1.0 + 0.20 * s, small, "POTENCIA", "WATTS"),
        "v": (a - 0.34 * s, -1.0 + 0.34 * s, big, "VELOCIDAD", "KM/H"),
        "c": (a - 0.86 * s, -1.0 + 0.20 * s, small, "CADENCIA", "RPM"),
    }


HUD_STRIP = ("TIEMPO", "DIST KM", "VUELTA", "KCAL")


def _hud_strip_x(s):
    """Centros x de las cuatro casillas de la tira superior."""
    return [(-1.5 + i) * 0.36 * s for i in range(len(HUD_STRIP))]


def hud_layers(snap, anim, aspect):
    """Capas del HUD (`hud_static`, `hud_values`, `hud_frame`) con la misma cache que el
    cluster: lo estatico solo cambia con las escalas o el aspecto, los digitos a 1 Hz y
    las agujas por frame (con la inercia asimetrica de la TUI y el self-test al abrir)."""
    cfg = Cluster._scales(snap)
    s, lay = _hud_layout(aspect)
    key = (repr(cfg), round(aspect, 3))
    out = []
    if anim.get("hud_key") != key:
        anim["hud_key"] = key
        anim["hud_vsig"] = None
        m = Mesh()
        for which, (cx, cy, r, label, unit) in lay.items():
            lo, hi, segs, major, minor = cfg[which]
            one = Mesh()
            one.panel(cx, cy)
            dial_static(one, r, lo, hi, segs, major, minor, label, unit, big=which in ("hr", "v"))
            m.merge(one)
        bar = Mesh()
        bar.panel(0.0, 1.0 - 0.10 * s)
        bar.box(0.0, 0.0, 0.0, 1.50 * s, 0.17 * s, 0.01, (0.07, 0.08, 0.10), light=0.2)
        bar.box(0.0, -0.085 * s, 0.006, 1.50 * s, 0.006 * s, 0.01, (0.30, 0.34, 0.40), light=0.4)
        for x, lab in zip(_hud_strip_x(s), HUD_STRIP):
            draw_text(bar, x, 0.035 * s, 0.01, 0.032 * s, lab, DIM, light=0.9, align="c")
        m.merge(bar)
        out.append(("hud_static", m))
    else:
        out.append(("hud_static", None))

    vsig = (snap.get("version"), key)
    if anim.get("hud_vsig") != vsig:
        anim["hud_vsig"] = vsig
        m = Mesh()
        vals = {"hr": float(snap.get("hr") or 0), "w": float(snap.get("watts") or 0),
                "v": float(snap.get("speed") or 0), "c": float(snap.get("cadence") or 0)}
        alarm = bool(snap.get("alarm"))
        tdial, tgt = _target_dial(snap)
        for which, (cx, cy, r, _l, _u) in lay.items():
            one = Mesh()
            one.panel(cx, cy)
            dial_values(one, r, _digits_for(which, vals[which]), alarm and which == "hr")
            if which == tdial:
                dial_target(one, r, cfg[which][0], cfg[which][1], tgt["lo"], tgt["hi"], tgt.get("status"))
            m.merge(one)
        dist_m = float(snap.get("dist_km") or 0) * 1000.0
        cells = (_clock(snap.get("elapsed_s") or 0), "%.2f" % (dist_m / 1000.0),
                 "%d" % (int(dist_m // HUD_LAP_M) + 1), "%d" % round(float(snap.get("total_kcal") or 0)))
        cols = (WHITE, CYAN, AMBER, GREEN)
        bar = Mesh()
        bar.panel(0.0, 1.0 - 0.10 * s)
        h = 0.060 * s
        for x, val, col in zip(_hud_strip_x(s), cells, cols):
            draw_digits(bar, x - digits_w(val, h) / 2.0, -0.055 * s, 0.012, h, val, col, light=0.95)
        m.merge(bar)
        out.append(("hud_values", m))
    else:
        out.append(("hud_values", None))

    m = Mesh()
    cols = {"hr": _hex(snap.get("zone_color") or "#ef4444"),
            "w": _hex(snap.get("wzone_color") or "#facc15"),
            "v": CYAN, "c": (0.66, 0.55, 0.98)}
    vals = {"hr": float(snap.get("hr") or 0), "w": float(snap.get("watts") or 0),
            "v": float(snap.get("speed") or 0), "c": float(snap.get("cadence") or 0)}
    needles = anim.setdefault("hud_needles", {})
    sweep = _sweep(anim)
    alarm = bool(snap.get("alarm"))
    for which, (cx, cy, r, _l, _u) in lay.items():
        lo, hi, _segs, major, minor = cfg[which]
        if sweep is None:
            prev = needles.get(which, 0.0)
            k = 0.5 if vals[which] > prev else 0.14
            needles[which] = prev + (vals[which] - prev) * k
        else:
            needles[which] = sweep * hi
        one = Mesh()
        one.panel(cx, cy)
        dial_frame(one, r, needles[which], lo, hi, major, minor,
                   0.90 if which == "hr" else (0.86 if which == "w" else None),
                   cols[which], alarm and which == "hr",
                   cache=anim.setdefault("tick_cache", {}))
        m.merge(one)
    out.append(("hud_frame", m))
    return out + plan_hud(snap, anim, aspect)
