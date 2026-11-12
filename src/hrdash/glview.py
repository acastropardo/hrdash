"""Ventana OpenGL nativa (3D real) al lado del dashboard.

La TUI de Textual ocupa el hilo principal y Terminal.app no pinta imagenes (ningun
protocolo grafico), asi que la escena 3D corre en **otro proceso** con su ventana
GLFW + contexto moderngl: camara en perspectiva, z-buffer y Lambert. El dashboard le
manda un snapshot por tuberia a ~1 Hz y la ventana anima localmente (~50 fps: aguja
con inercia, bici recorriendo la vuelta, camara orbital).

Escenas por vista (la tecla `3` abre/cierra la ventana; el modo persiste al navegar
con `h`/`w`/`g`/`p` y `← →`):

- hr: barras 3D del historial de FC coloreadas por zona + barra de la FC actual.
- watts: cajas 3D por banda de %FTP (altura = kJ).
- gauge: dial 3D (aro de balizas por zona, cubo y aguja con inercia).
- pista: velodromo con peralte, meta cuadriculada, estela y marcador.

Nada de esto puede tumbar el muestreo: el host atrapa todo y la TUI sigue en 2D.
"""

from __future__ import annotations

import math
import time

from .proto import VIEW_ORDER

VS = """#version 330
in vec3 in_position; in vec3 in_normal; in vec3 in_color; in float in_light;
out vec3 v_color; out vec3 v_normal; out float v_light; out vec3 v_world;
uniform mat4 mvp;
void main(){
    gl_Position = mvp * vec4(in_position, 1.0);
    v_color = in_color;
    v_normal = in_normal;
    v_light = in_light;
    v_world = in_position;
}"""

# Luz del pabellon, compartida por el programa general y el de la cubierta: si cada uno
# llevara su copia, el tablon y el resto de la escena acabarian con luces distintas.
SHADE = """
uniform vec3 EYE;
vec4 shade(vec3 v_color, vec3 v_normal, float v_light, vec3 v_world){
    vec3 N = normalize(v_normal);
    // Pabellon: luz principal calida (3000K), relleno frio y rebote del madera.
    // Con una sola luz fria plana el velodromo sale gris; las referencias muestran
    // madera ambarina con charcos de luz bajo las lamparas.
    vec3 L1 = normalize(vec3(0.30, -0.55, 0.75));
    vec3 L2 = normalize(vec3(-0.45, 0.20, 0.55));
    vec3 amb = mix(vec3(0.055, 0.060, 0.072), vec3(0.150, 0.160, 0.185), clamp(N.z, 0.0, 1.0));
    vec3 lit = v_color * (amb + vec3(0.95, 0.87, 0.73) * max(dot(N, L1), 0.0)
                          + vec3(0.19, 0.24, 0.32) * max(dot(N, L2), 0.0));
    vec3 V = normalize(EYE - v_world);
    float spec = pow(max(dot(N, normalize(V + L1)), 0.0), 22.0);
    lit += vec3(0.14, 0.12, 0.10) * spec;             // brillo del tablon barnizado
    vec3 glow = v_color * 1.22;
    vec3 col = clamp(mix(lit, glow, clamp(v_light, 0.0, 1.0)), 0.0, 1.0);
    float fog = clamp(1.0 - exp(-length(EYE - v_world) * 0.011), 0.0, 1.0);
    return vec4(mix(col, vec3(0.052, 0.060, 0.075), fog), 1.0);
}"""

FS = """#version 330
in vec3 v_color; in vec3 v_normal; in float v_light; in vec3 v_world;
out vec4 o;
""" + SHADE + """
void main(){ o = shade(v_color, v_normal, v_light, v_world); }"""

FLOOR = (0.09, 0.10, 0.13)
VATTR = "3f 3f 3f 1f"                     # posicion, normal, color, emisivo
FIELDS = ("in_position", "in_normal", "in_color", "in_light")
GRID = (0.19, 0.22, 0.27)

# Paleta del velodromo, muestreada de las fotos de referencia (pabellon a ~3000K) decodificando
# el JPEG a BMP y promediando bloques: la madera es mas clara y menos saturada de lo que parece
# a ojo (#b59b77 en zona media, #d7c2a9 bajo la lampara). El shader multiplica el albedo por la
# luz, asi que el albedo se pone un poco por encima del valor de foto para que coincida.
WOOD = (0.735, 0.635, 0.494)              # tablon, albedo medio (#bba17e)
WOOD_D = (0.660, 0.569, 0.439)            # junta entre tablones (la base, mas oscura)
APRON = (0.246, 0.345, 0.478)             # apron de hormigon pintado, en el interior
SHADOW = (0.098, 0.105, 0.118)
AZUR = (0.216, 0.329, 0.490)              # cote d'azur: el azul del borde interior
LINE_BLACK = (0.055, 0.055, 0.062)        # linea de referencia (datum), la mas rapida
LINE_RED = (0.686, 0.157, 0.145)          # linea del sprinter (70 cm sobre la negra)
LINE_BLUE = (0.231, 0.369, 0.545)         # linea del stayer (madison)
LINE_WHITE = (0.827, 0.847, 0.871)        # borde exterior
META = (0.900, 0.920, 0.950)              # banda de meta y linea de 200 m
META_W, LINE_W = 0.72, 0.04               # banda de meta y lineas transversales (aprox. UCI)
WALL = (0.451, 0.380, 0.227)              # madera del muro perimetral (#73613a en la foto)
SEAT = (0.157, 0.196, 0.255)              # butacas
SEAT_F = (0.118, 0.145, 0.185)            # escalon entre filas
# Bloques de color en las gradas (bloques de butacas sueltos, como en las fotos) y
# pasillos cada 12 m: sin pasillos la grada se lee como una mancha continua.
SEATS = [SEAT, (0.400, 0.290, 0.130), (0.420, 0.165, 0.145), (0.215, 0.300, 0.395)]
STEEL = (0.26, 0.29, 0.34)
RIB = (0.349, 0.380, 0.420)               # cerchos de madera laminada
ROOF = (0.190, 0.172, 0.145)              # cupula: gris calido, no neutro
LAMP = (0.941, 0.863, 0.651)              # lamparas calidas colgadas
INFIELD = (0.106, 0.122, 0.149)
INFIELD_FLOOR = (0.235, 0.275, 0.330)     # suelo deportivo de la zona interior
COURT_LINE = (0.80, 0.82, 0.86)
# Publicidad: (fondo, logo). Colores planos saturados, como las vallas de un velodromo.
AD_COLS = [((0.08, 0.18, 0.45), (0.92, 0.93, 0.95)), ((0.70, 0.10, 0.12), (0.95, 0.92, 0.85)),
           ((0.93, 0.93, 0.95), (0.10, 0.25, 0.60)), ((0.95, 0.72, 0.10), (0.10, 0.10, 0.12)),
           ((0.06, 0.06, 0.07), (0.95, 0.55, 0.10)), ((0.05, 0.45, 0.30), (0.92, 0.95, 0.92))]
AD_L = 6.0                                # largo de cada panel de la barandilla (m)
PITS = 8                                  # boxes de equipos en la zona interior

# Cubierta por shader (Fase 2): un velodromo real es de listones de pino de 4-5 cm a lo
# largo de la marcha, con testas escalonadas. Como geometria eso serian cientos de cintas;
# en el fragment shader es una funcion de (s, q) = (metros desde meta, metros desde el
# borde interior), filtrada con fwidth para que de lejos se promedie en vez de hacer moire.
SLAT, SLAT_L, SLAT_J = 0.05, 4.0, 0.0025  # liston: ancho, largo, junta (m)
DECK_STEP = 0.25                 # subdivision lateral de la cubierta (m); a 45 grados, 0,5 torcia 11 mm
# Velodromo cubierto de 250 m (estandar UCI de campeonatos y JJOO), 1 unidad = 1 m.
# Parametrizacion de un 250 m real con transicion en espiral de Euler (clotoide): por
# cuarto de vuelta 19 m de media recta + 13,5 m de clotoide + 30 m de medio arco, lo que da
# R = (13,5 + 60) / pi ~ 23,40 m (las dos clotoides giran lo mismo que medio arco).
# La longitud se mide sobre la linea de medicion.
TRACK_LEN = 250.0
STRAIGHT_L, TRANS_L, ARC_L = 38.0, 13.5, 60.0          # recta, clotoide y arco por curva
TRACK_STEP = 0.5                     # muestreo del trazado (m): con R 23 y 45 grados, 1 m serrucha
TRACK_W = 7.0                        # ancho del tablon (UCI: 7-8 m para 250 m)
HALF_W = TRACK_W / 2.0
COTE = 0.70                          # cote d'azur: franja azul bajo el borde interior (>= 10 %)
AXIS_Q = 0.20                        # el trazado `pts` es la linea de medicion: 20 cm del borde
BANK_S, BANK_C = math.radians(12.0), math.radians(45.0)   # peralte en recta y en el arco
DECK = 1.05                          # nivel del pie de la cote d'azur sobre el piso del recinto
WALL_H = 0.90                        # barandilla de seguridad sobre el borde exterior
STAND_ROWS, STAND_RUN, STAND_RISE = 11, 0.85, 0.40      # gradas escalonadas
# Las gradas no suben y bajan con el peralte: arrancan a la altura del borde exterior en el
# arco, y en las rectas queda un muro (frontón) entre la barandilla y la primera fila.
STAND_Z = DECK + (COTE + TRACK_W) * math.sin(BANK_C) + WALL_H
ROOF_H, LAMP_H = STAND_Z + STAND_ROWS * STAND_RISE + 9.0, STAND_Z + 5.5
# Fase 4: linea y postura segun la velocidad. Con mu ~0,6 la velocidad minima en el arco de
# 45 grados es sqrt(g R (tan45 - mu) / (1 + mu tan45)) ~ 27 km/h: por debajo, un ciclista real
# baja a la cote d'azur, y casi parado sale a la zona de seguridad.
LINE_FAST, LINE_SLOW, LINE_STOP = 34.0, 18.0, 10.0     # km/h
COTE_Q, APRON_Q = -COTE / 2.0, -COTE - 1.0              # linea en la cote d'azur y en el apron
LINE_RATE = 0.6                      # deriva lateral maxima (m/s)
LEAN_MAX, LEAN_RATE = math.radians(55.0), 1.2           # inclinacion maxima y su velocidad (rad/s)
CAM_MODES = ("chase", "tv", "grua")  # `v` los recorre en la ventana o en la TUI
TV_CAMS = 8                          # camaras fijas de la zona interior (una cada 31 m)
CAM_LEAN = 0.5                       # la chase se inclina la mitad que el rider
APRON_W = 2.5                        # zona de seguridad llana al pie de la cote d'azur
RIDER_Q = 0.50                       # linea del rider: justo encima de la negra (persecucion)
# Lineas pintadas, medidas desde el borde interior: (centro, color, ancho). La negra (de
# medicion) a 20 cm, la roja del sprinter a 85 cm, la azul del stayer a 1/3 del ancho o
# 2,45 m (lo mayor) y la cote d'azur por debajo del borde.
PAINT = ((-COTE / 2.0, "AZUR", COTE), (0.225, "LINE_BLACK", 0.05), (0.875, "LINE_RED", 0.05),
         (max(TRACK_W / 3.0, 2.45) + 0.025, "LINE_BLUE", 0.05), (TRACK_W - 0.05, "LINE_WHITE", 0.05))
CHASE_DIST = 2.4                    # chase por defecto; la rueda acerca hasta primera persona
ZOOM_MIN, ZOOM_MAX = 0.20, 6.0
STEP_ZOOM = 0.18                    # salto por notch de rueda
ZOOMABLE = ("pista",)               # unicas vistas con camara configurable por rueda
LOOKABLE = ("pista",)               # unicas vistas que se pueden orbitar con el arrastre

# Camara con el raton/touchpad. El touchpad de MacOS no expone gestos de pinza ni rotacion a
# GLFW: lo unico que llega son eventos de scroll, asi que dos dedos en horizontal = orbita y en
# vertical = zoom. El arrastre con boton es el otro camino, y hace falta que la ventana este
# enfocada (GLFW no entrega eventos de boton a una ventana sin foco).
STEP_LOOK = 0.011                   # rad por pixel de arrastre
STEP_LOOK_SCROLL = 0.13             # rad por unidad de scroll horizontal (dos dedos)
LOOK_SMOOTH = 7.0                   # inercia de la orbita (1/s): el gesto no sacuda el encuadre
LOOK_MAX_YAW, LOOK_MAX_TILT = math.pi, 0.45
LOOK_MIN_H = 0.15                   # el ojo no puede quedar por debajo del tablon
PRESS, RELEASE = 1, 0               # glfw.ACTION_PRESS / glfw.ACTION_RELEASE (no estan en el paquete)
TRAIL_T = 1.2                       # segundos de estela (se desvanece, no persigue al rider parado)
TRAIL_HW = 0.02                     # media anchura de la estela (4 cm, como una linea pintada)
ZCOLORS = ["#6b7280", "#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]

MSAA = 4                            # muestras por pixel pedidas a GLFW
A0, SWEEP = 225.0, 270.0            # barrido del dial (igual que gauges.py)

DECK_VATTR = "3f 3f 2f 1f"                # posicion, normal, (s, q), calidez del charco de luz
DECK_FIELDS = ("in_position", "in_normal", "in_uv", "in_warm")

DECK_VS = """#version 330
in vec3 in_position; in vec3 in_normal; in vec2 in_uv; in float in_warm;
out vec3 v_normal; out vec2 v_uv; out float v_warm; out vec3 v_world;
uniform mat4 mvp;
void main(){
    gl_Position = mvp * vec4(in_position, 1.0);
    v_normal = in_normal; v_uv = in_uv; v_warm = in_warm; v_world = in_position;
}"""


def _glsl_vec(col) -> str:
    return "vec3(%.4f, %.4f, %.4f)" % tuple(col)


def _deck_fs() -> str:
    """Fragment shader de la cubierta, con las constantes de Python incrustadas (una sola
    fuente para medidas y colores)."""
    paint = "\n".join(
        "    c = mix(c, %s, box(q, %.4f, %.4f, aq));" % (_glsl_vec(globals()[name]), d, w / 2.0)
        for d, name, w in PAINT)
    return """#version 330
in vec3 v_normal; in vec2 v_uv; in float v_warm; in vec3 v_world;
out vec4 o;
""" + SHADE + """
float hash(float n){ return fract(sin(n * 12.9898 + 78.233) * 43758.5453); }
// Cobertura exacta (filtro de caja de ancho aa) de la banda [c-hw, c+hw]: una linea de
// 5 cm a 60 m ocupa menos de un pixel y se atenua en vez de parpadear.
float box(float x, float c, float hw, float aa){
    return (clamp(x + aa * 0.5, c - hw, c + hw) - clamp(x - aa * 0.5, c - hw, c + hw)) / aa;
}
// Integral de un tren de juntas de fraccion j por periodo: con ella la junta se filtra igual.
float pulseI(float x, float j){ return floor(x) * j + max(0.0, fract(x) - (1.0 - j)); }
float joints(float x, float j, float aa){ return (pulseI(x + aa * 0.5, j) - pulseI(x - aa * 0.5, j)) / aa; }
void main(){
    float s = v_uv.x, q = v_uv.y;
    float aq = max(fwidth(q), 1e-5), as_ = max(fwidth(s), 1e-5);
    float k = floor(q / %(W).4f);
    float sk = s / %(L).4f + hash(k + 3.0);           // testas escalonadas por liston
    // Tono por liston: solo donde el liston se resuelve; de lejos se queda en el tono medio
    // (un hash por pixel a esa distancia es ruido, no madera).
    float detail = clamp(%(W).4f / (aq * 2.5) - 0.4, 0.0, 1.0);
    float tone = (hash(k * 1.731 + floor(sk) * 17.17) - 0.5) * 0.11 * detail;
    vec3 c = %(WOOD)s * (1.0 + tone);
    float jl = joints(q / %(W).4f, %(JW).4f, aq / %(W).4f);         // junta entre listones
    float jt = joints(sk, %(JL).5f, as_ / %(L).4f);               // testa
    c = mix(c, %(WOOD_D)s, clamp(jl + jt, 0.0, 1.0) * 0.85);
""" % {"W": SLAT, "L": SLAT_L, "JW": SLAT_J / SLAT, "JL": SLAT_J / SLAT_L,
       "WOOD": _glsl_vec(WOOD), "WOOD_D": _glsl_vec(WOOD_D)} + paint + """
    c = min(c * v_warm, vec3(1.0));
    o = shade(c, v_normal, 0.0, v_world);
}"""


class DeckMesh:
    """Cubierta de la pista en formato propio (`DECK_VATTR`): la dibuja `DECK_FS`.

    `fmt` le dice al bucle de render que programa usar. Si ese programa no compila,
    `to_mesh()` la convierte al formato general en madera lisa: peor aspecto, pero la
    ventana no muere por un driver quisquilloso.
    """

    __slots__ = ("v",)
    fmt = "deck"

    def __init__(self):
        self.v: list[float] = []

    def vert(self, p, n, s, q, warm) -> None:
        self.v.extend((p[0], p[1], p[2], n[0], n[1], n[2], s, q, warm))

    def to_mesh(self) -> "Mesh":
        m = Mesh()
        for i in range(0, len(self.v), 9):
            x, y, z, nx, ny, nz, _s, _q, w = self.v[i:i + 9]
            m.v.extend((x, y, z, nx, ny, nz, *_warm(WOOD, w), 0.0))
        return m

    @property
    def tris(self) -> int:
        return len(self.v) // 27


def build_deck(pts, warm) -> DeckMesh:
    """Malla de la cubierta: filas cada metro del trazado, columnas cada `DECK_STEP`.

    La subdivision lateral es lo que la mantiene pegada a la superficie en la rampa de
    peralte (un quad de 10 m de ancho se tuerce y su diagonal se separa cm). Las normales
    son por vertice, asi la luz pasa suave de la recta a la curva.
    """
    d = DeckMesh()
    n = len(pts) - 1
    cols = int(round((TRACK_W + COTE) / DECK_STEP))
    qs = [-COTE + (TRACK_W + COTE) * j / cols for j in range(cols + 1)]
    rows = []
    for i, pt in enumerate(pts):
        s = TRACK_LEN * i / n
        row = []
        for q in qs:
            p, nrm, _h = _surf(pt, q)
            row.append((p, nrm, s, q, warm(p)))
        rows.append(row)
    for r0, r1 in zip(rows, rows[1:]):
        for j in range(cols):
            a, b, c, e = r0[j], r0[j + 1], r1[j + 1], r1[j]
            for v in (a, b, c, a, c, e):
                d.vert(*v)
    return d


SHADOW_VS = """#version 330
in vec3 in_position; in vec2 in_rk;
out vec2 v_rk;
uniform mat4 mvp;
void main(){ gl_Position = mvp * vec4(in_position, 1.0); v_rk = in_rk; }"""

# Sombra de contacto suave: se dibuja con mezcla multiplicativa (DST_COLOR, ZERO), asi que
# el color de salida es el factor por el que se oscurece lo de debajo (el tablon del shader).
SHADOW_FS = """#version 330
in vec2 v_rk;
out vec4 o;
void main(){
    float a = 1.0 - smoothstep(0.0, 1.0, v_rk.x);
    o = vec4(vec3(1.0 - v_rk.y * a * a), 1.0);
}"""


class ShadowMesh:
    """Sombras de contacto (`fmt = "shadow"`): elipses en abanico, (posicion, (r, fuerza)).

    El disco negro de antes se leia como un agujero; el degradado radial con mezcla
    multiplicativa oscurece la madera sin taparla. Sin su programa no se dibuja (`to_mesh`
    vacio): sin sombra el rider se ve igual, solo menos apoyado.
    """

    __slots__ = ("v",)
    fmt = "shadow"

    def __init__(self):
        self.v: list[float] = []

    def ellipse(self, c, ax, ay, a, b, k, segs=20) -> None:
        ring = [tuple(c[j] + a * math.cos(t) * ax[j] + b * math.sin(t) * ay[j] for j in range(3))
                for t in (2 * math.pi * i / segs for i in range(segs + 1))]
        for p0, p1 in zip(ring, ring[1:]):
            self.v.extend((*c, 0.0, k, *p0, 1.0, k, *p1, 1.0, k))

    def to_mesh(self) -> "Mesh":
        return Mesh()

    @property
    def tris(self) -> int:
        return len(self.v) // 15


# Formatos de vertice propios: (vertex shader, fragment shader (funcion), VATTR, campos).
FORMATS = {
    "deck": (DECK_VS, lambda: _deck_fs(), DECK_VATTR, DECK_FIELDS),
    "shadow": (SHADOW_VS, lambda: SHADOW_FS, "3f 2f", ("in_position", "in_rk")),
}
_FMT_PROGS: dict = {}
_VA_FMT: dict = {}                        # id(VAO) -> formato, para el estado de mezcla


def _fmt_prog(ctx, fmt):
    """Programa del formato `fmt` para este contexto (compilado una vez por contexto).
    Devuelve None si no compila: el llamador cae a `mesh.to_mesh()`."""
    got = _FMT_PROGS.get((id(ctx), fmt))
    if got is not None and got[0] is ctx:
        return got[1]
    vs, fs, _attr, _fields = FORMATS[fmt]
    try:
        prog = ctx.program(vertex_shader=vs, fragment_shader=fs())
    except Exception as exc:  # noqa: BLE001 - un shader que no compila no tumba la ventana
        _emit({"warn": f"{fmt} sin shader: {type(exc).__name__}: {exc}"[:300]})
        prog = None
    _FMT_PROGS[(id(ctx), fmt)] = (ctx, prog)
    return prog


def _upload(ctx, prog, mesh, np, moderngl):
    """VAO de una capa con el programa y el formato que pide la malla."""
    fmt = getattr(mesh, "fmt", None)
    if fmt in FORMATS:
        fprog = _fmt_prog(ctx, fmt)
        if fprog is not None:
            _vs, _fs, attr, fields = FORMATS[fmt]
            width = sum(int(t[:-1]) for t in attr.split())
            buf = ctx.buffer(np.asarray(mesh.v if len(mesh.v) else [0.0] * width, dtype="f4").tobytes())
            va = ctx.vertex_array(fprog, [(buf, attr, *fields)])
            _VA_FMT[id(va)] = fmt
            return va, fprog
        mesh = mesh.to_mesh()
    buf = ctx.buffer(np.asarray(mesh.v if len(mesh.v) else [0.0] * 10, dtype="f4").tobytes())
    va = ctx.vertex_array(prog, [(buf, VATTR, *FIELDS)])
    _VA_FMT[id(va)] = None
    return va, prog


HUD_EYE = (0.0, -3.0, 0.0)               # camara del HUD: de frente al espacio panel


def hud_cam(aspect):
    """MVP ortografico del HUD: x en [-aspecto, aspecto], y en [-1, 1] (espacio panel)."""
    import numpy as np
    m = np.zeros((4, 4), dtype="f4")
    near, far = 0.1, 10.0
    m[0, 0], m[1, 1] = 1.0 / max(0.25, aspect), 1.0
    m[2, 2], m[2, 3], m[3, 3] = -2.0 / (far - near), -(far + near) / (far - near), 1.0
    return m @ look_at(HUD_EYE, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))


def _set_cam(vas, np, mvp, eye) -> None:
    """La misma camara para todos los programas de esas capas (general, cubierta, sombra)."""
    for pr in {id(va.program): va.program for va in vas}.values():
        pr["mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4").tobytes())
        try:
            # EYE: el brillo y la niebla del pabellon se calculan desde la camara.
            pr["EYE"].write(np.asarray(eye, dtype="f4").tobytes())
        except Exception:  # noqa: BLE001 - program sin ese uniform (sombra)
            pass


def _draw(ctx, vaos, moderngl, np, mvp, eye, hud=None) -> None:
    """Escena 3D y, encima, el HUD (capas `hud*`) si `hud` = (mvp, ojo).

    La escena va en orden; las sombras con mezcla multiplicativa sobre lo ya pintado. Para el
    HUD se borra solo la profundidad (con el color enmascarado, `clear` no toca la imagen):
    asi queda siempre delante sin tapar el fondo, y entre sus piezas sigue habiendo z-buffer.
    """
    scene = [va for n, va in vaos.items() if not n.startswith("hud")]
    _set_cam(scene, np, mvp, eye)
    for va in scene:
        if _VA_FMT.get(id(va)) == "shadow":
            ctx.enable(moderngl.BLEND)
            ctx.blend_func = (moderngl.DST_COLOR, moderngl.ZERO)
            va.render(moderngl.TRIANGLES)
            ctx.disable(moderngl.BLEND)
        else:
            va.render(moderngl.TRIANGLES)
    huds = [va for n, va in vaos.items() if n.startswith("hud")]
    if hud is None or not huds:
        return
    # En moderngl la mascara de color solo llega a GL al enlazar el framebuffer (`use()`), y
    # `use()` repone el viewport completo: en la ventana partida hay que restaurar el del slot.
    fbo, vp, sc = ctx.fbo, ctx.viewport, ctx.scissor
    fbo.color_mask = (False, False, False, False)
    fbo.use()
    ctx.viewport, ctx.scissor = vp, sc
    ctx.clear(depth=1.0)
    fbo.color_mask = (True, True, True, True)
    fbo.use()
    ctx.viewport, ctx.scissor = vp, sc
    _set_cam(huds, np, hud[0], hud[1])
    for va in huds:
        va.render(moderngl.TRIANGLES)


def _hex(col) -> tuple[float, float, float]:
    """Hex '#rrggbb' -> triple 0..1 (falla a gris, nunca levanta)."""
    try:
        return (int(col[1:3], 16) / 255.0, int(col[3:5], 16) / 255.0, int(col[5:7], 16) / 255.0)
    except Exception:  # noqa: BLE001
        return (0.5, 0.5, 0.5)


def _dial(frac: float) -> float:
    """Angulo (rad) del valor fraccionado 0..1 sobre el arco de 270 grados."""
    return math.radians(A0 - SWEEP * max(0.0, min(1.0, frac)))


def _surf(pt, d, lift=0.0):
    """Punto de la superficie a la distancia `d` medida sobre el tablon desde el borde
    interior (negativa en la cote d'azur, hasta -COTE).

    Devuelve (posicion, normal_de_superficie, normal_horizontal). `pts` es la linea de
    medicion (`AXIS_Q`), asi que el desplazamiento horizontal es `(d - AXIS_Q) * cos(bank)`
    y la altura `DECK + (d + COTE) * sin(bank)`: el ancho se mide sobre la pendiente. Con
    `tan(bank) * d` (lo de antes, valido a 14 grados) a 45 grados la pista mediria 9,9 m.

    `lift` levanta unos milimetros la banda a lo largo de la normal: las marcas apoyadas
    sobre la cubierta, en el mismo plano, harian z-fighting.
    """
    x, y, tx, ty, bank = pt
    ln = math.hypot(tx, ty) or 1.0
    nx, ny = ty / ln, -tx / ln
    cb, sb = math.cos(bank), math.sin(bank)
    o = (d - AXIS_Q) * cb - lift * sb
    p = (x + nx * o, y + ny * o, DECK + (d + COTE) * sb + lift * cb)
    return p, (-nx * sb, -ny * sb, cb), (nx, ny)


class Mesh:
    """Triangulos (posicion, normal, color, emisivo) en una lista plana de floats.

    Con `panel(tx, tz)` activado, la geometria se construye en **espacio panel**
    (x = derecha, y = alto, z = hacia el piloto) y se voltea al mundo al emitir el
    vertice: (x, y, z) -> (x+tx, -(z+depth), y+tz). Asi los diales se ven de frente.
    """

    __slots__ = ("v", "t")

    def __init__(self):
        self.v: list[float] = []
        self.t = None                       # (tx, tz, depth) o None = coordenadas de mundo

    def panel(self, tx, tz, depth=0.0) -> None:
        self.t = (tx, tz, depth)

    def _wpoint(self, p, n):
        if self.t is None:
            return p, n
        tx, tz, depth = self.t
        return ((p[0] + tx, -(p[2] + depth), p[1] + tz),
                (n[0], -n[2], n[1]))

    def tri(self, a, b, c, col, n=None, light=0.0) -> None:
        if n is None:
            ux, uy, uz = (b[i] - a[i] for i in range(3))
            vx, vy, vz = (c[i] - a[i] for i in range(3))
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            n = (nx / ln, ny / ln, nz / ln)
        pa, na = self._wpoint(a, n)
        pb, _ = self._wpoint(b, n)
        pc, _ = self._wpoint(c, n)
        for p in (pa, pb, pc):
            self.v.extend((p[0], p[1], p[2], na[0], na[1], na[2],
                           col[0], col[1], col[2], light))

    def quad(self, a, b, c, d, col, n=None, light=0.0) -> None:
        if n is None:
            ux, uy, uz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            vx, vy, vz = (d[0] - a[0], d[1] - a[1], d[2] - a[2])
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            n = (nx / ln, ny / ln, nz / ln)
        self.tri(a, b, c, col, n, light)
        self.tri(a, c, d, col, n, light)

    def box(self, cx, cy, cz, sx, sy, sz, col, yaw=0.0, light=0.0) -> None:
        """Caja de 6 caras centrada en (cx,cy,cz), girada `yaw` rad alrededor de Z."""
        hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
        co, si = (math.cos(yaw), math.sin(yaw)) if yaw else (1.0, 0.0)

        def tp(x, y, z):
            return (cx + x * co - y * si, cy + x * si + y * co, cz + z)

        # (normal, 4 esquinas en orden de quad) por cara
        faces = (
            ((1, 0, 0), ((hx, -hy, -hz), (hx, -hy, hz), (hx, hy, hz), (hx, hy, -hz))),
            ((-1, 0, 0), ((-hx, hy, -hz), (-hx, hy, hz), (-hx, -hy, hz), (-hx, -hy, -hz))),
            ((0, 1, 0), ((-hx, hy, -hz), (hx, hy, -hz), (hx, hy, hz), (-hx, hy, hz))),
            ((0, -1, 0), ((hx, -hy, -hz), (-hx, -hy, -hz), (-hx, -hy, hz), (hx, -hy, hz))),
            ((0, 0, 1), ((-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz))),
            ((0, 0, -1), ((-hx, hy, -hz), (hx, hy, -hz), (hx, -hy, -hz), (-hx, -hy, -hz))),
        )
        for n, cs in faces:
            nx, ny = n[0] * co - n[1] * si, n[0] * si + n[1] * co
            self.quad(*(tp(*c) for c in cs), col, (nx, ny, n[2]), light)

    def disc(self, cx, cy, cz, r, col, segs=36, light=0.0) -> None:
        """Disco plano (cara del dial) en espacio panel, normal hacia el piloto."""
        prev = None
        for i in range(segs + 1):
            a = 2 * math.pi * i / segs
            p = (cx + r * math.cos(a), cy + r * math.sin(a), cz)
            if prev:
                self.tri((cx, cy, cz), prev, p, col, (0.0, 0.0, 1.0), light)
            prev = p

    def ring(self, cx, cy, cz, r0, r1, col, segs=36, light=0.0) -> None:
        """Anillo plano entre r0 y r1 (bisel/aro iluminado)."""
        prev = None
        for i in range(segs + 1):
            a = 2 * math.pi * i / segs
            ca, sa = math.cos(a), math.sin(a)
            p = ((cx + r0 * ca, cy + r0 * sa, cz), (cx + r1 * ca, cy + r1 * sa, cz))
            if prev:
                self.quad(prev[0], prev[1], p[1], p[0], col, (0.0, 0.0, 1.0), light)
            prev = p

    def panel_backplate(self, x0, x1, z0, z1, curve, y0, frame=True) -> None:
        """Capota curva detras del cluster (en coordenadas de mundo, XZ)."""
        k = 20
        prev = None
        span = max(0.001, max(abs(x0), abs(x1)))
        for i in range(k + 1):
            x = x0 + (x1 - x0) * i / k
            y = y0 + curve * (x / span) ** 2
            if prev is not None:
                self.quad((prev[0], prev[1], z0), (x, y, z0), (x, y, z1),
                          (prev[0], prev[1], z1), (0.075, 0.085, 0.105),
                          (0.0, -1.0, 0.02))
            prev = (x, y)
        if frame:
            steel = (0.26, 0.29, 0.34)
            self.box(0, y0 + curve, z1 + 0.24, x1 - x0 + 0.9, 0.12, 0.28, steel)
            self.box(0, y0 + curve * 0.6, z0 - 0.24, x1 - x0 + 0.9, 0.12, 0.28, steel)
            for sx in (x0 - 0.40, x1 + 0.40):
                self.box(sx, y0 + curve, (z0 + z1) / 2, 0.16, 0.12,
                         z1 - z0 + 0.6, steel)

    def ribbon(self, pts, half, col, off=0.0, light=0.0, tint=None, lift=0.0) -> None:
        """Cinta continua sobre el trazado `pts` (x, y, tx, ty, bank por punto).

        El peralte es por punto (en un 400 m real solo las curvas estan inclinadas) y la
        altura de cada banda es `DECK + tan(bank) * (q + HALF_W)` con `q` la coordenada
        lateral medida desde el borde interior, que queda a 0. `off` desplaza la cinta a lo
        ancho (lineas pintadas y tablones sobre el madera). `tint`, si se da, es una funcion
        que recibe el centro del cuadro y devuelve el multiplicador de calidez: es el
        charco de luz bajo cada lampara, horneado por vertice porque el programa no tiene
        luces puntuales (coste solo de construccion, la capa se sube una vez).
        """
        # `off` esta centrado en el eje (-HALF_W..HALF_W) y `_surf` mide desde el borde
        # interior (0..TRACK_W): sin el `+ HALF_W` toda la madera salia 5 m hacia dentro, se
        # hundia bajo el apron en las curvas y dejaba un hueco negro antes del muro.
        prev = None
        for pt in pts:
            a_in, nrm, _ = _surf(pt, off - half + HALF_W, lift)
            a_out, _, _ = _surf(pt, off + half + HALF_W, lift)
            if prev:
                if tint is None:
                    self.quad(prev[0], prev[1], a_out, a_in, col, nrm, light)
                else:
                    cs = (_warm(col, tint(prev[0])), _warm(col, tint(prev[1])),
                          _warm(col, tint(a_out)), _warm(col, tint(a_in)))
                    self.vquad(prev[0], prev[1], a_out, a_in, cs, nrm, light)
            prev = (a_in, a_out)

    def floor(self, size=3.2, step=0.4, col=GRID, z0=0.0, lw=0.012) -> None:
        """Piso con grilla: un platano oscuro + lineas finas, apoyado en z0.

        Con `step=0` la grilla se omite (el piso del velodromo es liso, las lineas de
        5 m de la escena de historial no pintan nada a esa escala).
        """
        self.box(0, 0, z0 - 0.04, 2 * size, 2 * size, 0.06, FLOOR)
        if not step:
            return
        k = max(1, int(2 * size / step))
        for i in range(k + 1):
            p = -size + i * (2 * size / k)
            self.box(p, 0, z0 - 0.005, lw, 2 * size, lw, col)
            self.box(0, p, z0 - 0.005, 2 * size, lw, lw, col)

    def merge(self, other: "Mesh") -> None:
        """Anexa otra malla (ya transformada): un solo VBO para todo el cluster."""
        self.v.extend(other.v)

    def segment(self, a, b, r, col, light=0.0, sides=4) -> None:
        """Prisma entre dos puntos: un miembro (muslo, espinilla, brazo) o un tubo del
        cuadro. La seccion es un rombo (4 caras), suficiente a esta escala.

        Con `sides=6` la seccion se lee redondeada desde la camara: el rombo visto de canto
        sale como una tarjeta plana, que es lo que delataba al atleta.
        """
        d = [b[i] - a[i] for i in range(3)]
        ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
        d = [d[0] / ln, d[1] / ln, d[2] / ln]
        ref = (0.0, 0.0, 1.0) if abs(d[2]) < 0.9 else (0.0, 1.0, 0.0)
        u = [d[1] * ref[2] - d[2] * ref[1], d[2] * ref[0] - d[0] * ref[2], d[0] * ref[1] - d[1] * ref[0]]
        ul = math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]) or 1.0
        u = [u[0] / ul, u[1] / ul, u[2] / ul]
        v = [d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0]]
        ring = []
        for k in range(sides):
            ca, sa = math.cos(2 * math.pi * k / sides), math.sin(2 * math.pi * k / sides)
            ring.append([u[i] * ca + v[i] * sa for i in range(3)])
        for i in range(sides):
            s1, s2 = ring[i], ring[(i + 1) % sides]
            a1 = tuple(a[j] + r * s1[j] for j in range(3))
            a2 = tuple(a[j] + r * s2[j] for j in range(3))
            b1 = tuple(b[j] + r * s1[j] for j in range(3))
            b2 = tuple(b[j] + r * s2[j] for j in range(3))
            n = [(s1[j] + s2[j]) / 2.0 for j in range(3)]
            self.quad(a1, b1, b2, a2, col, n, light)

    def vtri(self, a, b, c, cols, n, light=0.0) -> None:
        """Triangulo con color por vertice.

        Hace falta para el charco de luz: con un color por cuadro, la gradacion sale como
        "ladrillos" (una linea cada metro de cuerda) en vez de como un degradado suave sobre
        el tablon. Con color por vertice el interpolador lo resuelve solo.
        """
        for p, col in zip((a, b, c), cols):
            pw, _ = self._wpoint(p, n)
            self.v.extend((pw[0], pw[1], pw[2], n[0], n[1], n[2], col[0], col[1], col[2], light))

    def vquad(self, a, b, c, d, cols, n, light=0.0) -> None:
        """Quad con color por vertice (verts en el mismo orden que `quad`)."""
        self.vtri(a, b, c, (cols[0], cols[1], cols[2]), n, light)
        self.vtri(a, c, d, (cols[0], cols[2], cols[3]), n, light)

    def plate(self, c, r, n, col, segs=18, light=0.0) -> None:
        """Disco plano en coordenadas de mundo: sombra de contacto y plato de la bici.

        La sombra es un parche oscuro apoyado en la superficie peraltada; sin ella la bici
        flota y la camara no tiene anclaje visual (es lo que hace que un render "de baja").
        """
        ref = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
        u = [n[1] * ref[2] - n[2] * ref[1], n[2] * ref[0] - n[0] * ref[2], n[0] * ref[1] - n[1] * ref[0]]
        ul = math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]) or 1.0
        u = [u[0] / ul, u[1] / ul, u[2] / ul]
        v = [n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0]]
        for i in range(segs):
            a0, a1 = 2 * math.pi * i / segs, 2 * math.pi * (i + 1) / segs

            def pt(a):
                return (c[0] + r * (u[0] * math.cos(a) + v[0] * math.sin(a)),
                        c[1] + r * (u[1] * math.cos(a) + v[1] * math.sin(a)),
                        c[2] + r * (u[2] * math.cos(a) + v[2] * math.sin(a)))
            self.tri(c, pt(a0), pt(a1), col, tuple(n), light)

    @property
    def tris(self) -> int:
        return len(self.v) // 30


# ---------------------------------------------------------------- matrices
def perspective(fovy, aspect, near=0.1, far=80.0):
    import numpy as np
    f = 1.0 / math.tan(fovy / 2.0)
    m = np.zeros((4, 4), dtype="f4")
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def look_at(eye, center, up=(0.0, 0.0, 1.0)):
    import numpy as np
    e = np.asarray(eye, dtype="f8")
    u = np.asarray(up, dtype="f8")
    fz = e - np.asarray(center, dtype="f8")
    fz /= (np.linalg.norm(fz) or 1.0)
    rx = np.cross(u, fz)
    rx /= (np.linalg.norm(rx) or 1.0)
    ry = np.cross(fz, rx)
    m = np.eye(4, dtype="f4")
    m[0, :3], m[1, :3], m[2, :3] = rx, ry, fz
    m[0, 3], m[1, 3], m[2, 3] = -(rx @ e), -(ry @ e), -(fz @ e)
    return m


def _rot(v, axis, a):
    """Rotacion de un vector alrededor de un eje (Rodrigues).

    La orbita se hace girando el rig completo (ojo + mira) alrededor del rider, no solo la mira:
    girando solo la mirada, en cuanto el yaw pasa de ~80 grados el rider sale del cuadro y la
    camara se pone a mirar la grada. Girando el rig se conserva el encuadre.
    """
    c, s = math.cos(a), math.sin(a)
    d = axis[0] * v[0] + axis[1] * v[1] + axis[2] * v[2]
    cr = (axis[1] * v[2] - axis[2] * v[1], axis[2] * v[0] - axis[0] * v[2],
          axis[0] * v[1] - axis[1] * v[0])
    return tuple(v[i] * c + cr[i] * s + axis[i] * d * (1.0 - c) for i in range(3))


def orbit(t, radius, height, speed=0.25, phase=0.0, center=(0.0, 0.0, 0.5), aspect=1.6):
    """Camara orbital lenta, retrocediendo si el panel es mas ancho que alto."""
    a = t * speed + phase
    d = radius * max(1.0, 1.55 / max(0.4, aspect))
    eye = (math.sin(a) * d, -math.cos(a) * d, height * max(1.0, 1.55 / max(0.4, aspect)))
    return eye, center, (0.0, 0.0, 1.0), 0.85


# ---------------------------------------------------------------- escenas
def scene_hr(snap, anim, aspect=1.6):
    """Barras 3D del historial de FC + barra destacada de la FC actual."""
    m = Mesh()
    m.floor()
    zs = snap.get("zcolors") or ZCOLORS
    hrmax = max(60.0, float(snap.get("hrmax") or 190))
    samples = (snap.get("samples") or [])[-70:]
    if samples:
        x0, x1 = -2.6, 2.2
        w = (x1 - x0) / len(samples)
        for i, (hr, _watts, zidx) in enumerate(samples):
            h = max(0.05, (hr - 40.0) / max(1.0, hrmax - 40.0) * 2.1)
            m.box(x0 + (i + 0.5) * w, 0.0, h / 2.0, w * 0.7, 0.18, h,
                  _hex(zs[min(max(int(zidx), 0), 5)]))
    hr = float(snap.get("hr") or 0)
    if hr:
        h = max(0.08, (hr - 40.0) / max(1.0, hrmax - 40.0) * 2.1)
        m.box(2.75, 0.0, h / 2.0, 0.24, 0.7, h, _hex(snap.get("zone_color") or "#ef4444"))
    return [("frame", m)], orbit(anim["t"], 5.6, 2.9, 0.18, center=(0.0, 0.0, 0.8), aspect=aspect)


def scene_watts(snap, anim, aspect=1.6):
    """Cajas 3D por banda de %FTP (altura = kJ) + caja del wattaje actual al frente."""
    m = Mesh()
    m.floor()
    ws = snap.get("wcolors") or ZCOLORS
    kjs = [float(x) for x in (snap.get("pzone_kj") or [0.0] * 6)]
    maxv = max(kjs + [1e-6])
    x0, x1 = -2.3, 2.3
    bw = (x1 - x0) / max(1, len(kjs))
    for i, kj in enumerate(kjs):
        h = max(0.05, kj / maxv * 2.2)
        m.box(x0 + (i + 0.5) * bw, 0.0, h / 2.0, bw * 0.66, bw * 0.66, h, _hex(ws[min(i, 5)]))
    w_now = float(snap.get("watts") or 0)
    ftp = max(50.0, float(snap.get("ftp") or 240))
    if w_now:
        h = max(0.08, w_now / (ftp * 1.6) * 3.0)
        m.box(0.0, -2.7, h / 2.0, 0.55, 0.55, h, _hex(snap.get("wzone_color") or "#facc15"))
    return [("frame", m)], orbit(anim["t"], 5.9, 3.2, 0.24, phase=0.6,
                                 center=(0.0, 0.0, 0.7), aspect=aspect)


def scene_gauge(snap, anim, aspect=1.6):
    """Cluster completo: los 4 diales + tira digital, llenando la ventana."""
    from .cluster import CLUSTER                  # lazy: cluster importa de aqui
    return CLUSTER.build(snap, anim, aspect)


def _kappa(s):
    """Curvatura en `s` metros desde meta: meta al final de la recta de meta, luego
    clotoide (curvatura lineal 0 -> 1/R), arco, clotoide de salida, recta, y otra curva."""
    R = (TRANS_L + ARC_L) / math.pi
    s = s % TRACK_LEN
    for _ in range(2):
        if s < TRANS_L:
            return s / TRANS_L / R
        s -= TRANS_L
        if s < ARC_L:
            return 1.0 / R
        s -= ARC_L
        if s < TRANS_L:
            return (1.0 - s / TRANS_L) / R
        s -= TRANS_L
        if s < STRAIGHT_L:
            return 0.0
        s -= STRAIGHT_L
    return 0.0


def track(step=TRACK_STEP):
    """Trazado de 250 m sobre la linea de medicion, sentido antihorario (el de carrera).

    Se integra la curvatura (`_kappa`) por punto medio con subpasos de 2,5 cm, asi la
    clotoide sale exacta y la fraccion de vuelta es distancia real. El peralte va ligado a
    la curvatura (12 grados en recta, 45 en el arco): la propia clotoide hace la transicion
    suave, sin rampa aparte. El error de cierre (micras) se reparte a lo largo de la vuelta.
    Cada punto: (x, y, tx, ty, bank). El ultimo punto repite el primero para cerrar.
    """
    R = (TRANS_L + ARC_L) / math.pi
    n = int(round(TRACK_LEN / step))
    ds = TRACK_LEN / n
    sub = max(1, int(round(ds / 0.025)))
    h = ds / sub
    x = y = th = 0.0
    raw = []
    for i in range(n):
        s0 = i * ds
        raw.append((x, y, th, _kappa(s0)))
        for j in range(sub):
            km = _kappa(s0 + (j + 0.5) * h)
            thm = th + km * h * 0.5
            x += math.cos(thm) * h
            y += math.sin(thm) * h
            th += km * h
    ex, ey = x, y
    xs = [r[0] - ex * i / n for i, r in enumerate(raw)]
    ys = [r[1] - ey * i / n for i, r in enumerate(raw)]
    cx, cy = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
    pts = [(xs[i] - cx, ys[i] - cy, math.cos(r[2]), math.sin(r[2]),
            BANK_S + (BANK_C - BANK_S) * min(1.0, r[3] * R)) for i, r in enumerate(raw)]
    pts.append(pts[0])                                    # cierra el trazado
    return pts


def _warm(col, t):
    """Color multiplicado por la calidez del charco de luz (recortado a 1)."""
    return tuple(min(1.0, v * t) for v in col)


def _lamps(pts):
    """Lamparas colgadas del cercho: posiciones (x, y, z), una cada 6 m de trazado."""
    k = max(1, int(round(6.0 / TRACK_STEP)))
    return [(_surf(pt, HALF_W)[0][0], _surf(pt, HALF_W)[0][1], LAMP_H)
            for pt in pts[:-1][::k]]


def _pool(lamps):
    """Calidez por vertice (1,0 en la penumbra, hasta ~1,3 bajo la lampara) + veta del madera.

    El programa no tiene luces puntuales, asi que el charco de luz se hornea al construir
    la capa estatica: no cuesta nada por frame y es lo que da el aspecto de pabellon. La
    veta es una variacion suave de tono por tablon, para que la banda no se lea como rayas
    de pintura.
    """
    def f(p):
        best = 0.0
        for lp in lamps:
            d2 = (p[0] - lp[0]) ** 2 + (p[1] - lp[1]) ** 2 + (p[2] - lp[2]) ** 2
            v = 120.0 / (120.0 + d2)
            if v > best:
                best = v
        return 1.0 + 0.26 * best
    return f


def build_static(pts):
    """Lo que no cambia nunca: tablon de pino, lineas UCI, meta, muro, gradas, techo y lamparas.

    La escena se construye una sola vez y se conserva como capa cacheada; por frame solo
    cambian el rider, la estela y la camara. Todo lo que da el aspecto de velodromo (madera
    ambarina, el muro que eleva el tablon, las filas de butacas, los cerchos) es estatico,
    asi que se puede pagar una vez y no entra en el presupuesto de frame.
    """
    m = Mesh()
    n = len(pts) - 1
    lamps = _lamps(pts)
    warm = _pool(lamps)

    # Piso del recinto: liso, sin grilla (a esta escala las lineas de 5 m no pintan).
    m.floor(size=110, step=0.0, col=INFIELD, z0=-0.05)

    # El tablon y las lineas pintadas a lo largo van en la capa `deck` (`build_deck`), con
    # su propio shader; aqui solo queda lo que no es cubierta.

    # Zona de seguridad (apron): hormigon pintado y llano al pie de la cote d'azur. No sigue
    # el peralte, por eso no se puede usar ribbon. Su borde interior baja al piso con un muro.
    for i in range(n):
        p0, _n0, h0 = _surf(pts[i], -COTE)
        p1, _n1, h1 = _surf(pts[i + 1], -COTE)
        a0 = (p0[0] - h0[0] * APRON_W, p0[1] - h0[1] * APRON_W, DECK)
        a1 = (p1[0] - h1[0] * APRON_W, p1[1] - h1[1] * APRON_W, DECK)
        cs = (_warm(APRON, warm(a0)), _warm(APRON, warm(a1)), _warm(APRON, warm(p1)), _warm(APRON, warm(p0)))
        m.vquad(a0, a1, p1, p0, cs, (0.0, 0.0, 1.0))
        g0, g1 = (a0[0], a0[1], 0.0), (a1[0], a1[1], 0.0)
        m.quad(g0, g1, a1, a0, _warm(WALL, warm(a0)), (-h0[0], -h0[1], 0.0))

    # Marcas transversales UCI. La meta no es un damero: es una banda blanca con una linea
    # negra fina en el centro, en el punto que cierra la vuelta. La linea de 200 m (blanca)
    # marca el tramo lanzado, y las de persecucion (rojas, medio ancho desde el borde
    # interior) estan a mitad de cada recta.
    _cross(m, pts, 0.0, META_W, META, 0.0, TRACK_W, warm, 0.012)
    _cross(m, pts, 0.0, LINE_W, LINE_BLACK, 0.0, TRACK_W, warm, 0.014)
    _cross(m, pts, TRACK_LEN - 200.0, LINE_W, META, 0.0, TRACK_W, warm, 0.012)
    for s_mid in _straight_mids(pts):
        _cross(m, pts, s_mid, LINE_W, LINE_RED, 0.0, TRACK_W / 2.0, warm, 0.012)

    _structure(m, pts, warm)
    _infield(m, pts, warm)
    _stands(m, pts, warm)
    _roof(m, pts, lamps, warm)
    return m


def _cross(m, pts, s, w, col, d0, d1, warm, lift) -> None:
    """Banda transversal de ancho `w` (metros de avance) centrada en `s` metros desde meta,
    de `d0` a `d1` medidos desde el borde interior. Sigue el peralte del punto, asi que en
    curva tambien queda apoyada en el tablon."""
    f0 = ((s - w / 2.0) / TRACK_LEN) % 1.0
    f1 = ((s + w / 2.0) / TRACK_LEN) % 1.0
    p0, p1 = _at(pts, f0), _at(pts, f1)
    a0, nrm, _ = _surf(p0, d0, lift)
    a1, _, _ = _surf(p0, d1, lift)
    b0, _, _ = _surf(p1, d0, lift)
    b1, _, _ = _surf(p1, d1, lift)
    m.vquad(a0, a1, b1, b0, tuple(_warm(col, warm(q)) for q in (a0, a1, b1, b0)), nrm, 0.22)


def _straight_mids(pts):
    """Metros desde meta del centro de cada recta (tramos con tangente constante)."""
    n = len(pts) - 1
    runs, start = [], None
    for i in range(n + 1):
        straight = i < n and abs(_kappa(i * TRACK_LEN / n)) < 1e-9 \
            and abs(_kappa((i + 1) * TRACK_LEN / n)) < 1e-9
        if straight and start is None:
            start = i
        elif not straight and start is not None:
            runs.append((start + i) / 2.0 * TRACK_LEN / n)
            start = None
    return runs


def _structure(m, pts, warm) -> None:
    """Barandilla con paneles publicitarios y fronton (con lonas) hasta la primera fila.

    En el arco la barandilla llega casi a la grada; en las rectas (12 grados) el borde
    exterior queda ~5 m mas bajo y lo que se ve detras es el muro que sostiene las gradas.
    Los paneles son de color liso con un "logo" claro: sin texto, pero se leen como
    publicidad y dan escala y ritmo a la vuelta (sin ellos el muro es una banda continua).
    """
    n = len(pts) - 1
    per = max(1, int(round(AD_L / TRACK_STEP)))             # muestras por panel
    for i in range(n):
        t0, _tn, ht0 = _surf(pts[i], TRACK_W)
        t1, _, ht1 = _surf(pts[i + 1], TRACK_W)
        s0 = (t0[0] + ht0[0] * 0.30, t0[1] + ht0[1] * 0.30, t0[2] + WALL_H)
        s1 = (t1[0] + ht1[0] * 0.30, t1[1] + ht1[1] * 0.30, t1[2] + WALL_H)
        inward = (-ht0[0], -ht0[1], 0.0)
        panel = i // per
        bg, fg = AD_COLS[(panel * 7) % len(AD_COLS)]
        m.quad(t0, t1, s1, s0, _warm(bg, warm(t0)), inward, 0.10)
        k = (i % per) / per
        if 0.18 <= k < 0.82:                                # logo: 64 % del panel, centrado
            lo, hi = 0.30, 0.72
            q = [(p[0] - ht0[0] * 0.012 + (r[0] - p[0]) * f, p[1] - ht0[1] * 0.012 + (r[1] - p[1]) * f,
                  p[2] + (r[2] - p[2]) * f)
                 for p, r, f in ((t0, s0, lo), (t1, s1, lo), (t1, s1, hi), (t0, s0, hi))]
            m.quad(*q, _warm(fg, warm(t0)), inward, 0.18)
        f0 = (s0[0] + ht0[0] * 0.3, s0[1] + ht0[1] * 0.3, s0[2])
        f1 = (s1[0] + ht1[0] * 0.3, s1[1] + ht1[1] * 0.3, s1[2])
        if STAND_Z - min(f0[2], f1[2]) > 0.05:
            m.quad(f0, f1, (f1[0], f1[1], STAND_Z), (f0[0], f0[1], STAND_Z),
                   _warm(SEAT_F, warm(f0)), inward)
            # Lona en el fronton de las rectas: banda horizontal bajo la primera fila.
            z0, z1 = max(f0[2], f1[2]) + 0.6, STAND_Z - 0.5
            if z1 - z0 > 1.0:
                bg2, _fg2 = AD_COLS[((i // (2 * per)) * 3 + 1) % len(AD_COLS)]
                g0 = (f0[0] - ht0[0] * 0.02, f0[1] - ht0[1] * 0.02)
                g1 = (f1[0] - ht1[0] * 0.02, f1[1] - ht1[1] * 0.02)
                m.quad((g0[0], g0[1], z0), (g1[0], g1[1], z0), (g1[0], g1[1], z1), (g0[0], g0[1], z1),
                       _warm(bg2, warm(f0)), inward, 0.12)


def _infield(m, pts, warm) -> None:
    """Zona interior: suelo deportivo, pista de calentamiento marcada y boxes de equipos.

    Antes era un vacio oscuro bajo la cote d'azur. En un velodromo real el interior tiene
    los boxes de los equipos (biombo, banco y rodillo) a lo largo de la recta opuesta.
    """
    n = len(pts) - 1
    ring = []
    for i in range(n):
        p, _n, h = _surf(pts[i], -COTE)
        ring.append((p[0] - h[0] * APRON_W, p[1] - h[1] * APRON_W, 0.01))
    for i in range(n):                                       # abanico desde el centro: es convexo
        a, b = ring[i], ring[(i + 1) % n]
        m.vtri((0.0, 0.0, 0.01), a, b, tuple(_warm(INFIELD_FLOOR, warm(q)) for q in ((0, 0, 0), a, b)),
               (0.0, 0.0, 1.0))
    # Lineas de una pista polideportiva centrada (28 x 15 m).
    W, H, lw = 14.0, 7.5, 0.06
    for x0, y0, x1, y1 in ((-W, -H, W, -H), (-W, H, W, H), (-W, -H, -W, H), (W, -H, W, H), (0, -H, 0, H)):
        if y0 == y1:
            m.quad((x0, y0 - lw, 0.02), (x1, y1 - lw, 0.02), (x1, y1 + lw, 0.02), (x0, y0 + lw, 0.02),
                   COURT_LINE, (0.0, 0.0, 1.0), 0.2)
        else:
            m.quad((x0 - lw, y0, 0.02), (x0 + lw, y0, 0.02), (x1 + lw, y1, 0.02), (x1 - lw, y1, 0.02),
                   COURT_LINE, (0.0, 0.0, 1.0), 0.2)
    # Boxes de equipos a lo largo de la recta opuesta (s ~ 92..120 m), mirando a la pista.
    for k in range(PITS):
        sm = 106.0 + (k - (PITS - 1) / 2.0) * 3.4
        pt = _at(pts, sm / TRACK_LEN)
        p, _n, h = _surf(pt, -COTE)
        tx, ty = pt[2], pt[3]
        c = (p[0] - h[0] * (APRON_W + 3.2), p[1] - h[1] * (APRON_W + 3.2))
        yaw = math.atan2(ty, tx)
        col = AD_COLS[(k * 5 + 2) % len(AD_COLS)][0]
        w = _warm(col, warm((c[0], c[1], 0.0)))
        m.box(c[0], c[1], 0.02, 3.0, 2.4, 0.02, _warm(col, 0.55), yaw)                   # alfombra
        bx, by = c[0] - h[0] * 1.2, c[1] - h[1] * 1.2                                    # biombo
        m.box(bx, by, 0.6, 3.0, 0.08, 1.2, w, yaw, 0.08)
        sx, sy = c[0] - h[0] * 0.7, c[1] - h[1] * 0.7                                    # banco
        m.box(sx, sy, 0.22, 1.6, 0.40, 0.44, STEEL, yaw, 0.05)
        m.box(c[0] + h[0] * 0.3, c[1] + h[1] * 0.3, 0.12, 1.2, 0.5, 0.24, SHOE, yaw, 0.05)  # rodillo


def _stands(m, pts, warm) -> None:
    """Gradas: filas escalonadas alrededor del borde exterior, con butacas cada 2 m de cuerda."""
    n = len(pts) - 1

    def row(pt, r):
        p, _n, h = _surf(pt, TRACK_W)
        k = 0.6 + r * STAND_RUN
        return (p[0] + h[0] * k, p[1] + h[1] * k, STAND_Z + r * STAND_RISE)

    for i in range(n):
        for r in range(STAND_ROWS):
            a, b = row(pts[i], r), row(pts[i + 1], r)
            c, d = row(pts[i + 1], r + 1), row(pts[i], r + 1)
            m.quad(a, b, c, d, _warm(SEAT_F, warm(a)), (0.0, 0.0, 1.0))
    every = max(1, int(round(2.0 / TRACK_STEP)))        # butaca cada 2 m de trazado
    for i in range(0, n, every):
        block = int(i * TRACK_STEP) // 12
        if block % 5 == 3:
            continue                                        # pasillo entre bloques
        for r in range(STAND_ROWS):
            p = row(pts[i], r)
            h = _surf(pts[i], TRACK_W)[2]
            yaw = math.atan2(h[1], h[0])
            col = SEAT if block % 3 else SEATS[(r + block // 3) % len(SEATS)]
            m.box(p[0], p[1], p[2] + 0.24, 0.44, 0.48, 0.46, _warm(col, warm(p)), yaw, 0.04)


def _roof(m, pts, lamps, warm) -> None:
    """Cerchos de madera laminada con las lamparas calidas colgadas.

    El techo es lo que cierra la escena desde primera persona: sin el, el cuadro se llena de
    vacio y la luz del shader no tiene de donde venir visualmente.
    """
    idx = list(range(0, len(pts) - 1, max(1, int(round(5.0 / TRACK_STEP)))))
    eave = STAND_Z + STAND_ROWS * STAND_RISE + 2.5         # alero por encima de la ultima fila

    def arch(i):
        p, _n, h = _surf(pts[i], HALF_W)
        a = (p[0] - h[0] * (HALF_W + 14.0), p[1] - h[1] * (HALF_W + 14.0), eave)
        b = (p[0], p[1], ROOF_H)
        k = HALF_W + STAND_ROWS * STAND_RUN + 5.0
        c = (p[0] + h[0] * k, p[1] + h[1] * k, eave)
        return a, b, c

    ribs = [arch(i) for i in idx]
    for j, (a, b, c) in enumerate(ribs):
        m.segment(a, b, 0.24, _warm(RIB, warm(b)), 0.06, sides=4)
        m.segment(b, c, 0.24, _warm(RIB, warm(b)), 0.06, sides=4)
        if j + 1 < len(ribs):
            a1, b1, c1 = ribs[j + 1]
            m.quad(a, a1, b1, b, _warm(ROOF, warm(b)), (0.0, 0.0, 1.0))
            m.quad(b, b1, c1, c, _warm(ROOF, warm(b)), (0.0, 0.0, 1.0))
    for lp in lamps:
        m.box(lp[0], lp[1], lp[2], 0.34, 0.34, 0.14, LAMP, 0.0, 1.0)
        top = (lp[0], lp[1], eave + (ROOF_H - eave) * 0.4)
        m.segment((lp[0], lp[1], lp[2] + 0.07), top, 0.02, STEEL, 0.1, sides=4)


SKIN = (0.80, 0.62, 0.50)
SUIT = (0.085, 0.095, 0.120)              # mono de pista (culote y costados)
SOCK = (0.90, 0.91, 0.93)
SHOE = (0.07, 0.07, 0.08)
GLOVE = (0.06, 0.06, 0.07)
HELMET = (0.90, 0.92, 0.95)
VISOR = (0.05, 0.07, 0.10)
FRAME = (0.13, 0.14, 0.17)                # carbono
CARBON = (0.09, 0.09, 0.11)               # lenticular y perfil de la rueda delantera
TIRE = (0.05, 0.05, 0.06)
METAL = (0.55, 0.57, 0.60)                # plato, bielas, bujes
LOGO = (0.82, 0.84, 0.88)                 # marca en las ruedas: sin ella no se lee que giran

# Geometria en el plano sagital (s = avance, h = altura) en metros, con el pedalier en s = 0.
# Atleta de ~1,75 m en bici de pista en posicion de persecucion (acoples aero).
WR, BB_H = 0.335, 0.28                    # rueda 700c con tubular, altura del pedalier
REAR, FRONT = (-0.39, WR), (0.585, WR)    # vainas cortas: batalla ~0,98 m
SEAT_A = math.radians(74.0)               # angulo del tubo de sillin
SEAT_DIR = (-math.cos(SEAT_A), math.sin(SEAT_A))
CLUSTER = (SEAT_DIR[0] * 0.48, BB_H + SEAT_DIR[1] * 0.48)     # union tubo superior / sillin
SADDLE = (SEAT_DIR[0] * 0.72, BB_H + SEAT_DIR[1] * 0.72)      # altura de sillin 0,72 m
HT_TOP, HT_BOT = (0.40, 0.80), (0.445, 0.64)                  # tubo de direccion
PAD = (0.40, 0.845)                       # apoyabrazos de los acoples
HIP = (-0.15, 1.02)                       # articulacion de la cadera (sobre el sillin)
SHO = (0.35, 1.15)                        # hombro: torso de 0,52 m a ~15 grados
ELBOW = (0.40, 0.875)                     # codo sobre el apoyabrazos
HAND = (0.64, 0.885)                      # manos en el extremo de los acoples
HEAD = (0.50, 1.235)                      # centro de la cabeza (la camara POV va aqui)
CRANK, THIGH, SHIN = 0.165, 0.44, 0.42
ANKLE = (-0.06, 0.10)                     # tobillo respecto al eje del pedal (pie plano)
HIP_W, PEDAL_W, SHO_W, ELB_W = 0.09, 0.11, 0.165, 0.085       # separaciones laterales
BODY_L = 0.12                             # emisivo leve: el cuerpo no se hunde en negro de canto


def _knee(H, F, L1, L2, bend=1.0):
    """Rodilla o codo por IK de dos tramos en el plano sagital (s, h).

    `bend` decide hacia donde dobla: la rodilla hacia fuera de la linea (1) y el codo hacia
    abajo (-1). Si la distancia H->F supera la suma de los tramos, el miembro sale casi
    recto, que es la postura real agarrado al manillar.
    """
    ds, dh = F[0] - H[0], F[1] - H[1]
    D = math.hypot(ds, dh) or 1e-6
    D = min(D, L1 + L2 - 1e-4)
    ca = max(-1.0, min(1.0, (L1 * L1 + D * D - L2 * L2) / (2 * L1 * D)))
    sa = math.sqrt(max(0.0, 1.0 - ca * ca))
    us, uh = ds / D, dh / D
    # Rotacion de (us, uh) un angulo `bend * a`: antihoraria con bend=1, horaria con -1. La
    # version anterior escalaba el coseno por `bend`, que con -1 es una reflexion y subia el
    # codo por encima del casco.
    return (H[0] + L1 * (ca * us - bend * sa * uh), H[1] + L1 * (bend * sa * us + ca * uh))


class ArrMesh:
    """Malla ya empaquetada como array de numpy (N x 10, formato `VATTR`).

    El ciclista se arma con numpy en coordenadas locales y se transforma de una vez: con
    sombreado suave y articulaciones son ~3-4k triangulos, que en listas de Python por
    frame no caben en el presupuesto de 20 ms.
    """

    __slots__ = ("v",)

    def __init__(self, arr):
        self.v = arr.reshape(-1)

    @property
    def tris(self) -> int:
        return len(self.v) // 30


# --- Primitivas en coordenadas locales (s, h, w) con normales por vertice -----------------

def _pack(np, P, N, col, light):
    """Triangulos (T, 3, 3) de posiciones y normales -> filas `VATTR`."""
    out = np.empty((P.shape[0] * 3, 10), dtype="f4")
    out[:, 0:3] = P.reshape(-1, 3)
    out[:, 3:6] = N.reshape(-1, 3)
    out[:, 6:9] = col
    out[:, 9] = light
    return out


def _grid(np, P, N, col, light):
    """Rejilla (U+1, V+1) de vertices -> dos triangulos por celda."""
    a, b = P[:-1, :-1], P[:-1, 1:]
    c, d = P[1:, 1:], P[1:, :-1]
    na, nb, nc, nd = N[:-1, :-1], N[:-1, 1:], N[1:, 1:], N[1:, :-1]
    T = np.concatenate([np.stack([a, b, c], 2).reshape(-1, 3, 3),
                        np.stack([a, c, d], 2).reshape(-1, 3, 3)])
    TN = np.concatenate([np.stack([na, nb, nc], 2).reshape(-1, 3, 3),
                         np.stack([na, nc, nd], 2).reshape(-1, 3, 3)])
    return _pack(np, T, TN, col, light)


def _basis(np, d):
    d = d / (np.linalg.norm(d) or 1.0)
    ref = np.array([0.0, 0.0, 1.0]) if abs(d[2]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(d, ref)
    u /= np.linalg.norm(u)
    return d, u, np.cross(d, u)


def _tube(np, a, b, ra, rb, col, sides=10, light=BODY_L):
    """Tronco de cono a->b con radios ra->rb: miembro (con su conicidad) o tubo del cuadro."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.linalg.norm(b - a)) or 1e-6
    d, u, v = _basis(np, b - a)
    ang = np.linspace(0.0, 2 * math.pi, sides + 1)
    ring = np.cos(ang)[:, None] * u + np.sin(ang)[:, None] * v
    n = ring + d * (ra - rb) / L
    n /= np.linalg.norm(n, axis=1)[:, None]
    P = np.stack([a + ra * ring, b + rb * ring])
    return _grid(np, P, np.stack([n, n]), col, light)


def _ellipsoid(np, c, e0, r, col, nu=7, nv=12, e1=None, light=BODY_L):
    """Elipsoide de semiejes `r` sobre la base (e0, e1, e2); e0 es el eje largo. Torso,
    pelvis, cabeza, casco, zapatillas y palos de la rueda delantera."""
    c = np.asarray(c, float)
    e0 = np.asarray(e0, float)
    e0 = e0 / np.linalg.norm(e0)
    if e1 is None:
        _d, e1, e2 = _basis(np, e0)
    else:
        e1 = np.asarray(e1, float)
        e1 = e1 - e0 * float(e1 @ e0)
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(e0, e1)
    th = np.linspace(0.0, math.pi, nu + 1)[:, None, None]
    ph = np.linspace(0.0, 2 * math.pi, nv + 1)[None, :, None]
    ct, st, cp, sp = np.cos(th), np.sin(th), np.cos(ph), np.sin(ph)
    P = c + r[0] * ct * e0 + r[1] * st * cp * e1 + r[2] * st * sp * e2
    N = ct / r[0] * e0 + st * cp / r[1] * e1 + st * sp / r[2] * e2
    N = N / np.linalg.norm(N, axis=2)[..., None]
    return _grid(np, P, N, col, light)


def _sphere(np, c, r, col, light=BODY_L):
    return _ellipsoid(np, c, (1.0, 0.0, 0.0), (r, r, r), col, 5, 9, light=light)


def _torus(np, c, R, r, col, segs=28, sides=6, light=0.08):
    """Neumatico: toro en el plano (s, h) alrededor del eje lateral."""
    a = np.linspace(0.0, 2 * math.pi, segs + 1)[:, None, None]
    b = np.linspace(0.0, 2 * math.pi, sides + 1)[None, :, None]
    radial = np.concatenate([np.cos(a), np.sin(a), np.zeros_like(a)], 2)
    lat = np.array([0.0, 0.0, 1.0])
    N = np.cos(b) * radial + np.sin(b) * lat
    P = np.asarray(c, float) + R * radial + r * N
    return _grid(np, P, np.broadcast_to(N, P.shape), col, light)


def _disc(np, c, r0, r1, w, col, segs=28, light=0.10):
    """Corona plana (r0..r1) en el plano de la rueda, a `w` del centro, mirando hacia fuera.
    r0 = 0 da un disco: la cara de la lenticular."""
    a = np.linspace(0.0, 2 * math.pi, segs + 1)
    radial = np.stack([np.cos(a), np.sin(a), np.zeros_like(a)], 1)
    off = np.array([0.0, 0.0, w])
    P = np.stack([np.asarray(c, float) + off + r0 * radial, np.asarray(c, float) + off + r1 * radial])
    n = np.array([0.0, 0.0, 1.0 if w >= 0 else -1.0])
    return _grid(np, P, np.broadcast_to(n, P.shape), col, light)


def _p(s, h, w=0.0):
    return (s, h, w)


def _rider_static(np, kit, pov):
    """Piezas rigidas del conjunto, en coordenadas locales: se arman una vez por color de zona.

    Cuadro, ruedas (lo que tiene simetria de giro), sillin, acoples y, salvo en primera
    persona, torso, brazos (fijos en los acoples), cuello, cabeza y casco.
    """
    parts = []
    T = lambda a, b, r, rb=None, col=FRAME, sides=8: parts.append(  # noqa: E731
        _tube(np, a, b, r, r if rb is None else rb, col, sides, 0.10))
    bb = _p(0.0, BB_H)
    # Cuadro: perfiles gruesos de carbono (diagonal y direccion), vainas y tirantes finos.
    T(bb, _p(*HT_BOT), 0.026)                                   # diagonal
    T(bb, _p(*CLUSTER), 0.021)                                  # tubo de sillin
    T(_p(*CLUSTER), _p(*HT_TOP), 0.018)                         # tubo superior
    T(_p(*HT_BOT), _p(*HT_TOP), 0.024)                          # direccion
    for w in (-0.055, 0.055):
        T(_p(0.0, BB_H, w * 0.6), _p(REAR[0], REAR[1], w), 0.011)          # vainas
        T(_p(CLUSTER[0], CLUSTER[1], w * 0.3), _p(REAR[0], REAR[1], w), 0.010)  # tirantes
        T(_p(HT_BOT[0], HT_BOT[1], w * 0.5), _p(FRONT[0], FRONT[1], w), 0.013)  # horquilla
    T(_p(*CLUSTER), _p(*SADDLE), 0.013, col=CARBON)             # tija
    parts.append(_ellipsoid(np, _p(SADDLE[0] + 0.02, SADDLE[1] + 0.01), (1, 0.05, 0),
                            (0.135, 0.018, 0.055), SUIT, 4, 10, light=0.10))       # sillin
    # Potencia, manillar base y acoples con sus apoyabrazos.
    T(_p(*HT_TOP), _p(0.47, 0.79), 0.016)
    T(_p(0.47, 0.79, -0.19), _p(0.47, 0.79, 0.19), 0.012, sides=6)
    for w in (-1.0, 1.0):
        T(_p(0.42, 0.83, 0.045 * w), _p(HAND[0] + 0.03, 0.865, 0.045 * w), 0.011, sides=6)
        T(_p(0.45, 0.79, 0.07 * w), _p(PAD[0], PAD[1] - 0.012, 0.07 * w), 0.010, sides=6)
        parts.append(_ellipsoid(np, _p(PAD[0], PAD[1], ELB_W * w), (1, 0, 0),
                                (0.06, 0.012, 0.035), SUIT, 3, 8, light=0.10))
    # Ruedas: tubular, lenticular trasera y perfil alto delantero (las caras son planas y
    # simetricas, asi que no giran; el giro lo cuentan la marca y los palos).
    for c in (REAR, FRONT):
        parts.append(_torus(np, _p(*c), WR - 0.012, 0.012, TIRE))
        parts.append(_tube(np, _p(c[0], c[1], -0.05), _p(c[0], c[1], 0.05), 0.016, 0.016, METAL, 8, 0.15))
    for w in (-0.016, 0.016):
        parts.append(_disc(np, _p(*REAR), 0.0, WR - 0.022, w, CARBON))
        parts.append(_disc(np, _p(*FRONT), WR - 0.085, WR - 0.022, w * 0.8, CARBON))
    # Plato (lado de la transmision) y su eje.
    parts.append(_disc(np, bb, 0.0, 0.11, 0.045, METAL, 24, 0.15))
    parts.append(_disc(np, bb, 0.0, 0.11, 0.040, METAL, 24, 0.15))
    T(_p(0.0, BB_H, -0.06), _p(0.0, BB_H, 0.06), 0.02, col=METAL)

    for w in (-1.0, 1.0):                                       # antebrazos y manos (POV incluido)
        el, hd = _p(ELBOW[0], ELBOW[1], ELB_W * w), _p(HAND[0], HAND[1], 0.055 * w)
        parts.append(_tube(np, el, hd, 0.040, 0.031, SKIN))
        parts.append(_sphere(np, el, 0.041, SKIN))
        parts.append(_ellipsoid(np, hd, (1, 0.1, 0), (0.055, 0.035, 0.03), GLOVE, 4, 8))
    if not pov:
        hip, sho = np.array(_p(*HIP)), np.array(_p(*SHO))
        axis = sho - hip
        # Torso: pecho ancho y abdomen mas estrecho (un solo elipsoide sale como un huevo).
        parts.append(_ellipsoid(np, hip + axis * 0.68, axis, (0.20, 0.12, 0.185), kit, e1=(0, 1, 0)))
        parts.append(_ellipsoid(np, hip + axis * 0.30, axis, (0.21, 0.115, 0.15), kit, e1=(0, 1, 0)))
        parts.append(_ellipsoid(np, hip + np.array([0.0, -0.02, 0.0]), (1, -0.2, 0),
                                (0.13, 0.10, 0.155), SUIT))                         # pelvis
        for w in (-1.0, 1.0):                                    # brazos: manga del color de zona
            sh, el = _p(SHO[0], SHO[1], SHO_W * w), _p(ELBOW[0], ELBOW[1], ELB_W * w)
            parts.append(_sphere(np, sh, 0.058, kit))
            parts.append(_tube(np, sh, el, 0.052, 0.042, kit))
        parts.append(_tube(np, _p(SHO[0] + 0.02, SHO[1] + 0.02), _p(HEAD[0] - 0.05, HEAD[1] - 0.04),
                           0.052, 0.045, SKIN))                                     # cuello
        parts.append(_sphere(np, _p(*HEAD), 0.092, SKIN))
        # Casco de crono en lagrima: de la frente a una cola que baja sobre la espalda.
        parts.append(_ellipsoid(np, _p(HEAD[0] - 0.07, HEAD[1] + 0.03), (-1, 0.22, 0),
                                (0.21, 0.105, 0.112), HELMET, 7, 14, e1=(0.22, 1, 0), light=0.16))
        # Visera: solo la cara frontal; mas ancha que la cabeza asomaba por el costado.
        parts.append(_ellipsoid(np, _p(HEAD[0] + 0.085, HEAD[1] - 0.01), (0.2, -1, 0),
                                (0.055, 0.03, 0.078), VISOR, 4, 10, e1=(1, 0.2, 0), light=0.05))
        parts.append(_ellipsoid(np, _p(HEAD[0] - 0.09, HEAD[1] + 0.13), (-1, 0.22, 0),
                                (0.13, 0.012, 0.03), kit, 3, 8, e1=(0.22, 1, 0), light=0.3))  # franja
    return np.concatenate(parts)


_RIDER_CACHE: dict = {}


def _rider_local(np, theta, roll, kit, pov):
    """Conjunto completo en coordenadas locales: lo rigido de la cache + lo que se mueve
    (piernas, bielas, palos de la delantera y la marca de la lenticular)."""
    key = (tuple(round(c, 3) for c in kit), pov)
    static = _RIDER_CACHE.get(key)
    if static is None:
        if len(_RIDER_CACHE) > 16:
            _RIDER_CACHE.clear()
        static = _RIDER_CACHE[key] = _rider_static(np, kit, pov)
    parts = [static]
    for side in (1.0, -1.0):
        ph = theta if side > 0 else theta + math.pi
        pedal = (CRANK * math.cos(ph), BB_H + CRANK * math.sin(ph))
        parts.append(_tube(np, _p(0.0, BB_H, 0.06 * side), _p(pedal[0], pedal[1], 0.085 * side),
                           0.012, 0.010, METAL, 6, 0.15))                            # biela
        # Tobillo: el talon baja en la fase de empuje y sube en la de recobro (+-9 grados).
        a = 0.16 * math.sin(ph)
        ca, sa = math.cos(a), math.sin(a)
        ank = (pedal[0] + ANKLE[0] * ca - ANKLE[1] * sa, pedal[1] + ANKLE[0] * sa + ANKLE[1] * ca)
        toe = (pedal[0] + 0.10 * ca + 0.01 * sa, pedal[1] + 0.10 * sa - 0.01 * ca)
        heel = (ank[0] - 0.05 * ca + 0.04 * sa, ank[1] - 0.05 * sa - 0.04 * ca)
        w_p = PEDAL_W * side
        mid = ((heel[0] + toe[0]) / 2.0, (heel[1] + toe[1]) / 2.0 + 0.01)
        L = math.hypot(toe[0] - heel[0], toe[1] - heel[1])
        parts.append(_ellipsoid(np, _p(mid[0], mid[1], w_p), (toe[0] - heel[0], toe[1] - heel[1], 0),
                                (L / 2.0 + 0.02, 0.045, 0.045), SHOE, 4, 10, e1=(0, 1, 0)))
        if pov:
            continue
        kn = _knee(HIP, ank, THIGH, SHIN, 1.0)
        hip, knee, ankle = _p(HIP[0], HIP[1], HIP_W * side), _p(kn[0], kn[1], 0.10 * side), _p(ank[0], ank[1], w_p)
        parts.append(_tube(np, hip, knee, 0.082, 0.058, SUIT))                       # muslo (culote)
        parts.append(_sphere(np, knee, 0.058, SUIT))
        sock = tuple(ankle[j] + (knee[j] - ankle[j]) * 0.20 for j in range(3))
        parts.append(_tube(np, knee, sock, 0.055, 0.040, SKIN))                      # gemelo
        parts.append(_tube(np, sock, ankle, 0.041, 0.036, SOCK))
        parts.append(_sphere(np, ankle, 0.037, SOCK))
    for k in range(3):                                           # palos de la delantera
        a = roll + 2 * math.pi * k / 3.0
        d = (math.cos(a), math.sin(a), 0.0)
        c = _p(FRONT[0] + d[0] * 0.15, FRONT[1] + d[1] * 0.15)
        parts.append(_ellipsoid(np, c, d, (0.14, 0.024, 0.011), CARBON, 3, 8, e1=(-d[1], d[0], 0), light=0.10))
    for k in range(2):                                           # marca de la lenticular
        a = roll + math.pi * k
        d = (math.cos(a), math.sin(a), 0.0)
        c = _p(REAR[0] + d[0] * 0.22, REAR[1] + d[1] * 0.22)
        parts.append(_ellipsoid(np, c, (-d[1], d[0], 0), (0.07, 0.018, 0.019), LOGO, 3, 8, e1=d, light=0.25))
    return np.concatenate(parts)


def _rider(base, fwd, up, lat, theta, roll, kit, pov=False) -> "ArrMesh":
    """Bici de pista + atleta en el marco (avance, altura, lateral) apoyado en el peralte.

    En primera persona (`pov`) solo se ven la bici, los antebrazos y las manos: el resto
    quedaria entre la camara (en el casco) y el cuadro. `theta` es el angulo de la biela y
    `roll` el de la rueda (distancia / radio), asi que los palos y la marca de la lenticular
    se paran cuando se para el rider.
    """
    import numpy as np                    # el extra `gl` lo trae; el nucleo no lo necesita
    loc = _rider_local(np, theta, roll, kit, pov)
    M = np.array([fwd, up, lat], dtype="f4")                 # filas: ejes s, h, w en el mundo
    out = loc.copy()
    out[:, 0:3] = loc[:, 0:3] @ M + np.asarray(base, dtype="f4")
    out[:, 3:6] = loc[:, 3:6] @ M
    return ArrMesh(out)


def _cam_broadcast(mode, pts, frac, base, fwd, nh):
    """Camaras de retransmision (`v` las recorre): (ojo, mira, fovy).

    - tv: `TV_CAMS` camaras fijas en la zona interior, a la altura de un tripode sobre el
      apron. Se corta a la siguiente cuando el rider pasa la actual, y el zoom (fovy) sigue
      la distancia para que el rider ocupe siempre lo mismo en el cuadro.
    - grua: brazo por dentro de la pista que acompana al rider desde arriba y algo detras.
    """
    aim = (base[0], base[1], base[2] + 0.8)                 # al torso, no a las ruedas
    if mode == "tv":
        k = int(frac * TV_CAMS + 0.65) % TV_CAMS            # la proxima camara por delante
        p, _n, h = _surf(_at(pts, k / TV_CAMS), -COTE)
        o = APRON_W + 2.5
        eye = (p[0] - h[0] * o, p[1] - h[1] * o, DECK + 2.0)
        d = math.dist(eye, aim)
        return eye, aim, max(0.08, min(1.0, 2.0 * math.atan(2.4 / max(d, 0.1))))
    eye = (base[0] - nh[0] * 7.0 - fwd[0] * 4.0, base[1] - nh[1] * 7.0 - fwd[1] * 4.0, base[2] + 5.0)
    return eye, aim, 0.50


def _line_for(kmh):
    """Linea lateral (q, metros desde el borde interior) que toma el rider a `kmh`."""
    if kmh >= LINE_FAST:
        return RIDER_Q
    if kmh >= LINE_SLOW:
        return COTE_Q + (RIDER_Q - COTE_Q) * (kmh - LINE_SLOW) / (LINE_FAST - LINE_SLOW)
    if kmh >= LINE_STOP:
        return APRON_Q + (COTE_Q - APRON_Q) * (kmh - LINE_STOP) / (LINE_SLOW - LINE_STOP)
    return APRON_Q


def _ground(pt, q):
    """(posicion, normal, lateral) del suelo bajo la linea `q`: la cubierta peraltada
    (tablon y cote d'azur) o, por debajo de -COTE, la zona de seguridad llana."""
    if q >= -COTE:
        p, n, h = _surf(pt, q)
        cb, sb = math.cos(pt[4]), math.sin(pt[4])
        return p, n, (h[0] * cb, h[1] * cb, sb)
    p, _n, h = _surf(pt, -COTE)
    o = -COTE - q
    return (p[0] - h[0] * o, p[1] - h[1] * o, DECK), (0.0, 0.0, 1.0), (h[0], h[1], 0.0)


def scene_pista(snap, anim, aspect=1.6):
    """Velodromo de 250 m a escala real (1 unidad = 1 m): rectas a 12 grados, clotoides y
    arcos a 45 grados.

    Lo fijo (tablon, lineas pintadas, meta, muro, gradas, cerchos y lamparas) se construye
    una vez y se conserva como capa estatica; por frame solo cambian el rider, la estela y
    la camara chase (1 persona, estilo juego de carreras, distancia configurable).
    """
    pts = anim.get("pts")
    if pts is None:
        pts = anim["pts"] = track()
    if anim.get("static") is None:
        anim["static"] = build_static(pts)
        anim["deck"] = build_deck(pts, _pool(_lamps(pts)))
        layers = [("static", anim["static"]), ("deck", anim["deck"])]
    else:
        layers = [("static", None), ("deck", None)]  # VBO vivo: la pista no se reconstruye
    m = Mesh()

    # Inercia: la telemetria no cambia de golpe. 3,0 m/s^2 para acelerar, 2,5 para frenar
    # y +/-40 rpm/s de cadencia. Si el rider para antes de meta, desacopla y se queda.
    anim["dt"] = min(0.2, max(0.0, anim["t"] - anim.get("last", 0.0))) or 0.02
    anim["last"] = anim["t"]
    spd = max(0.0, float(snap.get("speed") or 0.0) / 3.6)
    cad = max(0.0, float(snap.get("cadence") or 0.0))
    dv, dc = spd - anim["v"], cad - anim["c"]
    anim["v"] += min(dv, 3.0 * anim["dt"]) if dv > 0 else max(dv, -2.5 * anim["dt"])
    anim["c"] += min(dc, 40.0 * anim["dt"]) if dc > 0 else max(dc, -40.0 * anim["dt"])
    # El circuito siempre mide TRACK_LEN: la posicion es la distancia recorrida modulo la
    # pista, y la fraccion avanza con la velocidad suavizada (no con la del snap).
    d0 = float(snap.get("dist_km") or 0.0) * 1000.0
    frac = (d0 % TRACK_LEN) / TRACK_LEN
    if anim.get("anchor") is None:
        anim["frac"] = frac                        # primer frame: arrancar en la posicion real
    elif (frac - anim["anchor"]) % 1.0 > 0.3:      # el snap retrocede: vuelta cerrada o reset
        anim["frac"] = frac
    else:
        # Con la fraccion movida por la velocidad suavizada, si la telemetria se para el rider
        # sigue rodando por inercia y se queda donde lo deja la frenada (no lo jala el snap).
        anim["frac"] = (anim["frac"] + anim["v"] * anim["dt"] / TRACK_LEN) % 1.0
    anim["anchor"] = frac
    # Sentido de giro: `crank` y `roll` son angulos en el plano de la bici (s = avance, h = altura)
    # y pedalear / rodar hacia adelante es en sentido HORARIO en ese plano (el pie de arriba va
    # hacia adelante y baja; la parte alta de la rueda avanza), es decir con el angulo DECRECIENDO.
    # Antes crecian y el ciclista pedaleaba y las ruedas rodaban al reves, mientras la bici avanzaba.
    anim["crank"] = anim["crank"] - (anim["c"] / 60.0) * 2 * math.pi * anim["dt"]
    # La rueda gira por distancia recorrida (theta = arco / radio), no por la cadencia:
    # asi los radios se paran cuando el rider se queda, no siguen contando pedaladas.
    anim["roll"] = anim["roll"] - anim["v"] * anim["dt"] / WR

    col = _hex(snap.get("zone_color") or "#22d3ee")
    bx, by, btx, bty, bank = _at(pts, anim["frac"])
    bl = math.hypot(btx, bty) or 1.0
    fx, fy = btx / bl, bty / bl
    nx, ny = fy, -fx                                       # normal horizontal (exterior)
    fwd = (fx, fy, 0.0)
    v, dt = anim["v"], anim["dt"]
    # Linea segun la velocidad: deriva lateral limitada, como un ciclista que sube o baja.
    tq = _line_for(v * 3.6)
    q = anim["q"] if anim.get("q") is not None else tq
    q += max(-LINE_RATE * dt, min(LINE_RATE * dt, tq - q))
    anim["q"] = q
    base, sup, slat = _ground((bx, by, btx, bty, bank), q)
    # Inclinacion fisica respecto a la vertical: tan(phi) = v^2 * kappa / g, con kappa la
    # curvatura en la linea del rider (mas por fuera, radio mayor). A la velocidad de diseno
    # coincide con el peralte; por debajo el rider va "de pie" respecto a la madera, que es lo
    # real. En recta va vertical aunque el tablon este a 12 grados.
    kap = _kappa(anim["frac"] * TRACK_LEN)
    if kap > 0.0:
        kap = 1.0 / (1.0 / kap + max(0.0, q - AXIS_Q) * math.cos(bank))
    phi_t = min(LEAN_MAX, math.atan(v * v * kap / 9.81))
    phi = anim.get("lean", 0.0)
    phi += max(-LEAN_RATE * dt, min(LEAN_RATE * dt, phi_t - phi))
    anim["lean"] = phi

    def frame(a):
        """(up, lat) inclinados `a` rad hacia el interior de la curva."""
        ca, sa = math.cos(a), math.sin(a)
        return (-nx * sa, -ny * sa, ca), (nx * ca, ny * ca, sa)

    up, lat = frame(phi)                                   # marco del rider
    cup, clat = frame(phi * CAM_LEAN)                      # marco de la camara chase
    # Estela por posiciones reales: al pararse deja de crecer y se desvanece (no avanza
    # por detrás de un rider que ya esta quieto). Es una pegatina sobre el madera, no una
    # caja flotando 2 cm encima, que era lo que la hacía leer como basura.
    tr = anim["trail"]
    # La cinta es fina (4 cm, como una linea pintada) y termina detras de la rueda trasera:
    # los discos superpuestos de antes se leian como una mancha bajo la bici.
    if not tr or math.hypot(base[0] - tr[-1][1][0], base[1] - tr[-1][1][1]) >= 0.05:
        tr.append((anim["t"], base, slat, sup))
    while tr and (anim["t"] - tr[0][0] > TRAIL_T or len(tr) > 24):
        tr.pop(0)
    tail = [e for e in tr
            if sum((e[1][j] - base[j]) * fwd[j] for j in range(3)) < REAR[0] - WR]
    for (t0, p0, l0, u0), (t1, p1, l1, u1) in zip(tail, tail[1:]):
        fade = max(0.0, 1.0 - (anim["t"] - t1) / TRAIL_T)
        c = tuple(v * (0.25 + 0.75 * fade) for v in col)
        q = [tuple(p[j] + u[j] * 0.006 + l[j] * k for j in range(3))
             for p, l, u, k in ((p0, l0, u0, -TRAIL_HW), (p0, l0, u0, TRAIL_HW),
                                (p1, l1, u1, TRAIL_HW), (p1, l1, u1, -TRAIL_HW))]
        m.quad(*q, c, tuple(u1), 0.30 * fade)

    dist = min(ZOOM_MAX, max(ZOOM_MIN, float(anim.get("dist", CHASE_DIST))))
    mode = CAM_MODES[int(anim.get("cam_mode", 0)) % len(CAM_MODES)]
    pov = dist < 0.5 and mode == "chase"
    # Sombras de contacto: una alargada bajo todo el conjunto y dos puntos donde apoyan las
    # ruedas (se multiplican: el contacto queda mas oscuro). Sin anclaje la bici flota.
    sh = ShadowMesh()
    for c, a, b, k in ((((REAR[0] + FRONT[0]) / 2.0), 0.85, 0.26, 0.45),
                       (REAR[0], 0.16, 0.07, 0.45), (FRONT[0], 0.16, 0.07, 0.45)):
        p = tuple(base[j] + fwd[j] * c + sup[j] * 0.007 for j in range(3))
        sh.ellipse(p, fwd, slat, a, b, k)
    rider = _rider(base, fwd, up, lat, anim["crank"], anim["roll"], col, pov=pov)

    # Inercia de la orbita: los eventos de arrastre llegan a 60-120 Hz y el frame va a ~50 fps;
    # sin suavizado el gesto sacude el encuadre. `anim["dt"]` es el paso real del loop.
    kk = min(1.0, LOOK_SMOOTH * anim["dt"])
    anim["yaw"] += (anim["yaw_t"] - anim["yaw"]) * kk
    anim["tilt"] += (anim["tilt_t"] - anim["tilt"]) * kk
    yaw, tilt = anim["yaw"], anim["tilt"]
    pax = tuple(-x for x in (lat if pov else clat))     # eje de cabeceo de la camara

    if pov:
        # 1a persona: la camara esta dentro del casco (a la altura de los ojos, no de la
        # cumbrera) y mira unos 31 grados hacia abajo, que es lo que mete manillar y rueda
        # delantera en el cuadro. Con el pitch del chase la bici queda fuera del encuadre.
        # Con el atleta agachado sobre los acoples, las manos quedan ~68 grados por debajo
        # del ojo: con 0,55 de pitch se salian del cuadro y la POV era solo tablon.
        fovy, pitch, eh, s0, ahead = 1.22, 0.85, HEAD[1], HEAD[0] + 0.03, 3.2
        # Aqui el arrastre es giro de cabeza: el ojo no se mueve, porque orbitar un ojo apoyado
        # a 0,3 m del casco es un circulo de 12 cm. `tilt` positivo = mirar arriba.
        eye = tuple(base[j] + fwd[j] * s0 + up[j] * eh for j in range(3))
        gaze = _rot(_rot(tuple(fwd[j] - pitch * up[j] for j in range(3)), up, yaw), pax, -tilt)
    else:
        # chase: camara alta y detras, mirando ligeramente hacia abajo sobre la bici;
        # al cerrar el zoom sube al casco (primera persona). Alturas medidas en `cup`:
        # la camara se inclina la mitad que el rider (con la normal del peralte, en la curva
        # quedaba desplazada hacia dentro y el rider salia al borde del cuadro).
        k = min(1.0, max(0.0, (dist - ZOOM_MIN) / 0.6))
        # open angular: con 0,62 la bici quedaba fuera del cuadro a 2,4 m. En primera persona
        # hace falta mas angular y mas pitch para ver manillar y rueda delante de la mira.
        fovy = 1.00 + 0.30 * (1.0 - k)
        pitch = 0.50 - 0.30 * k
        eh = HEAD[1] + 0.03 + 0.40 * k
        ahead = max(4.0, dist * 1.9)
        s0 = -dist
        # El rig (ojo y mira) gira alrededor del torso del rider: `tilt` positivo sube el ojo y la
        # mira cae con el mismo angulo, que es lo que mantiene al rider en el cuadro al orbitar.
        pivot = tuple(base[j] + cup[j] * HEAD[1] * 0.5 for j in range(3))
        rig0 = tuple(-fwd[j] * dist + cup[j] * (eh - HEAD[1] * 0.5) for j in range(3))
        eye = tuple(pivot[j] + _rot(_rot(rig0, cup, yaw), pax, tilt)[j] for j in range(3))
        # El arrastre no puede meter el ojo por debajo del tablon: en la recta un tilt negativo
        # lo hundia, y en la curva ya lo sube el peralte.
        h = sum(sup[j] * (eye[j] - base[j]) for j in range(3))
        if h < LOOK_MIN_H:
            eye = tuple(eye[j] + sup[j] * (LOOK_MIN_H - h) for j in range(3))
        gaze = _rot(_rot(tuple(fwd[j] - pitch * cup[j] for j in range(3)), cup, yaw), pax, tilt)

    center = tuple(eye[j] + gaze[j] * ahead for j in range(3))
    if mode != "chase":
        eye, center, fovy = _cam_broadcast(mode, pts, anim["frac"], base, fwd, (nx, ny))
    from .cluster import hud_layers               # lazy: cluster importa de aqui
    hud_on = bool(anim.get("hud_on", True))
    return layers + [("frame", m), ("rider", rider), ("shadow", sh)] + hud_layers(snap, anim, aspect), \
        (eye, center, (0.0, 0.0, 1.0), fovy, 300.0, hud_on)


def _at(pts, f):
    """Punto (x, y, tx, ty, bank) en la fraccion `f` del trazado, interpolado entre muestras:
    sin interpolacion el rider salta de indice en indice y la estela se corta."""
    n = len(pts) - 1
    i = min(n - 1, max(0, int(f * n)))
    k = f * n - i
    x0, y0, tx0, ty0, b0 = pts[i]
    x1, y1, tx1, ty1, b1 = pts[i + 1]
    # Tangente y peralte tambien interpolados: a 45 grados un salto de peralte cada 0,5 m
    # se ve como un tembleque del ciclista.
    return (x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, tx0 + (tx1 - tx0) * k,
            ty0 + (ty1 - ty0) * k, b0 + (b1 - b0) * k)


def scene_menu(snap, anim, aspect=1.6):
    """Menu previo a Play (todo en capas `hud_*`)."""
    from .menuview import scene_menu as _scene          # lazy: menuview importa de aqui
    return _scene(snap, anim, aspect)


SCENES = {"hr": scene_hr, "watts": scene_watts, "gauge": scene_gauge, "pista": scene_pista,
          "menu": scene_menu}


def _plan_window(glfw, want=(1920, 1080)):
    """Elige monitor y tamano de ventana apuntando a `want` **pixels fisicos**.

    GLFW pide tamanos en puntos: en una pantalla Retina (content scale 2) una ventana
    de 960x540 puntos renderiza a 1920x1080 reales. Se elige el monitor cuya area util
    pueda alojar esos pixels y se centra alli la ventana; si ninguno puede, se baja a
    el 16:9 mas grande que quepa.
    """
    try:
        mons = [m for m in (glfw.get_monitors() or []) if m]
    except Exception:  # noqa: BLE001 - API ausente: se usa el primario
        prim = glfw.get_primary_monitor()
        mons = [prim] if prim else []

    def info(m):
        mode = glfw.get_video_mode(m)
        sx = sy = 1.0
        try:
            got = glfw.get_monitor_content_scale(m)
            if got and got[0]:
                sx, sy = float(got[0]), float(got[1] or got[0])
        except Exception:  # noqa: BLE001
            pass
        try:
            _x, _y, wa, ha = glfw.get_monitor_workarea(m)
        except Exception:  # noqa: BLE001
            wa = int((mode.size.width or want[0]) / sx)
            ha = int((mode.size.height or want[1]) / sy)
            _x = _y = 0
        return {"pos": (_x, _y), "pts": (int(wa), int(ha)), "scale": (sx, sy),
                "px": (int(wa * sx), int(ha * sy))}

    if not mons:
        return (want[0], want[1]), None
    cand = [(m, info(m)) for m in mons]
    # ordena por pixeles utiles: el monitor mas grande que soporte lo pedido
    roomy = [(m, d) for m, d in cand
             if d["px"][0] >= want[0] and d["px"][1] >= want[1]]
    if roomy:
        mon, d = max(roomy, key=lambda t: t[1]["px"][0] * t[1]["px"][1])
        sx, sy = d["scale"]
        size = (max(640, int(want[0] / sx)), max(360, int(want[1] / sy)))
        return size, mon
    mon, d = max(cand, key=lambda t: t[1]["px"][0] * t[1]["px"][1])
    sx, sy = d["scale"]
    k = min(d["pts"][0] * sx / float(want[0]), d["pts"][1] * sy / float(want[1]))
    size = (max(800, int(want[0] * k / sx)), max(450, int(want[1] * k / sy)))
    return size, mon


def _window_for(glfw, mon, want=(1920, 1080)):
    """Tamano en puntos para un monitor concreto (sin pasar de su area util)."""
    mode = glfw.get_video_mode(mon)
    sx = sy = 1.0
    _x = _y = 0
    try:
        got = glfw.get_monitor_content_scale(mon)
        if got and got[0]:
            sx, sy = float(got[0]), float(got[1] or got[0])
    except Exception:  # noqa: BLE001
        pass
    try:
        _x, _y, wa, ha = glfw.get_monitor_workarea(mon)
    except Exception:  # noqa: BLE001
        wa, ha = (mode.size.width or want[0]) / sx, (mode.size.height or want[1]) / sy
    size = (max(640, min(int(wa), int(want[0] / sx))), max(360, min(int(ha), int(want[1] / sy))))
    return size, (_x, _y, int(wa), int(ha))


def _mkwin(glfw, size, title, mon, share):
    """`create_window` con MSAA y, si el driver no lo da, otra vez sin multisample."""
    win = glfw.create_window(size[0], size[1], title, mon, share)
    if win is None:
        glfw.window_hint(glfw.SAMPLES, 0)
        win = glfw.create_window(size[0], size[1], title, mon, share)
    return win


def _split_rect(view, i, n):
    """Rectangulo normalizado (x, y, w, h) para el split vertical: dashboard 40%, race 60%."""
    if n <= 1:
        return (0.0, 0.0, 1.0, 1.0)
    if view == "gauge":
        return (0.0, 0.0, 0.40, 1.0)
    if view == "pista":
        return (0.40, 0.0, 0.60, 1.0)
    return (i / n, 0.0, 1.0 / n, 1.0)


def _new_anim():
    return {"t": 0.0, "needle": 0.0, "frac": 0.0, "last": 0.0,
            "hr": 0.0, "w": 0.0, "v": 0.0, "c": 0.0, "dt": 0.0, "crank": 0.0, "roll": 0.0,
            "yaw": 0.0, "yaw_t": 0.0, "tilt": 0.0, "tilt_t": 0.0,
            "trail": [], "pts": None, "static": None, "anchor": None, "q": None, "lean": 0.0}


# Teclas de vista en standalone (`--3d`, sin TUI): `h/w/g/p` cambian a esa vista, `3` y `r`
# hacen toggle. Es una funcion pura para poder afirmarla headless (en child_main no se
# pueden sintetizar eventos de teclado sin GLFW). El zoom (`+`/`-`) no se resuelve aqui:
# hace falta el slot, y `on_key` llama a `add_zoom`. Lo demas (q/x/s) se reenvia al motor.
VIEWKEYS = {"h": "hr", "w": "watts", "g": "gauge", "p": "pista"}
VIEWTOGGLE = {"3": "gauge", "r": "pista"}


def standalone_key(name, local_views):
    """Lista de vistas resultante, o None si la tecla no es de vista (se reenvia al motor).

    `h/w/g/p` cambian a esa vista, `3` y `r` hacen toggle, y `left`/`right` recorren el
    mismo orden que la consola (VIEW_ORDER): con una sola vista se pasa a la siguiente,
    con varias se cambia la primera sin perder las demas.
    """
    if name in VIEWKEYS:
        return [VIEWKEYS[name]]
    if name in VIEWTOGGLE:
        cur = list(local_views)
        v = VIEWTOGGLE[name]
        if v in cur:
            cur.remove(v)
        else:
            cur.append(v)
        return cur or ["gauge"]
    if name in ("left", "right"):
        cur = [v for v in local_views if v in VIEW_ORDER] or ["gauge"]
        i = VIEW_ORDER.index(cur[0]) if cur[0] in VIEW_ORDER else 0
        nxt = VIEW_ORDER[(i + (1 if name == "right" else -1)) % len(VIEW_ORDER)]
        return [nxt] + [v for v in cur[1:] if v != nxt]
    return None


def _uniq(items):
    """Deduplica por identidad (los objetos window de GLFW no son hashables)."""
    seen, out = [], []
    for it in items:
        if id(it) not in seen:
            seen.append(id(it))
            out.append(it)
    return out


# ---------------------------------------------------------------- proceso hijo
def child_main() -> None:
    """Entrada del proceso hijo (`python -m hrdash.glview`): ventana(s) GLFW + render.

    Habla JSON por stdio: el dashboard escribe un snapshot por linea (stdin) y la
    ventana reporta errores por stdout. Evitamos `multiprocessing` porque con `spawn`
    macOS re-ejecuta el entry point de consola y el hijo arrancaria otro dashboard.

    Con dos vistas activas: una ventana por monitor cuando hay monitores para todas
    (cada contexto con su propio program y VAO, que son objetos por contexto); si no,
    una sola ventana con split vertical: dashboard izquierda 40%, race derecha 60%.
    Una sola vista activa: esa ventana es full screen.
    """
    err = None
    glfw = None
    try:
        import glfw
        import numpy as np
        import moderngl
    except Exception as exc:  # noqa: BLE001 - sin dependencias gl: la TUI lo avisa
        _emit({"err": f"3D requiere las extras gl: pip install -e \".[gl]\" ({type(exc).__name__})"})
        return
    try:
        if not glfw.init():
            raise RuntimeError("glfw.init fallo")
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
        glfw.window_hint(glfw.RESIZABLE, glfw.TRUE)
        # 4x MSAA: sin el, radios, lineas pintadas y juntas del tablon serruchan. Si el driver
        # no da un framebuffer multisample, `_mkwin` repite sin el (nunca tumba el hijo).
        glfw.window_hint(glfw.SAMPLES, MSAA)
        mons = [m for m in (glfw.get_monitors() or []) if m]
        zoom = {}                      # vista -> distancia de camara (la rueda la cambia)
        look = {}                      # vista -> (yaw, tilt) de la orbita (el arrastre la mueve)
        press = {}                     # winkey(ventana) -> punto donde empezo el arrastre
        focus = {}                     # winkey(ventana) -> enfocada (sin foco no llegan teclas)
        nudge = None                   # nonce del ultimo paso de zoom que aplico el hijo
        cam_seen = None                # contador de `v` de la TUI ya aplicado
        hud_seen = None                # contador de `i` de la TUI ya aplicado
        primary = None                 # (win, ctx, prog, size): la ventana que no se recrea
        by_view = {}                     # view -> slot: persiste entre cambios de vista
        multi_mode = None              # ventana por monitor; se decide con el primer layout
        # Teclas reenviadas a la TUI: la TUI es la dueña de los bindings, aqui solo se traduce el
        # codigo de GLFW al nombre que la TUI usa, para no duplicar la tabla de acciones.
        KEYNAMES = {glfw.KEY_Q: "q", glfw.KEY_X: "x", glfw.KEY_S: "s", glfw.KEY_C: "c",
                    glfw.KEY_H: "h", glfw.KEY_W: "w", glfw.KEY_G: "g", glfw.KEY_P: "p",
                    glfw.KEY_3: "3", glfw.KEY_R: "r",
                    glfw.KEY_LEFT: "left", glfw.KEY_RIGHT: "right",
                    glfw.KEY_LEFT_BRACKET: "[", glfw.KEY_RIGHT_BRACKET: "]",
                    glfw.KEY_MINUS: "-", glfw.KEY_EQUAL: "+",     # `+` es KEY_EQUAL (sin shift)
                    glfw.KEY_V: "v", glfw.KEY_I: "i",
                    glfw.KEY_ENTER: "enter", glfw.KEY_KP_ENTER: "enter",
                    glfw.KEY_SPACE: "space", glfw.KEY_ESCAPE: "escape"}
        slots: list = []
        # En standalone (`--3d`) no hay TUI: la ventana es la dueña de la vista y del zoom,
        # y solo reenvia al motor lo que afecta a la sesion (q/x/s). `standalone` y
        # `local_views` se comparten entre el loop (que los lee del snapshot) y `on_key`.
        standalone = False
        # Fase del motor ("menu" antes de Play, "running" despues). En el menu NINGUNA tecla de
        # vista actua: si lo hiciera, `g` o las flechas se saltarian el Play y empezaria el render.
        phase = "running"
        local_views: list = []

        def set_views(v):
            """Cambiar la vista en standalone y reflejarla al motor (el motor la refleja)."""
            local_views[:] = [x for x in v if x in SCENES]
            _emit({"views": list(local_views)})

        def winkey(win):
            """Clave comparable y hashable para una ventana GLFW.

            Las ventanas son `c_void_p`: dos instancias distintas pueden ser la misma ventana (cada
            callback recibe su propia instancia, no la que devolvio `create_window`), y ademas no son
            hashables para usarlas como clave de dict. Por eso el hit-test compara el `value` y `press`
            se indexa por esa clave.
            """
            v = getattr(win, "value", None)
            if v:
                return v
            try:
                # glfw-python (>= 2.x en Linux) da `LP__GLFWwindow`, un puntero sin `.value`, y
                # cada callback recibe un objeto nuevo: `id(win)` nunca coincide y `slot_under`
                # devolvia None (rueda, orbita, foco y hover no se enteraban). Vale la direccion.
                import ctypes
                return ctypes.cast(win, ctypes.c_void_p).value or id(win)
            except Exception:  # noqa: BLE001 - tipo inesperado: el comportamiento de siempre
                return id(win)

        def same_win(a, b):
            return winkey(a) == winkey(b)

        def slot_under(win):
            """Slot bajo el cursor: en split hay varias vistas en la misma ventana."""
            mine = [s for s in slots if same_win(s[1], win)]
            if len(mine) == 1:
                return mine[0]
            try:
                px, py = glfw.get_cursor_pos(win)
                wpts, hpts = glfw.get_window_size(win)
                if not wpts or not hpts:
                    return mine[0]
                nx, ny = px / float(wpts), 1.0 - py / float(hpts)   # rects: origen abajo-izq
                for s in mine:
                    r = s[2]
                    if r[0] <= nx < r[0] + r[2] and r[1] <= ny < r[1] + r[3]:
                        return s
            except Exception:  # noqa: BLE001 - sin cursor: la primera vista de la ventana
                pass
            return mine[0] if mine else None

        def view_under(win):
            s = slot_under(win)
            return s[0] if s else None

        def add_zoom(s, dstep):
            """Paso de zoom sobre la vista, con topete sobre el valor nuevo.

            Lo usan la rueda y el `+`/`-` que manda la TUI: la camara es estado del hijo, asi que la
            TUI no fija el zoom, manda un paso. El topete se aplica sobre el valor nuevo, no sobre el
            actual, para poder seguir empujando desde el limite y volver.
            """
            if s is None or s[0] not in ZOOMABLE:
                return
            v = s[0]
            z = min(ZOOM_MAX, max(ZOOM_MIN, zoom.get(v, CHASE_DIST) + dstep))
            if z != zoom.get(v, CHASE_DIST):
                zoom[v] = z
                _emit({"zoom": {v: round(z, 3)}})

        def add_look(s, dyaw, dtilt):
            """Suma el gesto a la orbita de la vista apuntada, con topete sobre el valor nuevo.

            El topete se aplica despues de sumar, no sobre el valor actual: si el tilt ya estaba en
            el topete, comprobar antes de sumar permitia seguir empujando y el valor se salia (la
            camara se quedaba pegada arriba, sin poder volver).
            """
            if s is None or s[0] not in LOOKABLE:
                return
            v = s[0]
            y, t = look.get(v, [0.0, 0.0])
            y = min(LOOK_MAX_YAW, max(-LOOK_MAX_YAW, y + dyaw))
            t = min(LOOK_MAX_TILT, max(-LOOK_MAX_TILT, t + dtilt))
            if [y, t] != look.get(v):
                look[v] = [y, t]

        def on_move(win, x, y):
            """Arrastre con boton: orbita la vista apuntada (horizontal = yaw, vertical = tilt).

            El click que enfoca la ventana no debe mover la camara: `on_button` solo ancla el punto
            y el gesto empieza en el movimiento siguiente. En MacOS el primer click sobre la ventana
            suele ser el que la enfoca (GLFW no entrega eventos de boton a una ventana sin foco), asi
            que hay que clicar una vez y despues arrastrar. Con el touchpad el arrastre de un dedo
            tambien llega por aqui: el trackpad actua como raton con boton.
            """
            if phase == "menu":                  # hover: el cursor en fraccion de ventana
                s = slot_under(win)
                try:
                    wpts, hpts = glfw.get_window_size(win)
                    if s is not None and wpts and hpts:
                        s[5]["mouse_n"] = (x / float(wpts), 1.0 - y / float(hpts))
                except Exception:  # noqa: BLE001 - ventana cerrada durante el movimiento
                    pass
                return
            p = press.get(winkey(win))
            if p is None:
                return
            add_look(slot_under(win), STEP_LOOK * (x - p[0]), STEP_LOOK * (p[1] - y))
            press[winkey(win)] = (x, y)

        def on_button(win, button, action, _mods):
            """Clic: mantener el boton = arrastre; boton derecho centra la orbita."""
            if phase == "menu":                  # en el menu el clic izquierdo pulsa el widget
                if button == glfw.MOUSE_BUTTON_LEFT and action == PRESS:
                    try:
                        from .menuview import hit_id
                        px, py = glfw.get_cursor_pos(win)
                        wpts, hpts = glfw.get_window_size(win)
                        wid = hit_id(snap.get("menu"), (px / float(wpts), 1.0 - py / float(hpts)),
                                     wpts / float(hpts))
                    except Exception:  # noqa: BLE001 - ventana cerrada durante el clic
                        wid = None
                    if wid:
                        _emit({"ui": {"act": wid}})
                return
            if action == PRESS:
                s = slot_under(win)
                if button == glfw.MOUSE_BUTTON_RIGHT and s is not None:
                    look[s[0]] = [0.0, 0.0]
                    return
                try:
                    press[winkey(win)] = glfw.get_cursor_pos(win)   # la ventana no es hashable
                except Exception:  # noqa: BLE001 - ventana cerrada durante el click
                    pass
            elif action == RELEASE:
                press.pop(winkey(win), None)

        def on_key(win, key, _scan, action, _mods):
            """Tecla: en modo TUI se reenvia, que la TUI es la dueña de los bindings.

            GLFW solo entrega teclas a la ventana enfocada, y cuando la ventana 3D esta enfocada el
            terminal deja de recibirlas: por eso el reenvio es lo que hace que `q`, `x`, `p` o `r`
            funcionen desde la ventana. Sin foco no llega nada. Se reenvia solo PRESS: con REPEAT la
            TUI recibiria una accion por cada repeticion del teclado.

            En standalone (`--3d`) no hay terminal al que reenviar: la ventana resuelve aqui la
            vista (`h/w/g/p`, `3` y `r`) y el zoom (`+`/`-`); lo demas (q/x/s) va al motor, que
            procesa la sesion. Asi el teclado de la ventana no colisiona con el del terminal.
            """
            if action != PRESS:
                return
            name = KEYNAMES.get(key)
            if not name:
                return
            if phase == "menu":
                if name in ("enter", "space"):
                    _emit({"ui": {"act": "play"}})
                elif name in ("q", "escape"):
                    _emit({"key": "q"})          # salir del menu sin grabar nada
                return
            if name == "v":                  # camara de la pista: es estado de la ventana
                cycle_cam()
                return
            if name == "i":                  # HUD de la pista: tambien estado de la ventana
                toggle_hud()
                return
            if standalone:
                new = standalone_key(name, local_views)
                if new is not None:
                    set_views(new)
                    return
                if name in ("+", "-"):
                    add_zoom(next((s for s in slots if s[0] in ZOOMABLE), None),
                             STEP_ZOOM * (1 if name == "+" else -1))
                    return
            _emit({"key": name})

        def cycle_cam():
            """Siguiente modo de camara (chase / tv / grua) en la vista pista."""
            for s in slots:
                if s[0] == "pista":
                    s[5]["cam_mode"] = (int(s[5].get("cam_mode", 0)) + 1) % len(CAM_MODES)
                    _emit({"cam": CAM_MODES[s[5]["cam_mode"]]})
                    return

        def toggle_hud():
            """Muestra u oculta los tacometros superpuestos a la vista pista."""
            for s in slots:
                if s[0] == "pista":
                    s[5]["hud_on"] = not s[5].get("hud_on", True)
                    _emit({"hud": s[5]["hud_on"]})
                    return

        def on_focus(win, focused):
            focus[winkey(win)] = bool(focused)

        def on_scroll(win, dx, dy):
            """Rueda/touchpad: `dy` es zoom, `dx` es la orbita horizontal.

            El touchpad de MacOS no expone gestos de pinza ni de rotacion a GLFW: lo unico que llega
            son estos eventos de scroll, asi que dos dedos en horizontal = yaw y en vertical = zoom.
            Por eso el zoom por pinza no se puede hacer sin un layer privado de MacOS.
            """
            s = slot_under(win)
            if s is None or s[0] not in ZOOMABLE:
                return
            add_zoom(s, STEP_ZOOM * float(dy))
            if dx:
                add_look(s, STEP_LOOK_SCROLL * float(dx), 0.0)

        def bind(win):
            """Los callbacks de la ventana: zoom, orbita, centrar, teclas y foco."""
            glfw.set_scroll_callback(win, on_scroll)
            glfw.set_cursor_pos_callback(win, on_move)
            glfw.set_mouse_button_callback(win, on_button)
            glfw.set_key_callback(win, on_key)
            glfw.set_window_focus_callback(win, on_focus)
            try:
                glfw.focus_window(win)          # sin foco no llega ni una tecla ni un clic
            except Exception:  # noqa: BLE001 - enfocar es decorativo
                pass
            focus.setdefault(winkey(win), False)

        def open_windows(views):
            """(view, window, rect, ctx, prog, anim, vaos): una ventana por monitor o split.

            Las ventanas persisten entre cambios de vista. moderngl cachea su primer
            contexto del proceso y desde el segundo `create_context` intenta detectar el
            contexto actual, que en Linux devuelve NULL y levanta (glXGetCurrentContext):
            el memo destruia y recreaba la ventana al cambiar de vista, y navegar con las
            flechas mataba al hijo. La ventana primaria ya no se recrea (el memo la salta)
            y el split se reconstruye sobre ella; la ventana por monitor solo funciona si
            moderngl detecta el contexto de cada ventana, y si no se vuelve al split sin
            matar el hijo.
            """
            nonlocal primary, multi_mode

            if multi_mode is None:
                multi_mode = len(views) >= 2 and len(mons) >= len(views)
            if multi_mode:
                for i, view in enumerate(views):
                    if view in by_view:
                        yield by_view[view]
                        continue
                    size, (xa, ya, wa, ha) = _window_for(glfw, mons[i], (1920, 1080))
                    win = _mkwin(glfw, size, f"HRDash 3D - {view}",
                                 mons[i], primary[0] if primary else None)
                    if win is None:
                        raise RuntimeError("glfw no pudo crear la ventana")
                    try:                                   # centrada en el monitor que la aloja
                        glfw.set_window_pos(win, int(xa + (wa - size[0]) / 2),
                                            int(ya + (ha - size[1]) / 2))
                    except Exception:  # noqa: BLE001 - centrar es decorativo
                        pass
                    glfw.make_context_current(win)
                    bind(win)                                          # zoom, orbita y centrar
                    # con dos ventanas el vsync se pagaria dos veces por frame: aqui no esperamos
                    glfw.swap_interval(0)
                    # contexto por ventana: los objetos (program/VAO) pertenecen a SU contexto
                    try:
                        cx = moderngl.create_context()
                    except Exception:  # noqa: BLE001 - solo el primer contexto es detectable
                        try:                                # (Linux): se cae al split en la
                            glfw.destroy_window(win)        # primaria, el hijo no muere
                        except Exception:  # noqa: BLE001
                            pass
                        multi_mode = False
                        break
                    if primary is None:
                        primary = (win, cx, cx.program(vertex_shader=VS, fragment_shader=FS), size)
                    _emit({"win": list(size), "fb": list(glfw.get_framebuffer_size(win)),
                           "view": view, "rect": [0.0, 0.0, 1.0, 1.0]})
                    slot = (view, win, (0.0, 0.0, 1.0, 1.0), cx,
                            cx.program(vertex_shader=VS, fragment_shader=FS), _new_anim(), {})
                    by_view[view] = slot
                    yield slot
                if multi_mode:
                    return

            if primary is None:                            # ventana unica: se crea la primaria
                size, mon = _plan_window(glfw, (1920, 1080))
                win = _mkwin(glfw, size, "HRDash 3D", mon, None)
                if win is None:
                    raise RuntimeError("glfw no pudo crear la ventana")
                if mon is not None:
                    try:
                        xa, ya, wa, ha = glfw.get_monitor_workarea(mon)
                        glfw.set_window_pos(win, int(xa + (wa - size[0]) / 2),
                                            int(ya + (ha - size[1]) / 2))
                    except Exception:  # noqa: BLE001 - centrar es decorativo
                        pass
                glfw.make_context_current(win)
                bind(win)                                            # zoom, orbita y centrar
                glfw.swap_interval(1)
                cx = moderngl.create_context()
                primary = (win, cx, cx.program(vertex_shader=VS, fragment_shader=FS), size)
            win0, cx, prog, size = primary
            rects = [(v, _split_rect(v, i, len(views))) for i, v in enumerate(views)]
            _emit({"win": list(size), "fb": list(glfw.get_framebuffer_size(win0)),
                   "view": [v for v, _r in rects],
                   "rect": [list(r) for _v, r in rects]})
            for view, r in rects:
                if view in by_view:                           # slot que sobrevive al cambio
                    old = by_view[view]                       # (su contexto sigue vigente)
                    slot = (view, old[1], r, old[3], old[4], old[5], old[6])
                else:
                    slot = (view, win0, r, cx, prog, _new_anim(), {})
                by_view[view] = slot
                yield slot

        feed = _stdin_feed()
        snap = {"view": "gauge", "views": ["gauge"], "hr": 0, "hrmax": 180,
                "samples": [], "zone_color": "#ef4444"}
        clock = time.time()
        key = None                     # `slots` es el estado que ve el callback de la rueda
        frames, report_t, work = 0, time.time(), 0.0
        while True:
            t0 = time.time()
            keep, got = feed()
            if not keep:
                break                                       # el dashboard cerro la tuberia
            if got is not None:
                snap = got
                standalone = bool(snap.get("standalone"))
                new_phase = str(snap.get("phase") or "running")
                if standalone and new_phase != phase:
                    # menu -> running: la ventana pasa a las vistas que el motor fijo para rodar
                    local_views[:] = [v for v in (snap.get("views") or []) if v in SCENES]
                phase = new_phase
                if standalone and not local_views and snap.get("views"):
                    # Primera linea: la ventana hereda las vistas del motor; a partir de
                    # aqui la ventana las cambia y se las refleja al motor por stdout.
                    local_views[:] = [v for v in snap["views"] if v in SCENES]
                for v, z in (snap.get("zoom") or {}).items():   # zoom fijado desde fuera
                    if v in ZOOMABLE and isinstance(z, (int, float)) and zoom.get(v) != float(z):
                        zoom[v] = min(ZOOM_MAX, max(ZOOM_MIN, float(z)))
                for v, val in (snap.get("look") or {}).items():   # orbita fijada desde fuera
                    if v in LOOKABLE and isinstance(val, (list, tuple)) and len(val) == 2:
                        y = min(LOOK_MAX_YAW, max(-LOOK_MAX_YAW, float(val[0])))
                        t = min(LOOK_MAX_TILT, max(-LOOK_MAX_TILT, float(val[1])))
                        if look.get(v) != [y, t]:
                            look[v] = [y, t]
            views = [v for v in (snap.get("views") or [snap.get("view", "gauge")]) if v in SCENES]
            if not views:
                break
            shape = (tuple(views), len(mons))
            if shape != key:
                key = shape
                # Sobreviven los slots de las vistas que siguen: la ventana primaria no se
                # destruye porque su contexto es el unico que moderngl detecta (recrearla
                # era el bug que mataba al hijo con las flechas). Se destruye solo la
                # ventana de la vista que sale, liberando sus VAOs con su contexto vigente.
                for view, win, _r, c, _p, _a, vaos in slots:
                    if view in views:
                        continue
                    try:
                        glfw.make_context_current(win)
                        for va in vaos.values():
                            va.release()
                    except Exception:  # noqa: BLE001
                        pass
                    if primary is None or win != primary[0]:
                        try:
                            glfw.destroy_window(win)
                        except Exception:  # noqa: BLE001
                            pass
                    by_view.pop(view, None)
                slots = list(open_windows(views))
            cmd = snap.get("zoom_cmd")                   # `+`/`-`: la TUI manda el paso, no el valor
            if isinstance(cmd, (list, tuple)) and len(cmd) == 2 and cmd[0] != nudge:
                try:
                    d = int(cmd[1])
                except (TypeError, ValueError):  # noqa: BLE001 - linea malformada
                    d = 0
                if d:                                    # delta 0 es el estado inicial: no mueve nada
                    nudge = cmd[0]                       # el mismo nonce no se aplica dos veces
                    # Se aplica aqui, no con el resto del snapshot: `slots` acaba de abrirse y la
                    # primera linea JSON llega antes de crear la ventana, asi que no hay vista.
                    add_zoom(next((s for s in slots if s[0] in ZOOMABLE), None),
                             STEP_ZOOM * (1 if d > 0 else -1))
            cc = snap.get("cam_cmd")                     # `v` en la TUI: contador, no valor
            if isinstance(cc, int):
                if cam_seen is not None and cc != cam_seen:
                    cycle_cam()
                cam_seen = cc
            hc = snap.get("hud_cmd")                     # `i` en la TUI: igual que `v`
            if isinstance(hc, int):
                if hud_seen is not None and hc != hud_seen:
                    toggle_hud()
                hud_seen = hc
            if any(glfw.window_should_close(s[1]) for s in slots):
                err = "ventana cerrada"
                break
            for view, win, rect, ctx, prog, anim, vaos in slots:
                glfw.make_context_current(win)
                ctx.enable(moderngl.DEPTH_TEST)
                fbw, fbh = glfw.get_framebuffer_size(win)
                x, y = int(fbw * rect[0]), int(fbh * rect[1])
                vw, vh = max(1, int(fbw * rect[2])), max(1, int(fbh * rect[3]))
                ctx.scissor = (x, y, vw, vh)
                ctx.viewport = (x, y, vw, vh)
                ctx.clear(*FLOOR, 1.0)
                anim["t"] = t0 - clock
                anim["dist"] = zoom.get(view, CHASE_DIST)
                anim["yaw_t"], anim["tilt_t"] = look.get(view, [0.0, 0.0])
                layers, cam = SCENES[view](snap, anim, max(0.25, vw / vh))
                for name, mesh in layers:
                    if mesh is None:
                        continue                       # capa sin cambios: conserva su VBO
                    old = vaos.get(name)
                    if old is not None:
                        old.release()
                    vaos[name], _p = _upload(ctx, prog, mesh, np, moderngl)
                eye, center, up, fovy = cam[:4]
                far = cam[4] if len(cam) > 4 else 80.0     # la pista necesita mas fondo
                mvp = perspective(fovy, vw / vh, 0.1, far) @ look_at(eye, center, up)
                hud = (hud_cam(vw / vh), HUD_EYE) if len(cam) > 5 and cam[5] else None
                _draw(ctx, vaos, moderngl, np, mvp, eye, hud)
            t_drawn = time.time()
            # Un intercambio por VENTANA y fotograma, despues de dibujar todas las vistas. En la
            # vista mixta (tablero + pista) las dos comparten ventana: intercambiar dentro del bucle
            # por vistas hacia dos `swap_buffers` por fotograma, con vsync cada uno espera a un
            # refresco (maximo 30 fps), el primero mostraba el fotograma a medias y la segunda
            # mitad se pintaba sobre un buffer indefinido (parpadeo).
            for w in _uniq(s[1] for s in slots):
                glfw.make_context_current(w)
                glfw.swap_buffers(w)
            glfw.poll_events()
            frames += 1
            if frames >= 60:
                now = time.time()
                _emit({"fps": round(frames / max(1e-6, now - report_t), 1),
                       "ms": round(1000 * work / max(1, frames), 1),
                       "dist": {v: round(z, 3) for v, z in zoom.items()},
                       "look": {v: [round(y, 3), round(t, 3)] for v, (y, t) in look.items()},
                       "focus": {s[0]: bool(focus.get(winkey(s[1]))) for s in slots}})
                frames, report_t, work = 0, now, 0.0
            work += t_drawn - t0                         # sin la espera del vsync: es el coste real
            # Tope de ~60 fps solo para no girar en vacio sin vsync. Con vsync el intercambio ya
            # bloquea hasta el refresco (>= 16,7 ms) y no se duerme nada: dormir hasta 20 ms
            # metia 50 fps sobre una pantalla de 60 Hz, que se ve como judder.
            time.sleep(max(0.0, 0.016 - (time.time() - t0)))
        for view, win, _r, ctx, _p, _a, vaos in slots:
            try:
                glfw.make_context_current(win)
                for va in vaos.values():
                    va.release()
            except Exception:  # noqa: BLE001
                pass
        for w in _uniq(s[1] for s in slots):
            try:
                glfw.destroy_window(w)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 - la ventana nunca tumba el dashboard
        err = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            glfw.terminate()
        except Exception:  # noqa: BLE001
            pass
        if err:
            _emit({"err": err[:120]})


def _emit(obj: dict) -> None:
    import sys
    try:
        import json
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _stdin_feed():
    """Reader no bloqueante: devuelve el ultimo snapshot JSON o None si el padre se fue."""
    import json
    import os
    import sys
    fd = sys.stdin.fileno()
    try:
        os.set_blocking(fd, False)
    except Exception:  # noqa: BLE001
        pass
    state = {"buf": "", "last": None, "eof": False}

    def feed():
        """(sigue_vivo, ultimo_snapshot). sigue_vivo False solo si el padre cerro stdin."""
        if state["eof"]:
            return False, state["last"]
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    state["eof"] = True
                    break
                state["buf"] += chunk.decode("utf-8", "replace")
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            state["eof"] = True
        while "\n" in state["buf"]:
            line, state["buf"] = state["buf"].split("\n", 1)
            try:
                got = json.loads(line)
                if isinstance(got, dict):
                    state["last"] = got
            except Exception:  # noqa: BLE001 - linea corrupta: se ignora
                pass
        return (not state["eof"]) or state["last"] is not None, state["last"]

    return feed


# ---------------------------------------------------------------- host (lado TUI)
class GLHost:
    """Dueño del proceso de la ventana 3D: arranca, alimenta y detiene sin tumbar la TUI."""

    def __init__(self):
        self._proc = None
        self._out = None
        self._version = 0
        self.error = ""

    def start(self) -> bool:
        """Levanta la ventana en un proceso aparte; False si el entorno no da para GL."""
        import subprocess
        import sys
        self.stop()
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "hrdash.glview"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._out = self._proc.stdout
            try:
                import os
                os.set_blocking(self._out.fileno(), False)
            except Exception:  # noqa: BLE001
                self._out = None
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - sin GL: seguir solo en 2D
            self.error = f"3D: {type(exc).__name__}: {exc}"[:90]
            self._proc = self._out = None
            return False

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def push(self, snap: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        self._version += 1
        snap["version"] = self._version
        try:
            import json
            self._proc.stdin.write(json.dumps(snap, default=float) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            self.poll_error()
            self.stop()
        except Exception as exc:  # noqa: BLE001
            self.error = f"3D push: {type(exc).__name__}"[:90]
            self.stop()

    def drain(self) -> list:
        """Lee sin bloquear lo que la ventana haya reportado (errores, teclas, foco, camara)."""
        msgs = []
        if self._out is not None:
            try:
                import json
                line = self._out.readline()
                while line:
                    try:
                        got = json.loads(line)
                        if isinstance(got, dict):
                            msgs.append(got)
                            if got.get("err"):
                                self.error = str(got["err"])[:90]
                    except Exception:  # noqa: BLE001
                        pass
                    line = self._out.readline()
            except Exception:  # noqa: BLE001
                pass
        elif self._proc is not None and not self.alive():
            self.error = self.error or "la ventana 3D murio al arrancar"
        return msgs

    def poll_error(self) -> str:
        """Lo de siempre: leer y quedarse con el error."""
        self.drain()
        return self.error

    def stop(self) -> None:
        proc = self._proc
        self._proc = self._out = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=0.6)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":            # `python -m hrdash.glview` abre la ventana
    child_main()