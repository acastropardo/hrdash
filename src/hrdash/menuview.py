"""Escena `menu` de la ventana 3D: lo que se ve antes de pulsar INICIAR.

El padre (motor) es dueno de los datos y manda un bloque `menu` en el snapshot (ranuras de FC y
rodillo con su lectura en vivo, dispositivos del ultimo escaneo, estado del escaneo, si se puede
iniciar). Esta escena solo lo dibuja y resuelve la interaccion; el hijo contesta con eventos
`{"ui": {"act": <id del widget>}}` y no sabe nada de BLE.

`menu_widgets(menu, aspecto)` es una funcion PURA y es la unica fuente de la disposicion: la
usan el dibujo, el hit-test del raton y la navegacion con teclado, asi las zonas de clic no
pueden separarse de lo que se ve. Se compone en un lienzo fijo (x en [-1,78, 1,78], y en [-1, 1])
que se escala uniformemente (`_scale`) y se centra, de modo que en ventanas estrechas todo se
encoge junto en vez de montarse.

Interaccion por FOCO, como un menu de consola: flechas/Tab mueven el foco, Enter/Espacio
activan, `B` busca, el raton mueve el foco al pasar y activa con el clic. El foco inicial es
INICIAR, asi que Enter a secas sigue iniciando.

Capas (todas `hud_*`, camara ortografica; no hay escena 3D debajo), de menos a mas cambiante:
`hud_static` (aspecto), `hud_list` (dispositivos, estados y etiquetas: solo cuando cambian),
`hud_values` (lecturas en vivo: cada snapshot) y `hud_frame` (fondos, foco y animacion: cada
frame). El texto va en quads planos (`depth=0`): es ~8 veces mas barato que en cajas.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field

from .cluster import (AMBER, CYAN, DIM, FONT, GREEN, INK, RED, WHITE, digits_extent, draw_digits,
                      draw_text, ink_w)
from .glview import Mesh

CANVAS_X = 1.78
MAX_ROWS = 8                            # fila del simulador + hasta 7 dispositivos

BG = (0.045, 0.052, 0.068)
PANEL = (0.085, 0.098, 0.125)
ROW = (0.105, 0.120, 0.150)
ROW_USE = (0.080, 0.170, 0.115)
ROW_FOCUS = (0.150, 0.260, 0.195)
RING = (0.45, 0.90, 0.58)
BTN = (0.14, 0.50, 0.26)
BTN_FOCUS = (0.20, 0.68, 0.36)
BTN_OFF = (0.16, 0.18, 0.22)
BTN2 = (0.16, 0.30, 0.52)               # botones secundarios (BUSCAR, QUITAR)
BTN2_FOCUS = (0.24, 0.44, 0.74)

# Rectangulos del lienzo: (x0, y0, x1, y1)
LIST_PANEL = (-1.70, -0.58, -0.06, 0.66)
CARDS_PANEL = (0.06, -0.58, 1.70, 0.66)
SCAN_RECT = (-0.74, 0.51, -0.12, 0.65)
QUIT_RECT = (1.20, -0.300, 1.62, -0.200)
PLAY_RECT = (-0.46, -0.90, 0.46, -0.72)
FC_CARD = (0.10, 0.02, 1.66, 0.53)
TR_CARD = (0.10, -0.54, 1.66, -0.03)
ROW_X0, ROW_X1, ROW_TOP, ROW_H, ROW_GAP = -1.66, -0.10, 0.455, 0.100, 0.010


@dataclass(frozen=True)
class Widget:
    id: str
    kind: str                           # "button" | "row"
    rect: tuple                         # (x0, y0, x1, y1) en coordenadas de pantalla (ya escalado)
    label: str = ""
    enabled: bool = True
    data: dict = field(default_factory=dict, compare=False, hash=False)


def _scale(aspect: float) -> float:
    """Factor lienzo -> pantalla: 1 en ventanas anchas, menor en estrechas (todo se encoge junto)."""
    return max(0.2, min(1.0, aspect / CANVAS_X))


def _r(rect, s):
    return tuple(v * s for v in rect)


def _row_rect(i: int):
    y1 = ROW_TOP - i * (ROW_H + ROW_GAP)
    return (ROW_X0, y1 - ROW_H, ROW_X1, y1)


def to_layout(nxy, aspect: float):
    """(x, y) de la ventana en 0..1 (origen abajo-izquierda) -> coordenadas de pantalla del HUD."""
    return ((nxy[0] * 2.0 - 1.0) * aspect, nxy[1] * 2.0 - 1.0)


def menu_widgets(menu, aspect: float) -> list:
    """Widgets del menu en orden de foco (funcion pura). Los deshabilitados siguen en la lista
    (se dibujan apagados) pero ni el foco ni el clic los toman."""
    menu = menu or {}
    s = _scale(aspect)
    scanning = (menu.get("scan") or {}).get("state") == "scanning"
    ws = [Widget("scan", "button", _r(SCAN_RECT, s), "BUSCANDO..." if scanning else "BUSCAR",
                 enabled=not scanning)]
    ws.append(Widget("sim", "row", _r(_row_rect(0), s), "SIMULADOR", True,
                     {"sim": True, "use": bool(menu.get("sim"))}))
    for i, d in enumerate((menu.get("devices") or [])[:MAX_ROWS - 1]):
        ws.append(Widget("dev:" + str(d.get("id")), "row", _r(_row_rect(i + 1), s),
                         str(d.get("name") or ""), True, d))
    if menu.get("trainer"):
        ws.append(Widget("notrainer", "button", _r(QUIT_RECT, s), "QUITAR"))
    ws.append(Widget("play", "button", _r(PLAY_RECT, s), "INICIAR",
                     enabled=bool(menu.get("can_play", True))))
    return ws


def hit_test(widgets, x: float, y: float):
    """Id del widget habilitado bajo (x, y) o None (el ultimo de la lista queda encima)."""
    for w in reversed(widgets):
        if w.enabled and w.rect[0] <= x <= w.rect[2] and w.rect[1] <= y <= w.rect[3]:
            return w.id
    return None


def hit_id(menu, nxy, aspect: float):
    """Atajo para el hijo: widget bajo el cursor dado en fraccion de ventana."""
    x, y = to_layout(nxy, aspect)
    return hit_test(menu_widgets(menu, aspect), x, y)


def focus_or_default(widgets, focus_id):
    """El foco si sigue siendo un widget habilitado; si no (desaparecio al cambiar la lista),
    INICIAR si esta habilitado y si no el primero que lo este."""
    on = [w.id for w in widgets if w.enabled]
    if focus_id in on:
        return focus_id
    return "play" if "play" in on else (on[0] if on else None)


def move_focus(widgets, focus_id, step: int):
    """Foco tras mover `step` posiciones por los widgets habilitados (con vuelta)."""
    on = [w.id for w in widgets if w.enabled]
    if not on:
        return None
    cur = focus_or_default(widgets, focus_id)
    return on[(on.index(cur) + step) % len(on)]


def activate(widgets, focus_id):
    """Id a mandar al motor al activar el foco, o None si no hay nada habilitado en el foco."""
    f = focus_or_default(widgets, focus_id)
    return f if any(w.id == f and w.enabled for w in widgets) else None


# ---------------------------------------------------------------- texto
def font_text(txt) -> str:
    """Texto a lo que sabe pintar la fuente: sin acentos, en mayusculas y '?' para lo demas."""
    t = unicodedata.normalize("NFKD", str(txt or "")).encode("ascii", "ignore").decode("ascii")
    return "".join(c if c in FONT else "?" for c in t.upper())


def _clip(txt: str, h: float, maxw: float) -> str:
    """Recorta hasta que el trazo cabe en `maxw` (por ancho real, no por caracteres)."""
    while txt and ink_w(txt, h) > maxw:
        txt = txt[:-1]
    return txt


def _fit_h(txt: str, h: float, maxw: float) -> float:
    """Mayor alto <= h con el que `txt` cabe en `maxw` (en unidades del lienzo)."""
    t = font_text(txt)
    while h > 0.01 and ink_w(t, h) > maxw:
        h *= 0.93
    return h


def _t(m, s, x, y, h, txt, col, align="l", light=0.9, z=0.0, maxw=None):
    """Texto en coordenadas del lienzo (x, y = centro vertical), escalado a pantalla."""
    txt = font_text(txt)
    if maxw is not None:
        txt = _clip(txt, h * s, maxw * s)
    if txt:
        draw_text(m, x * s, y * s, z, h * s, txt, col, light=light, align=align, depth=0.0)


def _rect(m, s, rect, z, col, light=0.0):
    """Rectangulo plano del lienzo."""
    x0, y0, x1, y1 = (v * s for v in rect)
    m.quad((x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z), col, (0.0, 0.0, 1.0), light)


def _panel_mesh() -> Mesh:
    m = Mesh()
    m.panel(0.0, 0.0)
    return m


# ---------------------------------------------------------------- capas
def _static(s: float) -> Mesh:
    m = _panel_mesh()
    _rect(m, s, (-2.4, -1.2, 2.4, 1.2), -0.5, BG)
    _rect(m, s, LIST_PANEL, -0.3, PANEL)
    _rect(m, s, CARDS_PANEL, -0.3, PANEL)
    _t(m, s, 0.0, 0.89, 0.115, "HRDASH", WHITE, "c")
    _t(m, s, 0.0, 0.775, 0.036, "ENTRENAMIENTO CON FC Y RODILLO", DIM, "c")
    _t(m, s, -1.64, 0.585, 0.046, "DISPOSITIVOS", INK)
    _t(m, s, 0.14, 0.585, 0.046, "SENSORES EN USO", INK)
    _t(m, s, 0.0, -0.965, 0.030,
       "FLECHAS O TAB: MOVER   ENTER: ELEGIR   B: BUSCAR   Q O ESC: SALIR", DIM, "c")
    return m


def _rssi_level(rssi: int) -> int:
    return 4 if rssi >= -55 else 3 if rssi >= -65 else 2 if rssi >= -75 else 1 if rssi >= -85 else 0


def _scan_line(scan) -> tuple:
    scan = scan or {}
    st = scan.get("state")
    if st == "scanning":
        return "BUSCANDO...", AMBER
    if st == "error":
        return scan.get("msg") or "ERROR AL BUSCAR", RED
    if st == "done":
        n = int(scan.get("n") or 0)
        return (("%d DISPOSITIVO%s" % (n, "" if n == 1 else "S")), GREEN) if n else \
            ("NADA ENCONTRADO: ENCIENDE Y PULSA B", AMBER)
    return "PULSA BUSCAR", DIM


def _src_state(src, empty: str) -> tuple:
    """(nombre, texto de estado, color) de una ranura."""
    if not src:
        return empty, "", DIM
    name = (src.get("name") or "").strip() or "(SIN NOMBRE)"
    if src.get("connected"):
        return name, "CONECTADO", GREEN
    err = (src.get("error") or "").strip()
    if err:
        return name, "ERROR: " + err, RED
    return name, "CONECTANDO...", AMBER


def _list(menu, widgets, s: float) -> Mesh:
    """Todo el texto y adornos que solo cambian cuando cambia la lista o el estado."""
    m = _panel_mesh()
    menu = menu or {}
    line, col = _scan_line(menu.get("scan"))
    _t(m, s, -1.64, -0.495, 0.036, line, col, maxw=1.54)
    for w in widgets:
        if w.kind == "row":
            x0, y0, x1, y1 = (v / s for v in w.rect)
            yc = (y0 + y1) / 2.0
            d = w.data
            if d.get("sim"):
                tag, tcol, name = "DEMO", AMBER, "SIMULADOR"
                info = "FC Y RODILLO SIMULADOS"
            else:
                tag, tcol = ("FC", GREEN) if d.get("kind") == "hr" else ("ROD", CYAN)
                name = w.label or "(SIN NOMBRE)"
                info = " ".join(d.get("caps") or []) or "SIN SERVICIOS ANUNCIADOS"
            _t(m, s, x0 + 0.04, yc + 0.024, 0.034, tag, tcol, z=0.0)
            _t(m, s, x0 + 0.24, yc + 0.025, 0.046, name, INK, maxw=1.12)
            _t(m, s, x0 + 0.24, yc - 0.027, 0.030, info, DIM, maxw=0.80)
            if d.get("use"):
                _t(m, s, x1 - 0.14, yc - 0.027, 0.030, "EN USO", GREEN, "r", z=0.01)
            if not d.get("sim"):                      # barras de senal: 4, encendidas segun RSSI
                lvl = _rssi_level(int(d.get("rssi", -100)))
                bx = x1 - 0.115
                for i in range(4):
                    on = i < lvl
                    h = 0.020 + 0.014 * i
                    _rect(m, s, (bx + i * 0.020, yc + 0.020 - h, bx + i * 0.020 + 0.014, yc + 0.020),
                          0.01, GREEN if on else (0.22, 0.25, 0.30), 0.5 if on else 0.0)
        else:
            lab = w.label
            x0, y0, x1, y1 = (v / s for v in w.rect)
            yc, hh = (y0 + y1) / 2.0, (y1 - y0)
            col = DIM if not w.enabled else WHITE
            if w.id == "play":
                lh = _fit_h(lab, hh * 0.40, (x1 - x0) * 0.62)
                tw = ink_w(font_text(lab), lh)
                ts = hh * 0.32
                tx = (x0 + x1) / 2.0 - (ts * 1.6 + hh * 0.35 + tw) / 2.0
                tri = (WHITE if w.enabled else DIM)
                m.tri((tx * s, (yc - ts) * s, 0.06), ((tx + ts * 1.6) * s, yc * s, 0.06),
                      (tx * s, (yc + ts) * s, 0.06), tri, (0.0, 0.0, 1.0), 0.9)
                _t(m, s, tx + ts * 1.6 + hh * 0.35, yc, lh, lab, col, z=0.06, light=0.95)
            else:
                _t(m, s, (x0 + x1) / 2.0, yc, _fit_h(lab, hh * 0.46, (x1 - x0) * 0.86), lab, col, "c",
                   z=0.06)
    # tarjetas de las ranuras
    hr, tr = menu.get("hr"), menu.get("trainer")
    for card, label, src, empty in ((FC_CARD, "FRECUENCIA CARDIACA", hr, "SIN BANDA"),
                                    (TR_CARD, "RODILLO", tr, "SIN RODILLO")):
        x0, y0, x1, y1 = card
        _t(m, s, x0 + 0.06, y1 - 0.050, 0.030, label, DIM)
        name, state, scol = _src_state(src, empty)
        _t(m, s, x0 + 0.06, y1 - 0.130, 0.052, name, INK, maxw=1.40)
        _t(m, s, x0 + 0.06, y1 - 0.205, 0.036, state or ("ELIGE UNO EN LA LISTA" if not src else ""),
           scol if state else DIM, maxw=0.98 if card is TR_CARD else 1.4)
    _t(m, s, 0.0, -0.645, 0.038, menu.get("hint") or "", AMBER, "c", maxw=1.9)
    return m


def _values(menu, s: float) -> Mesh:
    """Lecturas en vivo de las dos tarjetas (cambian a cada snapshot)."""
    m = _panel_mesh()
    menu = menu or {}

    def num(x, y, h, val, fmt, col, unit):
        txt = fmt % val if val is not None else "--"
        lo, hi = digits_extent(txt, h * s)
        draw_digits(m, x * s - lo, y * s, 0.02, h * s, txt, col if val is not None else DIM, light=0.95)
        _t(m, s, x + (hi - lo) / s + 0.03, y - h * 0.30, 0.036, unit, DIM)

    hr = menu.get("hr")
    if hr:
        num(0.16, 0.215 - 0.10, 0.17, hr.get("hr"), "%d", AMBER, "BPM")
    tr = menu.get("trainer")
    if tr:
        num(0.16, -0.42, 0.12, tr.get("watts"), "%d", AMBER, "W")
        num(0.62, -0.42, 0.12, tr.get("cadence"), "%d", AMBER, "RPM")
        num(1.08, -0.42, 0.12, tr.get("speed"), "%.1f", AMBER, "KM/H")
    return m


def _frame(widgets, focus, t: float, scanning: bool, s: float) -> Mesh:
    """Fondos de botones y filas, foco y animacion: lo unico que se rehace en cada frame."""
    m = _panel_mesh()
    pulse = 0.5 + 0.5 * math.sin(t * 2.6)
    for w in widgets:
        x0, y0, x1, y1 = w.rect
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        foc = w.id == focus
        if w.kind == "row":
            col = ROW_FOCUS if foc else (ROW_USE if w.data.get("use") else ROW)
            if foc:
                m.box(cx, cy, -0.12, x1 - x0 + 0.012 * s, y1 - y0 + 0.012 * s, 0.01, RING, light=0.5)
            m.box(cx, cy, -0.1, x1 - x0, y1 - y0, 0.01, col, light=0.15)
            continue
        if not w.enabled:
            m.box(cx, cy, 0.04, x1 - x0, y1 - y0, 0.01, BTN_OFF, light=0.1)
            continue
        base, hot = (BTN, BTN_FOCUS) if w.id == "play" else (BTN2, BTN2_FOCUS)
        edge = 0.012 * s + (0.010 * s if foc else 0.0)
        rim = RING if w.id == "play" else (0.34, 0.50, 0.80)
        glow = tuple(c * (0.55 + 0.25 * pulse) for c in rim)
        m.box(cx, cy, 0.02, x1 - x0 + 2 * edge, y1 - y0 + 2 * edge, 0.01, RING if foc else glow,
              light=0.5 if foc else 0.3)
        m.box(cx, cy, 0.04, x1 - x0, y1 - y0, 0.01,
              hot if foc else tuple(c * (0.92 + 0.08 * pulse) for c in base), light=0.35)
    if scanning:                                       # barra indeterminada bajo la cabecera
        bx0, bx1 = ROW_X0 * s, ROW_X1 * s
        seg = (bx1 - bx0) * 0.22
        x = bx0 + ((t * 0.9) % 1.0) * ((bx1 - bx0) - seg)
        m.box((bx0 + bx1) / 2.0, 0.488 * s, -0.05, bx1 - bx0, 0.008 * s, 0.01, (0.18, 0.20, 0.25))
        m.box(x + seg / 2.0, 0.488 * s, -0.04, seg, 0.010 * s, 0.01, AMBER, light=0.8)
    return m


def _list_sig(menu, widgets) -> str:
    """Firma de lo que dibuja `_list`: todo el menu MENOS las lecturas en vivo."""
    menu = menu or {}
    trimmed = {k: v for k, v in menu.items() if k not in ("hr", "trainer")}
    slots = []
    for key in ("hr", "trainer"):
        v = menu.get(key)
        slots.append(None if not v else (v.get("name"), v.get("connected"), v.get("error"), v.get("sim")))
    return repr((sorted(trimmed.items(), key=lambda kv: kv[0]), slots,
                 [(w.id, w.enabled, w.label) for w in widgets]))


def scene_menu(snap, anim, aspect=1.6):
    """Capas `hud_*` del menu y la camara (la escena 3D queda vacia: todo es HUD)."""
    menu = snap.get("menu") or {}
    s = _scale(aspect)
    widgets = menu_widgets(menu, aspect)
    focus = focus_or_default(widgets, anim.get("menu_focus"))
    sigs = anim.setdefault("menu_sigs", {})

    def cached(name, sig, build):
        if sigs.get(name) == sig:
            return (name, None)                          # sin cambios: conserva su VBO
        sigs[name] = sig
        return (name, build())

    scanning = (menu.get("scan") or {}).get("state") == "scanning"
    layers = [cached("hud_static", round(aspect, 3), lambda: _static(s)),
              cached("hud_list", (round(aspect, 3), _list_sig(menu, widgets)),
                     lambda: _list(menu, widgets, s)),
              cached("hud_values", (round(aspect, 3), snap.get("version")), lambda: _values(menu, s)),
              ("hud_frame", _frame(widgets, focus, anim.get("t", 0.0), scanning, s))]
    return layers, ((0.0, -5.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.6, 50.0, True)
