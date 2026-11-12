"""Escena `menu` de la ventana 3D: lo que se ve antes de pulsar INICIAR.

El padre (motor) es dueno de los datos y manda un bloque `menu` en el snapshot: la pagina visible,
las ranuras de FC y rodillo con su lectura en vivo, los dispositivos del ultimo escaneo, la sesion
elegida (minutos, preset o personalizada), si se puede iniciar. Esta escena solo lo dibuja y
resuelve la interaccion; el hijo contesta con eventos `{"ui": {"act": <accion>}}` y no sabe nada
de BLE ni de como se calcula un plan.

Dos paginas, con pestanas (teclas `1` y `2`):
  SENSORES  buscar y asignar la banda de FC y el rodillo, con su lectura en vivo.
  SESION    tipo de sesion (libre, presets, personalizada) y su detalle: minutos, descripcion o
            editor de intervalos, y el perfil de bloques.

`menu_widgets(menu, aspecto)` es una funcion PURA y es la unica fuente de la disposicion: la
usan el dibujo, el hit-test del raton y la navegacion con teclado, asi las zonas de clic no
pueden separarse de lo que se ve. Se compone en un lienzo fijo (x en [-1,78, 1,78], y en [-1, 1])
que se escala uniformemente (`_scale`) y se centra, de modo que en ventanas estrechas todo se
encoge junto en vez de montarse.

Interaccion por FOCO, como un menu de consola: flechas/Tab mueven el foco, Enter/Espacio
activan, `B` busca, el raton mueve el foco al pasar y activa con el clic. Un STEPPER (fila con
`<` y `>`) se ajusta con Izquierda/Derecha, o con el clic a cada lado del valor; Enter suma. El
foco inicial es INICIAR, asi que Enter a secas sigue iniciando.

Capas (todas `hud_*`, camara ortografica; no hay escena 3D debajo), de menos a mas cambiante:
`hud_static` (aspecto y pagina), `hud_list` (todo lo que depende de la lista, el estado y la
sesion: solo cuando cambian), `hud_values` (lecturas en vivo: cada snapshot) y `hud_frame`
(fondos, foco y animacion: cada frame). El texto va en quads planos (`depth=0`): es ~8 veces mas
barato que en cajas.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field

from . import workouts
from .cluster import (AMBER, CYAN, DIM, FONT, GREEN, INK, RED, WHITE, digits_extent, draw_digits,
                      draw_text, ink_w)
from .glview import Mesh

CANVAS_X = 1.78
MAX_ROWS = 8                            # pagina sensores: fila del simulador + hasta 7 dispositivos
PAGES = ("sensores", "sesion")

BG = (0.045, 0.052, 0.068)
PANEL = (0.085, 0.098, 0.125)
ROW = (0.105, 0.120, 0.150)
ROW_USE = (0.080, 0.170, 0.115)
ROW_FOCUS = (0.150, 0.260, 0.195)
RING = (0.45, 0.90, 0.58)
BTN = (0.14, 0.50, 0.26)
BTN_FOCUS = (0.20, 0.68, 0.36)
BTN_OFF = (0.16, 0.18, 0.22)
BTN2 = (0.16, 0.30, 0.52)               # botones secundarios (BUSCAR, QUITAR, atajos)
BTN2_FOCUS = (0.24, 0.44, 0.74)
TAB_ON = (0.20, 0.34, 0.60)
TAB_OFF = (0.10, 0.12, 0.17)
# Color del perfil por banda de intensidad (`workouts.band`): recuperacion ... maximo
BAND_COLORS = ((0.42, 0.47, 0.56), (0.18, 0.48, 0.95), (0.13, 0.77, 0.37), (0.98, 0.80, 0.08),
               (0.98, 0.45, 0.09), (0.94, 0.27, 0.27), (0.66, 0.33, 0.97))

# Rectangulos del lienzo: (x0, y0, x1, y1)
TAB_RECTS = {"page:sensores": (-1.70, 0.665, -0.88, 0.765), "page:sesion": (-0.84, 0.665, -0.02, 0.765)}
LIST_PANEL = (-1.70, -0.56, -0.06, 0.62)
CARDS_PANEL = (0.06, -0.56, 1.70, 0.62)
SCAN_RECT = (-0.74, 0.475, -0.12, 0.595)
QUIT_RECT = (1.20, -0.300, 1.62, -0.200)
PLAY_RECT = (-0.46, -0.90, 0.46, -0.72)
FC_CARD = (0.10, 0.00, 1.66, 0.49)
TR_CARD = (0.10, -0.52, 1.66, -0.04)
ROW_X0, ROW_X1, ROW_TOP, ROW_H, ROW_GAP = -1.66, -0.10, 0.405, 0.100, 0.010
# pagina sesion
S_ROW_TOP, S_ROW_H, S_ROW_GAP = 0.485, 0.084, 0.008
R_X0, R_X1 = 0.14, 1.62
CHART = (R_X0, -0.50, R_X1, -0.21)
FREE_STEPPERS = {"min1": (0.14, 0.275, 1.62, 0.350), "min5": (0.14, 0.190, 1.62, 0.265)}
QUICK = (("min:set:15", "15"), ("min:set:30", "30"), ("min:set:45", "45"), ("min:set:60", "60"),
         ("min:set:90", "90"), ("min:set:0", "SIN LIMITE"))
QUICK_Y = (0.040, 0.120)
CUSTOM_FIELDS = (("cu:warm", "CALENTAR"), ("cu:reps", "SERIES"), ("cu:work", "FUERTE"),
                 ("cu:wlvl", "NIVEL FUERTE"), ("cu:rest", "SUAVE"), ("cu:rlvl", "NIVEL SUAVE"),
                 ("cu:cool", "ENFRIAR"))
CUSTOM_Y0, CUSTOM_PITCH, CUSTOM_H = 0.335, 0.0745, 0.066
STEPPER_XV = 1.20                       # centro de la zona de valor de un stepper (lienzo)
STEPPER_ARROW = 0.32                    # distancia de cada flecha al centro


@dataclass(frozen=True)
class Widget:
    id: str
    kind: str                           # "button" | "row" | "stepper"
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


def _srow_rect(i: int):
    y1 = S_ROW_TOP - i * (S_ROW_H + S_ROW_GAP)
    return (ROW_X0, y1 - S_ROW_H, ROW_X1, y1)


def to_layout(nxy, aspect: float):
    """(x, y) de la ventana en 0..1 (origen abajo-izquierda) -> coordenadas de pantalla del HUD."""
    return ((nxy[0] * 2.0 - 1.0) * aspect, nxy[1] * 2.0 - 1.0)


def current_page(menu) -> str:
    p = (menu or {}).get("page")
    return p if p in PAGES else "sensores"


def session_rows() -> list:
    """[(accion, titulo, subtitulo)] de la lista de tipos de sesion."""
    rows = [("kind:free", "LIBRE", "MINUTOS A ELEGIR")]
    for pid in workouts.PRESET_IDS:
        w = workouts.preset(pid)
        rows.append((f"kind:preset:{pid}", w.name.upper(), workouts.fmt_total(w.total_s)))
    rows.append(("kind:custom", "PERSONALIZADA", "ARMA TUS INTERVALOS"))
    return rows


def _active_kind_act(session) -> str:
    s = session or {}
    if s.get("kind") == "preset":
        return f"kind:preset:{s.get('preset')}"
    return "kind:custom" if s.get("kind") == "custom" else "kind:free"


def menu_widgets(menu, aspect: float) -> list:
    """Widgets del menu en orden de foco (funcion pura). Los deshabilitados siguen en la lista
    (se dibujan apagados) pero ni el foco ni el clic los toman."""
    menu = menu or {}
    s = _scale(aspect)
    page = current_page(menu)
    ws = [Widget(wid, "button", _r(rect, s), label, True, {"tab": True, "active": wid == "page:" + page})
          for (wid, rect), label in zip(TAB_RECTS.items(), ("1 SENSORES", "2 SESION"))]
    if page == "sensores":
        scanning = (menu.get("scan") or {}).get("state") == "scanning"
        ws.append(Widget("scan", "button", _r(SCAN_RECT, s), "BUSCANDO..." if scanning else "BUSCAR",
                         enabled=not scanning))
        ws.append(Widget("sim", "row", _r(_row_rect(0), s), "SIMULADOR", True,
                         {"sim": True, "use": bool(menu.get("sim"))}))
        for i, d in enumerate((menu.get("devices") or [])[:MAX_ROWS - 1]):
            ws.append(Widget("dev:" + str(d.get("id")), "row", _r(_row_rect(i + 1), s),
                             str(d.get("name") or ""), True, d))
        if menu.get("trainer"):
            ws.append(Widget("notrainer", "button", _r(QUIT_RECT, s), "QUITAR"))
    else:
        session = menu.get("session") or {}
        active = _active_kind_act(session)
        for i, (act, title, sub) in enumerate(session_rows()):
            ws.append(Widget(act, "row", _r(_srow_rect(i), s), title, True,
                             {"sub": sub, "use": act == active}))
        kind = session.get("kind", "free")
        xv = STEPPER_XV * s
        if kind == "free":
            for wid, rect in FREE_STEPPERS.items():
                ws.append(Widget(wid, "stepper", _r(rect, s), "MINUTOS (+/- %s)" % ("1" if wid == "min1" else "5"),
                                 True, {"xv": xv}))
            n, gap = len(QUICK), 0.03
            widths = [0.17] * (n - 1) + [R_X1 - R_X0 - (n - 1) * (0.17 + gap)]
            x = R_X0
            for (act, label), w in zip(QUICK, widths):
                ws.append(Widget(act, "button", _r((x, QUICK_Y[0], x + w, QUICK_Y[1]), s), label, True,
                                 {"quick": True, "use": session.get("minutes") == int(act.split(":")[-1])}))
                x += w + gap
        elif kind == "custom":
            for i, (wid, label) in enumerate(CUSTOM_FIELDS):
                yc = CUSTOM_Y0 - i * CUSTOM_PITCH
                ws.append(Widget(wid, "stepper", _r((R_X0, yc - CUSTOM_H / 2, R_X1, yc + CUSTOM_H / 2), s),
                                 label, True, {"xv": xv}))
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


def hit_act(menu, nxy, aspect: float):
    """Accion a mandar al motor por un clic en `nxy`, o None. En un stepper depende del lado: a la
    izquierda del valor resta (`id:-`) y a la derecha suma (`id:+`)."""
    ws = menu_widgets(menu, aspect)
    x, y = to_layout(nxy, aspect)
    wid = hit_test(ws, x, y)
    if wid is None:
        return None
    w = next(w for w in ws if w.id == wid)
    return act_of(w, -1 if (w.kind == "stepper" and x < w.data.get("xv", 0.0)) else 1)


def act_of(w: Widget, step: int = 1) -> str:
    """Accion de un widget: su id, o `id:-`/`id:+` si es un stepper."""
    return f"{w.id}:{'-' if step < 0 else '+'}" if w.kind == "stepper" else w.id


def focus_or_default(widgets, focus_id):
    """El foco si sigue siendo un widget habilitado; si no (desaparecio al cambiar la lista),
    INICIAR si esta habilitado y si no lo primero util de la pagina (no una pestana: sin FC, lo
    que hace falta es buscar o elegir un sensor, y las pestanas van delante en el orden)."""
    on = [w.id for w in widgets if w.enabled]
    if focus_id in on:
        return focus_id
    if "play" in on:
        return "play"
    useful = [w.id for w in widgets if w.enabled and not w.data.get("tab")]
    return useful[0] if useful else (on[0] if on else None)


def move_focus(widgets, focus_id, step: int):
    """Foco tras mover `step` posiciones por los widgets habilitados (con vuelta)."""
    on = [w.id for w in widgets if w.enabled]
    if not on:
        return None
    cur = focus_or_default(widgets, focus_id)
    return on[(on.index(cur) + step) % len(on)]


def activate(widgets, focus_id, step: int = 1):
    """Accion a mandar al motor al activar el foco (con `step` = -1 resta en un stepper), o None
    si no hay nada habilitado en el foco."""
    f = focus_or_default(widgets, focus_id)
    w = next((w for w in widgets if w.id == f and w.enabled), None)
    return act_of(w, step) if w is not None else None


def focused_stepper(widgets, focus_id) -> bool:
    f = focus_or_default(widgets, focus_id)
    return any(w.id == f and w.kind == "stepper" and w.enabled for w in widgets)


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
def _static(s: float, page: str) -> Mesh:
    m = _panel_mesh()
    _rect(m, s, (-2.4, -1.2, 2.4, 1.2), -0.5, BG)
    _rect(m, s, LIST_PANEL, -0.3, PANEL)
    _rect(m, s, CARDS_PANEL, -0.3, PANEL)
    _t(m, s, 0.0, 0.895, 0.105, "HRDASH", WHITE, "c")
    _t(m, s, 1.70, 0.715, 0.034, "ENTRENAMIENTO CON FC Y RODILLO", DIM, "r")
    if page == "sensores":
        _t(m, s, -1.64, 0.545, 0.046, "DISPOSITIVOS", INK)
        _t(m, s, 0.14, 0.545, 0.046, "SENSORES EN USO", INK)
        keys = "FLECHAS O TAB: MOVER   ENTER: ELEGIR   B: BUSCAR   1 O 2: PAGINA   Q O ESC: SALIR"
    else:
        _t(m, s, -1.64, 0.545, 0.046, "TIPO DE SESION", INK)
        keys = "FLECHAS O TAB: MOVER   IZQ/DER: AJUSTAR   ENTER: ELEGIR   1 O 2: PAGINA   Q O ESC: SALIR"
    _t(m, s, 0.0, -0.965, 0.028, keys, DIM, "c")
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


def _button_label(m, s, w, z=0.06):
    """Etiqueta de un boton (con el triangulo de play en INICIAR), ajustada al ancho."""
    x0, y0, x1, y1 = (v / s for v in w.rect)
    yc, hh = (y0 + y1) / 2.0, (y1 - y0)
    col = DIM if not w.enabled else WHITE
    lab = w.label
    if w.id == "play":
        lh = _fit_h(lab, hh * 0.40, (x1 - x0) * 0.62)
        tw = ink_w(font_text(lab), lh)
        ts = hh * 0.32
        tx = (x0 + x1) / 2.0 - (ts * 1.6 + hh * 0.35 + tw) / 2.0
        m.tri((tx * s, (yc - ts) * s, 0.06), ((tx + ts * 1.6) * s, yc * s, 0.06),
              (tx * s, (yc + ts) * s, 0.06), WHITE if w.enabled else DIM, (0.0, 0.0, 1.0), 0.9)
        _t(m, s, tx + ts * 1.6 + hh * 0.35, yc, lh, lab, col, z=z, light=0.95)
    else:
        _t(m, s, (x0 + x1) / 2.0, yc, _fit_h(lab, hh * 0.46, (x1 - x0) * 0.86), lab, col, "c", z=z)


def _list_sensores(menu, widgets, s: float, m: Mesh) -> None:
    line, col = _scan_line(menu.get("scan"))
    _t(m, s, -1.64, -0.495, 0.036, line, col, maxw=1.54)
    for w in widgets:
        if w.kind != "row":
            continue
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
        _t(m, s, x0 + 0.04, yc + 0.024, 0.034, tag, tcol)
        _t(m, s, x0 + 0.24, yc + 0.025, 0.046, name, INK, maxw=1.12)
        _t(m, s, x0 + 0.24, yc - 0.027, 0.030, info, DIM, maxw=0.80)
        if d.get("use"):
            _t(m, s, x1 - 0.14, yc - 0.027, 0.030, "EN USO", GREEN, "r", z=0.01)
        if not d.get("sim"):                          # barras de senal: 4, encendidas segun RSSI
            lvl = _rssi_level(int(d.get("rssi", -100)))
            bx = x1 - 0.115
            for i in range(4):
                on = i < lvl
                h = 0.020 + 0.014 * i
                _rect(m, s, (bx + i * 0.020, yc + 0.020 - h, bx + i * 0.020 + 0.014, yc + 0.020),
                      0.01, GREEN if on else (0.22, 0.25, 0.30), 0.5 if on else 0.0)
    hr, tr = menu.get("hr"), menu.get("trainer")
    for card, label, src, empty in ((FC_CARD, "FRECUENCIA CARDIACA", hr, "SIN BANDA"),
                                    (TR_CARD, "RODILLO", tr, "SIN RODILLO")):
        x0, y0, x1, y1 = card
        _t(m, s, x0 + 0.06, y1 - 0.050, 0.030, label, DIM)
        name, state, scol = _src_state(src, empty)
        _t(m, s, x0 + 0.06, y1 - 0.130, 0.052, name, INK, maxw=1.40)
        _t(m, s, x0 + 0.06, y1 - 0.205, 0.036, state or ("ELIGE UNO EN LA LISTA" if not src else ""),
           scol if state else DIM, maxw=0.98 if card is TR_CARD else 1.4)


def _band_color(pct: int):
    return BAND_COLORS[workouts.band(pct)]


def _chart(m, s, session) -> None:
    """Perfil de bloques: una barra por bloque, ancho = duracion y alto/color = intensidad."""
    x0, y0, x1, y1 = CHART
    _rect(m, s, (x0, y0 - 0.004, x1, y0), 0.0, (0.30, 0.34, 0.40))
    prof = session.get("profile") or []
    total = sum(sec for sec, _ in prof)
    if not total:
        _t(m, s, (x0 + x1) / 2.0, (y0 + y1) / 2.0, 0.034, "SIN PLAN: SOLO CRONOMETRO", DIM, "c")
        return
    t = 0.0
    for sec, pct in prof:
        bx0 = x0 + (x1 - x0) * t / total
        bx1 = x0 + (x1 - x0) * (t + sec) / total
        h = (y1 - y0) * max(0.12, min(1.0, pct / 140.0))
        gap = 0.0015 if bx1 - bx0 > 0.006 else 0.0
        _rect(m, s, (bx0, y0, bx1 - gap, y0 + h), 0.0, _band_color(pct), 0.55)
        t += sec
    _t(m, s, x0, y0 - 0.036, 0.026, "0", DIM)
    _t(m, s, x1, y0 - 0.036, 0.026, workouts.fmt_total(total), DIM, "r")


def _list_sesion(menu, widgets, s: float, m: Mesh) -> None:
    session = menu.get("session") or {}
    kind = session.get("kind", "free")
    for w in widgets:
        if w.kind == "row":                                   # tipos de sesion
            x0, y0, x1, y1 = (v / s for v in w.rect)
            yc = (y0 + y1) / 2.0
            _t(m, s, x0 + 0.06, yc + 0.020, 0.040, w.label, INK, maxw=1.20)
            _t(m, s, x0 + 0.06, yc - 0.022, 0.028, w.data.get("sub", ""), DIM, maxw=1.0)
            if w.data.get("use"):
                _t(m, s, x1 - 0.06, yc, 0.030, "ELEGIDA", GREEN, "r", z=0.01)
    title = {"free": "SESION LIBRE", "custom": "SESION PERSONALIZADA"}.get(kind) or \
        (session.get("name") or "").upper()
    _t(m, s, R_X0, 0.545, 0.046, title, INK, maxw=1.46)
    for w in widgets:
        if w.kind == "stepper":
            x0, y0, x1, y1 = (v / s for v in w.rect)
            yc = (y0 + y1) / 2.0
            label, val, sub = w.label, "", ""
            c = session.get("custom") or {}
            if w.id in ("min1", "min5"):
                mins = session.get("minutes", 0)
                val = "SIN LIMITE" if not mins else "%d MIN" % mins
            elif w.id == "cu:warm":
                val = "%d MIN" % c.get("warm", 0) if c.get("warm") else "SIN"
            elif w.id == "cu:cool":
                val = "%d MIN" % c.get("cool", 0) if c.get("cool") else "SIN"
            elif w.id == "cu:reps":
                val = "%d" % c.get("reps", 1)
            elif w.id == "cu:work":
                val = _mmss(c.get("work_s", 0))
            elif w.id == "cu:rest":
                val = _mmss(c.get("rest_s", 0)) if c.get("rest_s") else "SIN PAUSA"
            elif w.id in ("cu:wlvl", "cu:rlvl"):
                lvl = c.get("work" if w.id == "cu:wlvl" else "rest", "")
                val = workouts.LEVEL_NAMES.get(lvl, lvl.upper())
                sub = _level_range(lvl)
            if sub:
                _t(m, s, x0 + 0.05, yc + 0.016, 0.030, label, INK)
                _t(m, s, x0 + 0.05, yc - 0.017, 0.020, sub, DIM)
            else:
                _t(m, s, x0 + 0.05, yc, 0.036, label, INK)
            xv = w.data["xv"] / s
            _t(m, s, xv, yc, _fit_h(val, 0.040, 2 * STEPPER_ARROW - 0.06), val, AMBER, "c")
            _t(m, s, xv - STEPPER_ARROW, yc, 0.050, "<", WHITE)
            _t(m, s, xv + STEPPER_ARROW, yc, 0.050, ">", WHITE, "r")
        elif w.kind == "button" and w.data.get("quick"):
            _button_label(m, s, w)
    # total grande, descripcion y perfil
    if kind == "free":
        _t(m, s, R_X0, 0.150, 0.026, "ATAJOS (MINUTOS)", DIM)
    if kind == "preset":
        lines = []
        for line in session.get("lines") or []:
            head, sep, pause = line.partition(" PAUSA ")      # la pausa va en su propia linea
            lines += [head] + (["    PAUSA " + pause] if sep else [])
        for i, line in enumerate(lines[:6]):
            _t(m, s, R_X0, 0.31 - i * 0.070, 0.036, line, DIM if "PAUSA" in line else INK, maxw=1.46)
    _chart(m, s, session)
    # pie: resumen de la sesion y motivo por el que no se puede iniciar
    _t(m, s, 0.0, -0.605, 0.034, "SESION: " + (session.get("summary") or ""), INK, "c", maxw=1.9)


def _mmss(secs: int) -> str:
    m, s_ = divmod(int(secs), 60)
    return f"{m}:{s_:02d}"


def _level_range(level: str) -> str:
    lv = workouts.LEVELS.get(level)
    return "" if not lv else workouts._fmt_range(lv[0], "FTP")


def _list(menu, widgets, s: float) -> Mesh:
    """Todo el texto y adornos que solo cambian cuando cambia la lista, el estado o la sesion."""
    m = _panel_mesh()
    menu = menu or {}
    for w in widgets:                                         # pestanas y botones
        if w.kind == "button" and not w.data.get("quick"):
            _button_label(m, s, w)
    if current_page(menu) == "sensores":
        _list_sensores(menu, widgets, s, m)
        _t(m, s, 0.0, -0.605, 0.034, "SESION: " + ((menu.get("session") or {}).get("summary") or ""),
           INK, "c", maxw=1.9)
    else:
        _list_sesion(menu, widgets, s, m)
    _t(m, s, 0.0, -0.672, 0.038, menu.get("hint") or "", AMBER, "c", maxw=1.9)
    return m


def _values(menu, s: float) -> Mesh:
    """Lecturas en vivo (cambian a cada snapshot) y la duracion total de la sesion."""
    m = _panel_mesh()
    menu = menu or {}

    def num(x, y, h, val, fmt, col, unit):
        txt = fmt % val if val is not None else "--"
        lo, hi = digits_extent(txt, h * s)
        draw_digits(m, x * s - lo, y * s, 0.02, h * s, txt, col if val is not None else DIM, light=0.95)
        _t(m, s, x + (hi - lo) / s + 0.03, y - h * 0.30, 0.036, unit, DIM)

    if current_page(menu) == "sesion":
        session = menu.get("session") or {}
        total = int(session.get("total_s") or 0)
        if session.get("kind") == "free":
            mins = session.get("minutes", 0)
            num(R_X0 + 0.02, 0.435, 0.09, mins if mins else None, "%d", AMBER, "MIN" if mins else "SIN LIMITE")
        elif total:
            mm, ss = divmod(total, 60)
            if ss:
                txt = "%d:%02d" % (mm, ss)
                lo, hi = digits_extent(txt, 0.09 * s)
                draw_digits(m, (R_X0 + 0.02) * s - lo, 0.435 * s, 0.02, 0.09 * s, txt, AMBER, light=0.95)
                _t(m, s, R_X0 + 0.02 + (hi - lo) / s + 0.03, 0.408, 0.036, "MIN", DIM)
            else:
                num(R_X0 + 0.02, 0.435, 0.09, mm, "%d", AMBER, "MIN")
        return m
    hr = menu.get("hr")
    if hr:
        num(0.16, 0.115, 0.17, hr.get("hr"), "%d", AMBER, "BPM")
    tr = menu.get("trainer")
    if tr:
        num(0.16, -0.40, 0.12, tr.get("watts"), "%d", AMBER, "W")
        num(0.62, -0.40, 0.12, tr.get("cadence"), "%d", AMBER, "RPM")
        num(1.08, -0.40, 0.12, tr.get("speed"), "%.1f", AMBER, "KM/H")
    return m


def _frame(widgets, focus, t: float, scanning: bool, s: float) -> Mesh:
    """Fondos de botones, filas y steppers, foco y animacion: lo unico que se rehace en cada frame."""
    m = _panel_mesh()
    pulse = 0.5 + 0.5 * math.sin(t * 2.6)
    for w in widgets:
        x0, y0, x1, y1 = w.rect
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        foc = w.id == focus
        if w.kind in ("row", "stepper"):
            col = ROW_FOCUS if foc else (ROW_USE if w.data.get("use") else ROW)
            if foc:
                m.box(cx, cy, -0.12, x1 - x0 + 0.012 * s, y1 - y0 + 0.012 * s, 0.01, RING, light=0.5)
            m.box(cx, cy, -0.1, x1 - x0, y1 - y0, 0.01, col, light=0.15)
            continue
        if not w.enabled:
            m.box(cx, cy, 0.04, x1 - x0, y1 - y0, 0.01, BTN_OFF, light=0.1)
            continue
        if w.data.get("tab"):
            base, hot = (TAB_ON, BTN2_FOCUS) if w.data.get("active") else (TAB_OFF, BTN2)
        elif w.data.get("quick") and w.data.get("use"):
            base, hot = (BTN, BTN_FOCUS)
        else:
            base, hot = (BTN, BTN_FOCUS) if w.id == "play" else (BTN2, BTN2_FOCUS)
        edge = 0.012 * s + (0.010 * s if foc else 0.0)
        rim = RING if w.id == "play" else (0.34, 0.50, 0.80)
        glow = tuple(c * (0.55 + 0.25 * pulse) for c in rim)
        if w.data.get("tab") and not w.data.get("active"):
            glow = (0.18, 0.22, 0.30)
        m.box(cx, cy, 0.02, x1 - x0 + 2 * edge, y1 - y0 + 2 * edge, 0.01, RING if foc else glow,
              light=0.5 if foc else 0.3)
        m.box(cx, cy, 0.04, x1 - x0, y1 - y0, 0.01,
              hot if foc else tuple(c * (0.92 + 0.08 * pulse) for c in base), light=0.35)
    if scanning:                                       # barra indeterminada bajo la cabecera
        bx0, bx1 = ROW_X0 * s, ROW_X1 * s
        seg = (bx1 - bx0) * 0.22
        x = bx0 + ((t * 0.9) % 1.0) * ((bx1 - bx0) - seg)
        m.box((bx0 + bx1) / 2.0, 0.455 * s, -0.05, bx1 - bx0, 0.008 * s, 0.01, (0.18, 0.20, 0.25))
        m.box(x + seg / 2.0, 0.455 * s, -0.04, seg, 0.010 * s, 0.01, AMBER, light=0.8)
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
    page = current_page(menu)
    widgets = menu_widgets(menu, aspect)
    focus = focus_or_default(widgets, anim.get("menu_focus"))
    sigs = anim.setdefault("menu_sigs", {})

    def cached(name, sig, build):
        if sigs.get(name) == sig:
            return (name, None)                          # sin cambios: conserva su VBO
        sigs[name] = sig
        return (name, build())

    scanning = page == "sensores" and (menu.get("scan") or {}).get("state") == "scanning"
    a = round(aspect, 3)
    layers = [cached("hud_static", (a, page), lambda: _static(s, page)),
              cached("hud_list", (a, _list_sig(menu, widgets)), lambda: _list(menu, widgets, s)),
              cached("hud_values", (a, page, snap.get("version")), lambda: _values(menu, s)),
              ("hud_frame", _frame(widgets, focus, anim.get("t", 0.0), scanning, s))]
    return layers, ((0.0, -5.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.6, 50.0, True)
