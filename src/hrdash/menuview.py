"""Escena `menu` de la ventana 3D: lo que se ve antes de pulsar Play.

El padre (motor) es dueno de los datos y manda un bloque `menu` en el snapshot; esta escena solo
lo dibuja y resuelve la interaccion (hover, clic). Los widgets se calculan en `menu_widgets`, que
es una funcion PURA de (menu, aspecto): la usa la escena para dibujar y el hijo para saber que
se pulso, asi dibujo y zona de clic no pueden separarse. Coordenadas de pantalla: x en
[-aspecto, aspecto], y en [-1, 1], origen al centro, y hacia arriba (las del HUD).

Se dibuja en las capas `hud_*` (camara ortografica, segunda pasada de `glview._draw`); no hay
escena 3D debajo. El hijo devuelve eventos `{"ui": {"act": <id del widget>}}`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .cluster import AMBER, DIM, GREEN, INK, RED, WHITE, draw_text, ink_w
from .glview import Mesh

BG = (0.045, 0.052, 0.068)
PANEL = (0.085, 0.098, 0.125)
BTN = (0.14, 0.50, 0.26)
BTN_HOVER = (0.20, 0.68, 0.36)
BTN_EDGE = (0.45, 0.90, 0.58)


@dataclass(frozen=True)
class Widget:
    id: str
    kind: str                     # "button"
    rect: tuple                   # (x0, y0, x1, y1)
    label: str
    enabled: bool = True


def _scale(aspect: float) -> float:
    """Todo se encoge en ventanas estrechas para que nada se salga de la pantalla."""
    return max(0.35, min(1.0, aspect / 1.3))


def to_layout(nxy, aspect: float):
    """(x, y) de la ventana en 0..1 (origen abajo-izquierda) -> coordenadas de la escena."""
    return ((nxy[0] * 2.0 - 1.0) * aspect, nxy[1] * 2.0 - 1.0)


def menu_widgets(menu, aspect: float) -> list:
    """Widgets del menu para este aspecto (funcion pura)."""
    s = _scale(aspect)
    bw, bh = min(0.99 * s, aspect * 1.7), 0.22 * s
    return [Widget("play", "button", (-bw / 2, -0.40 - bh / 2, bw / 2, -0.40 + bh / 2), "INICIAR")]


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


def _status(src, label):
    """(texto, color) de una fuente para el resumen de sensores."""
    if src is None:
        return "SIN RODILLO", DIM
    if src.get("connected"):
        if label == "FC":
            v = src.get("hr")
            return ("CONECTADA  %d BPM" % v if v else "CONECTADA  ESPERANDO FC"), GREEN
        parts = []
        if src.get("watts") is not None:
            parts.append("%d W" % src["watts"])
        if src.get("cadence") is not None:
            parts.append("%d RPM" % round(src["cadence"]))
        return "CONECTADO  " + "  ".join(parts) if parts else "CONECTADO", GREEN
    err = (src.get("error") or "").strip()
    if err:
        return ("ERROR: " + err)[:34], RED
    return "BUSCANDO...", AMBER


def _clip(txt: str, h: float, maxw: float) -> str:
    """Recorta el texto hasta que su trazo cabe en `maxw` (por ancho real, no por caracteres)."""
    while txt and ink_w(txt, h) > maxw:
        txt = txt[:-1]
    return txt


def _static(menu, aspect: float) -> Mesh:
    """Lo que solo cambia con el snapshot (a ~2 Hz): fondo, titulo, sensores y ayuda."""
    m = Mesh()
    m.panel(0.0, 0.0)
    s = _scale(aspect)
    m.box(0.0, 0.0, -0.5, 2.0 * aspect + 0.2, 2.2, 0.02, BG)                      # fondo
    pw = min(1.9 * s * 1.0, 2.0 * aspect - 0.1)
    m.box(0.0, 0.10, -0.3, pw, 0.50 * s + 0.12, 0.02, PANEL)                      # panel de sensores
    draw_text(m, 0.0, 0.66, 0.0, 0.16 * s, "HRDASH", WHITE, light=0.9, align="c", depth=0.04)
    draw_text(m, 0.0, 0.50, 0.0, 0.040 * s, "ENTRENAMIENTO CON FC Y RODILLO", DIM, light=0.8, align="c")
    menu = menu or {}
    rows = (("FC", menu.get("hr")), ("RODILLO", menu.get("trainer", None)))
    h = 0.046 * s
    x0 = -pw / 2 + 0.08 * s
    xv = x0 + 0.52 * s                                  # columna de nombre y estado
    maxw = pw / 2 - 0.06 * s - xv
    for i, (label, src) in enumerate(rows):
        y = 0.25 - i * 0.20 * s
        txt, col = _status(src, label)
        name = (src.get("name") or "").strip() if src else ""
        draw_text(m, x0, y, 0.0, h, label, DIM, light=0.8)
        if name:
            draw_text(m, xv, y, 0.0, h, _clip(name, h, maxw), INK, light=0.85)
        draw_text(m, xv, y - 0.080 * s, 0.0, h * 0.85, _clip(txt, h * 0.85, maxw), col, light=0.9)
    draw_text(m, 0.0, -0.88, 0.0, 0.030 * s, "ENTER / ESPACIO / CLIC: INICIAR   -   Q / ESC: SALIR",
              DIM, light=0.7, align="c")
    return m


def _frame(widgets, hover, t: float) -> Mesh:
    """Botones: cambian con el cursor y laten un poco, asi que van por frame (son pocos)."""
    m = Mesh()
    m.panel(0.0, 0.0)
    for w in widgets:
        x0, y0, x1, y1 = w.rect
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        hot = hover == w.id
        pulse = 0.5 + 0.5 * math.sin(t * 2.6)
        col = BTN_HOVER if hot else tuple(c * (0.92 + 0.08 * pulse) for c in BTN)
        edge = 0.012 + (0.010 if hot else 0.0)
        m.box(cx, cy, 0.02, x1 - x0 + 2 * edge, y1 - y0 + 2 * edge, 0.01, BTN_EDGE if hot else
              tuple(c * (0.55 + 0.25 * pulse) for c in BTN_EDGE), light=0.5 if hot else 0.3)
        m.box(cx, cy, 0.04, x1 - x0, y1 - y0, 0.01, col, light=0.35)
        # icono de play (triangulo) a la izquierda y la etiqueta a su lado
        hgt = (y1 - y0) * 0.50
        ts = hgt * 0.5
        label_h = (y1 - y0) * 0.36
        tw = ink_w(w.label, label_h)
        total = ts * 1.6 + hgt * 0.45 + tw
        tx = cx - total / 2.0
        n = (0.0, 0.0, 1.0)
        m.tri((tx, cy - ts, 0.06), (tx + ts * 1.6, cy, 0.06), (tx, cy + ts, 0.06), WHITE, n, 0.9)
        draw_text(m, tx + ts * 1.6 + hgt * 0.45, cy, 0.06, label_h, w.label, WHITE, light=0.95,
                  align="l", depth=0.03)
    return m


def scene_menu(snap, anim, aspect=1.6):
    """Capas `hud_*` del menu y la camara (la escena 3D queda vacia: todo es HUD)."""
    menu = snap.get("menu") or {}
    widgets = menu_widgets(menu, aspect)
    mouse = anim.get("mouse_n")
    hover = hit_id(menu, mouse, aspect) if mouse else None
    key = (snap.get("version"), round(aspect, 3))
    layers = []
    if anim.get("menu_key") != key:
        anim["menu_key"] = key
        layers.append(("hud_static", _static(menu, aspect)))
    else:
        layers.append(("hud_static", None))
    layers.append(("hud_frame", _frame(widgets, hover, anim.get("t", 0.0))))
    return layers, ((0.0, -5.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 0.6, 50.0, True)
