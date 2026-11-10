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

VS = """#version 330
in vec3 in_position; in vec3 in_normal; in vec3 in_color; in float in_light;
out vec3 v_color; out vec3 v_normal; out float v_light;
uniform mat4 mvp;
void main(){
    gl_Position = mvp * vec4(in_position, 1.0);
    v_color = in_color;
    v_normal = in_normal;
    v_light = in_light;
}"""

FS = """#version 330
in vec3 v_color; in vec3 v_normal; in float v_light;
out vec4 o;
void main(){
    vec3 L = normalize(vec3(0.30, -0.55, 0.75));
    float d = max(dot(normalize(v_normal), L), 0.0);
    vec3 lit = v_color * (0.24 + 0.80 * d);
    vec3 glow = v_color * 1.22;
    o = vec4(mix(lit, glow, clamp(v_light, 0.0, 1.0)), 1.0);
}"""

FLOOR = (0.09, 0.10, 0.13)
VATTR = "3f 3f 3f 1f"                     # posicion, normal, color, emisivo
FIELDS = ("in_position", "in_normal", "in_color", "in_light")
GRID = (0.19, 0.22, 0.27)
ASPHALT = (0.17, 0.18, 0.21)
EDGE = (0.62, 0.66, 0.70)
META = (0.90, 0.92, 0.95)
KERB = (0.52, 0.20, 0.16)            # kerb rojo del borde interior
HALF_W = 5.0                         # ancho de pista: 8 calles de 1,22 m
TRACK_LEN = 400.0                    # perimetro del trazado (a escala real, 1 u = 1 m)
CHASE_DIST = 2.4                    # chase por defecto; la rueda acerca hasta primera persona
ZOOM_MIN, ZOOM_MAX = 0.20, 6.0
STEP_ZOOM = 0.18                    # salto por notch de rueda
ZOOMABLE = ("pista",)               # unicas vistas con camara configurable por rueda
TRAIL_T = 1.2                       # segundos de estela (se desvanece, no persigue al rider parado)
ZCOLORS = ["#6b7280", "#2f7bff", "#22c55e", "#facc15", "#f97316", "#ef4444"]

A0, SWEEP = 225.0, 270.0            # barrido del dial (igual que gauges.py)


def _hex(col) -> tuple[float, float, float]:
    """Hex '#rrggbb' -> triple 0..1 (falla a gris, nunca levanta)."""
    try:
        return (int(col[1:3], 16) / 255.0, int(col[3:5], 16) / 255.0, int(col[5:7], 16) / 255.0)
    except Exception:  # noqa: BLE001
        return (0.5, 0.5, 0.5)


def _dial(frac: float) -> float:
    """Angulo (rad) del valor fraccionado 0..1 sobre el arco de 270 grados."""
    return math.radians(A0 - SWEEP * max(0.0, min(1.0, frac)))


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

    def ribbon(self, pts, half, col, off=0.0, light=0.0) -> None:
        """Cinta continua sobre el trazado `pts` (x, y, tx, ty, bank por punto).

        El peralte es por punto (en un 400 m real solo las curvas estan inclinadas) y la
        altura de cada banda es `tan(bank) * (q + HALF_W)` con `q` la coordenada lateral
        medida desde el borde interior, que queda a 0. `off` desplaza la cinta a lo ancho
        (lineas y kerbs sobre el asfalto).
        """
        prev = None
        for (x, y, tx, ty, bank) in pts:
            ln = math.hypot(tx, ty) or 1.0
            nx, ny = ty / ln, -tx / ln                     # normal horizontal (exterior)
            tb = math.tan(bank)
            q_in, q_out = off - half, off + half
            a_in = (x + nx * q_in, y + ny * q_in, tb * (q_in + HALF_W))
            a_out = (x + nx * q_out, y + ny * q_out, tb * (q_out + HALF_W))
            cb, sb = math.cos(bank), math.sin(bank)
            if prev:
                self.quad(prev[0], prev[1], a_out, a_in, col, (-nx * sb, -ny * sb, cb), light)
            prev = (a_in, a_out)

    def floor(self, size=3.2, step=0.4, col=GRID, z0=0.0, lw=0.012) -> None:
        """Piso con grilla: un platano oscuro + lineas finas, apoyado en z0."""
        self.box(0, 0, z0 - 0.04, 2 * size, 2 * size, 0.06, FLOOR)
        k = max(1, int(2 * size / step))
        for i in range(k + 1):
            p = -size + i * (2 * size / k)
            self.box(p, 0, z0 - 0.005, lw, 2 * size, lw, col)
            self.box(0, p, z0 - 0.005, 2 * size, lw, lw, col)

    def merge(self, other: "Mesh") -> None:
        """Anexa otra malla (ya transformada): un solo VBO para todo el cluster."""
        self.v.extend(other.v)

    def segment(self, a, b, r, col, light=0.0) -> None:
        """Prisma de 4 caras entre dos puntos: un miembro (muslo, espinilla, brazo)
        o un tubo del cuadro. La seccion es un rombo, suficiente a esta escala."""
        d = [b[i] - a[i] for i in range(3)]
        ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
        d = [d[0] / ln, d[1] / ln, d[2] / ln]
        ref = (0.0, 0.0, 1.0) if abs(d[2]) < 0.9 else (0.0, 1.0, 0.0)
        u = [d[1] * ref[2] - d[2] * ref[1], d[2] * ref[0] - d[0] * ref[2], d[0] * ref[1] - d[1] * ref[0]]
        ul = math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]) or 1.0
        u = [u[0] / ul, u[1] / ul, u[2] / ul]
        v = [d[1] * u[2] - d[2] * u[1], d[2] * u[0] - d[0] * u[2], d[0] * u[1] - d[1] * u[0]]
        ring = [u, v, [-x for x in u], [-x for x in v]]
        for i in range(4):
            s1, s2 = ring[i], ring[(i + 1) % 4]
            a1 = tuple(a[j] + r * s1[j] for j in range(3))
            a2 = tuple(a[j] + r * s2[j] for j in range(3))
            b1 = tuple(b[j] + r * s1[j] for j in range(3))
            b2 = tuple(b[j] + r * s2[j] for j in range(3))
            n = [(s1[j] + s2[j]) / 2.0 for j in range(3)]
            self.quad(a1, b1, b2, a2, col, n, light)

    def wheel(self, c, r, fwd, up, lat, col, thick=0.02, segs=16, light=0.0) -> None:
        """Neumatico como banda cilindrica en el plano (avance, arriba): se lee desde
        la camara chase (de canto no vale una banda plana) y desde primera persona."""
        for i in range(segs):
            a0, a1 = 2 * math.pi * i / segs, 2 * math.pi * (i + 1) / segs
            def pt(a, o):
                return (c[0] + (fwd[0] * math.cos(a) + up[0] * math.sin(a)) * r + lat[0] * o,
                        c[1] + (fwd[1] * math.cos(a) + up[1] * math.sin(a)) * r + lat[1] * o,
                        c[2] + (fwd[2] * math.cos(a) + up[2] * math.sin(a)) * r + lat[2] * o)
            n = (fwd[0] * math.cos(a0) + up[0] * math.sin(a0),
                 fwd[1] * math.cos(a0) + up[1] * math.sin(a0),
                 fwd[2] * math.cos(a0) + up[2] * math.sin(a0))
            self.quad(pt(a0, -thick / 2.0), pt(a0, thick / 2.0),
                      pt(a1, thick / 2.0), pt(a1, -thick / 2.0), col, n, light)

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


def track(r=36.0, bank=math.radians(14.0), step=2.0):
    """Trazado de 400 m tipo Londres/Monaco: dos rectas y dos curvas, sentido antihorario
    (el de carrera), meta en el primer punto.

    Muestreado a paso fijo de arco para que la fraccion de vuelta sea distancia real, y
    con peralte solo en las curvas (rampa de entrada y salida; en rectas el peralte es 0).
    Cada punto: (x, y, tx, ty, bank). El ultimo punto repite el primero para cerrar.
    """
    L = (TRACK_LEN - 2 * math.pi * r) / 2.0
    half = L / 2.0
    ns, nc = max(2, int(round(L / step))), max(2, int(round(math.pi * r / step)))
    ds, dc = L / ns, math.pi * r / nc            # paso exacto de arco: la fraccion es distancia real

    def ramp(a):
        up = min(1.0, a / (0.25 * math.pi))
        down = min(1.0, (math.pi - a) / (0.25 * math.pi))
        return bank * min(up, down)

    pts = []
    for i in range(ns):                                   # recta de abajo
        pts.append((-half + i * ds, -r, 1.0, 0.0, 0.0))
    for i in range(nc):                                   # curva derecha
        a = -math.pi / 2 + math.pi * i / nc
        pts.append((half + r * math.cos(a), r * math.sin(a),
                    -math.sin(a), math.cos(a), ramp(math.pi * i / nc)))
    for i in range(ns):                                   # recta de arriba
        pts.append((half - i * ds, r, -1.0, 0.0, 0.0))
    for i in range(nc):                                   # curva izquierda
        a = math.pi / 2 + math.pi * i / nc
        pts.append((-half + r * math.cos(a), r * math.sin(a),
                    -math.sin(a), math.cos(a), ramp(math.pi * i / nc)))
    pts.append(pts[0])                                    # cierra el trazado
    return pts


def build_static(pts):
    """Lo que no cambia nunca: pista, lineas de calles, kerb, meta y suelo del estadio."""
    m = Mesh()
    m.floor(size=95, step=5.0, col=GRID, z0=-0.06, lw=0.06)
    m.ribbon(pts, HALF_W, ASPHALT)
    for q in (-HALF_W + 0.12, HALF_W - 0.12):             # lineas de las calles extrema
        m.ribbon(pts, 0.06, EDGE, off=q)
    m.ribbon(pts, 0.14, KERB, off=-HALF_W + 0.14)         # kerb del borde interior
    x, y, tx, ty, b = pts[0]
    nx, ny = ty, -tx                                      # a lo ancho en el punto de meta
    for i in range(10):                                   # meta cuadriculada, a traves
        q = -HALF_W + (i + 0.5)
        m.box(x + nx * q, y + ny * q, math.tan(b) * (q + HALF_W) + 0.02, 0.4, 1.0, 0.04,
              META if i % 2 == 0 else (0.05, 0.06, 0.08), 0.0, 0.25)
    return m


SKIN = (0.62, 0.49, 0.41)
FRAME = (0.15, 0.16, 0.19)
RIM = (0.24, 0.26, 0.30)
TIRE = (0.12, 0.13, 0.15)
SHOE = (0.11, 0.12, 0.14)
HELMET = (0.86, 0.89, 0.93)
# Proporciones del atleta y la bici en metros (el trazado esta a 1 unidad = 1 m)
WR, BB_H = 0.34, 0.27                                   # radio de rueda, altura del pedalier
REAR, FRONT = (-0.53, WR), (0.56, WR)                   # ejes (batalla ~1,09 m)
HTUBE, BAR = (0.40, 0.70), (0.47, 0.62)                 # direccion y manillar
HIP, SHO, HEAD = (-0.16, 0.78), (0.08, 1.06), (0.20, 1.22)   # postura inclinada de carrera
CRANK, THIGH, SHIN = 0.17, 0.43, 0.44
Q_RIDER = -3.0                          # linea del rider: 3 m dentro del eje de pista


def _knee(H, F, L1, L2):
    """Rodilla por IK de dos tramos en el plano sagital (s, h): (rodilla, pie)."""
    ds, dh = F[0] - H[0], F[1] - H[1]
    D = math.hypot(ds, dh) or 1e-6
    D = min(D, L1 + L2 - 1e-4)
    ca = max(-1.0, min(1.0, (L1 * L1 + D * D - L2 * L2) / (2 * L1 * D)))
    sa = math.sqrt(max(0.0, 1.0 - ca * ca))
    us, uh = ds / D, dh / D
    return (H[0] + L1 * (ca * us - sa * uh), H[1] + L1 * (sa * us + ca * uh))


def _rider(m, base, fwd, up, lat, theta, kit, pov=False) -> None:
    """Bici + atleta: cuadro, aros, pedalada (dos piernas), torso, brazos y casco.

    El marco local es (avance, altura, lateral) apoyado en la superficie peraltada.
    En primera persona (`pov`) solo se dibuja la bici, el pedal y las manos: el cuerpo
    quedaria entre la camara (en el casco) y el cuadro.
    """
    def P(s, h, w=0.0):
        return (base[0] + fwd[0] * s + up[0] * h + lat[0] * w,
                base[1] + fwd[1] * s + up[1] * h + lat[1] * w,
                base[2] + fwd[2] * s + up[2] * h + lat[2] * w)

    saddle = (HIP[0], HIP[1] + 0.05)

    for a, b, r in (((0.0, BB_H), saddle, 0.018), ((0.0, BB_H), HTUBE, 0.022),
                    (saddle, HTUBE, 0.015), (HTUBE, FRONT, 0.017),
                    ((0.0, BB_H), REAR, 0.015), (saddle, REAR, 0.012),
                    (HTUBE, BAR, 0.017)):
        m.segment(P(a[0], a[1]), P(b[0], b[1]), r, FRAME, 0.15)
    for c in (REAR, FRONT):
        m.wheel(P(c[0], c[1]), WR, fwd, up, lat, TIRE, 0.03, 16, 0.12)
        p = P(c[0], c[1])
        m.box(p[0], p[1], p[2], 0.06, 0.06, 0.06, RIM, 0.0, 0.2)

    for side in (1.0, -1.0):
        ph = theta if side > 0 else theta + math.pi
        foot = (CRANK * math.cos(ph), BB_H + CRANK * math.sin(ph))
        m.segment(P(0.0, BB_H, 0.06 * side), P(foot[0], foot[1], 0.06 * side), 0.014, RIM, 0.2)
        kn = _knee(HIP, foot, THIGH, SHIN)
        if not pov:
            m.segment(P(HIP[0], HIP[1], 0.06 * side), P(kn[0], kn[1], 0.07 * side), 0.065, SKIN, 0.2)
            m.segment(P(kn[0], kn[1], 0.07 * side), P(foot[0], foot[1], 0.075 * side), 0.048, SKIN, 0.2)
            m.segment(P(SHO[0], SHO[1], 0.07 * side), P(BAR[0], BAR[1], 0.05 * side), 0.040, SKIN, 0.2)
        p = P(foot[0], foot[1], 0.075 * side)
        m.box(p[0], p[1], p[2], 0.28, 0.11, 0.07, SHOE, 0.0, 0.15)              # zapatilla
        h = P(BAR[0], BAR[1], 0.05 * side)
        m.box(h[0], h[1], h[2], 0.11, 0.09, 0.06, SKIN, 0.0, 0.25)              # mano en el manillar
    if not pov:
        m.segment(P(HIP[0], HIP[1]), P(SHO[0], SHO[1]), 0.100, kit, 0.35)        # torso
        m.segment(P(SHO[0], SHO[1]), P(HEAD[0], HEAD[1]), 0.050, SKIN, 0.25)     # cuello
        p = P(HEAD[0], HEAD[1])
        m.box(p[0], p[1], p[2], 0.22, 0.19, 0.13, HELMET, 0.0, 0.3)


def scene_pista(snap, anim, aspect=1.6):
    """Pista de 400 m a escala real (1 unidad = 1 m): dos rectas, dos curvas peraltadas.

    Lo fijo (pista, lineas de calles, kerb, meta y suelo del estadio) se construye una
    vez y se conserva como capa estatica; por frame solo cambian el rider, la estela y
    la camara chase (1 persona, estilo juego de carreras, distancia configurable).
    """
    pts = anim.get("pts")
    if pts is None:
        pts = anim["pts"] = track()
    if anim.get("static") is None:
        anim["static"] = build_static(pts)
        layers = [("static", anim["static"])]
    else:
        layers = [("static", None)]                 # VBO vivo: la pista no se reconstruye
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
    anim["crank"] = anim["crank"] + (anim["c"] / 60.0) * 2 * math.pi * anim["dt"]

    col = _hex(snap.get("zone_color") or "#22d3ee")
    bx, by, btx, bty, bank = _at(pts, anim["frac"])
    bl = math.hypot(btx, bty) or 1.0
    fx, fy = btx / bl, bty / bl
    nx, ny = fy, -fx                                       # normal horizontal (exterior)
    cb, sb = math.cos(bank), math.sin(bank)
    up = (-nx * sb, -ny * sb, cb)                          # normal del plano inclinado
    lat = (nx * cb, ny * cb, sb)                           # lateral sobre la superficie
    fwd = (fx, fy, 0.0)
    base = (bx + nx * Q_RIDER, by + ny * Q_RIDER, math.tan(bank) * (Q_RIDER + HALF_W))
    # Estela por posiciones reales: al pararse deja de crecer y se desvanece (no avanza
    # por detrás de un rider que ya esta quieto).
    tr = anim["trail"]
    if not tr or math.hypot(base[0] - tr[-1][1][0], base[1] - tr[-1][1][1]) >= 0.05:
        tr.append((anim["t"], base))
    while tr and (anim["t"] - tr[0][0] > TRAIL_T or len(tr) > 24):
        tr.pop(0)
    for tt, p in tr:
        fade = max(0.12, 1.0 - (anim["t"] - tt) / TRAIL_T)
        m.box(p[0], p[1], p[2] + 0.02, 0.30, 0.30, 0.04,
              tuple(c * (0.15 + 0.85 * fade) for c in col))

    dist = min(ZOOM_MAX, max(ZOOM_MIN, float(anim.get("dist", CHASE_DIST))))
    pov = dist < 0.5
    _rider(m, base, fwd, up, lat, anim["crank"], col, pov=pov)
    # chase: camara alta y detras, mirando ligeramente hacia abajo sobre la bici;
    # al cerrar el zoom sube al casco (primera persona). Alturas medidas en `up`.
    k = min(1.0, max(0.0, (dist - ZOOM_MIN) / 0.6))
    # open angular: con 0,62 la bici quedaba fuera del cuadro a 2,4 m. En primera persona
    # hace falta mas angular y mas pitch para ver manillar y rueda delante de la mira.
    fovy = 1.00 + 0.30 * (1.0 - k)
    pitch = 0.50 - 0.35 * k
    eh = HEAD[1] + 0.03 + 0.35 * k
    ahead = max(4.0, dist * 1.9)
    s0 = HEAD[0] if pov else -dist
    eye = tuple(base[j] + fwd[j] * s0 + up[j] * eh for j in range(3))
    center = tuple(base[j] + fwd[j] * (s0 + ahead) + up[j] * (eh - ahead * pitch) for j in range(3))
    return layers + [("frame", m)], (eye, center, (0.0, 0.0, 1.0), fovy, 300.0)


def _at(pts, f):
    """Punto (x, y, tx, ty, bank) en la fraccion `f` del trazado, interpolado entre muestras:
    sin interpolacion el rider salta de indice en indice y la estela se corta."""
    n = len(pts) - 1
    i = min(n - 1, max(0, int(f * n)))
    k = f * n - i
    x0, y0, tx0, ty0, b0 = pts[i]
    x1, y1, _tx1, _ty1, _b1 = pts[i + 1]
    return (x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, tx0, ty0, b0)


SCENES = {"hr": scene_hr, "watts": scene_watts, "gauge": scene_gauge, "pista": scene_pista}


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
            "hr": 0.0, "w": 0.0, "v": 0.0, "c": 0.0, "dt": 0.0, "crank": 0.0,
            "trail": [], "pts": None, "static": None, "anchor": None}


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
        mons = [m for m in (glfw.get_monitors() or []) if m]
        zoom = {}                      # vista -> distancia de camara (la rueda la cambia)
        slots: list = []

        def view_under(win):
            """Vista bajo el cursor: en split hay varias vistas en la misma ventana."""
            mine = [s for s in slots if s[1] is win]
            if len(mine) == 1:
                return mine[0][0]
            try:
                px, py = glfw.get_cursor_pos(win)
                wpts, hpts = glfw.get_window_size(win)
                if not wpts or not hpts:
                    return mine[0][0]
                nx, ny = px / float(wpts), 1.0 - py / float(hpts)   # rects: origen abajo-izq
                for v, _w, r, _c, _p, _a, _va in mine:
                    if r[0] <= nx < r[0] + r[2] and r[1] <= ny < r[1] + r[3]:
                        return v
            except Exception:  # noqa: BLE001 - sin cursor: la primera vista de la ventana
                pass
            return mine[0][0] if mine else None

        def on_scroll(win, _dx, dy):
            """Rueda: acerca/aleja la camara de la vista apuntada (chase <-> 1a persona)."""
            v = view_under(win)
            if v is None or v not in ZOOMABLE:
                return
            z = min(ZOOM_MAX, max(ZOOM_MIN, zoom.get(v, CHASE_DIST) + STEP_ZOOM * float(dy)))
            if z != zoom.get(v, CHASE_DIST):
                zoom[v] = z
                _emit({"zoom": {v: round(z, 3)}})

        def open_windows(views):
            """(view, window, rect, ctx, prog, anim, vaos): una ventana por monitor o split."""
            if len(views) >= 2 and len(mons) >= len(views):
                for i, view in enumerate(views):
                    size, (xa, ya, wa, ha) = _window_for(glfw, mons[i], (1920, 1080))
                    win = glfw.create_window(size[0], size[1], f"HRDash 3D - {view}", mons[i], None)
                    if win is None:
                        raise RuntimeError("glfw no pudo crear la ventana")
                    try:                                   # centrada en el monitor que la aloja
                        glfw.set_window_pos(win, int(xa + (wa - size[0]) / 2),
                                            int(ya + (ha - size[1]) / 2))
                    except Exception:  # noqa: BLE001 - centrar es decorativo
                        pass
                    glfw.make_context_current(win)
                    glfw.set_scroll_callback(win, on_scroll)      # rueda: zoom de la vista
                    # con dos ventanas el vsync se pagaria dos veces por frame: aqui no esperamos
                    glfw.swap_interval(0)
                    # contexto por ventana: los objetos (program/VAO) pertenecen a SU contexto
                    ctx = moderngl.create_context()
                    _emit({"win": list(size), "fb": list(glfw.get_framebuffer_size(win)),
                           "view": view, "rect": [0.0, 0.0, 1.0, 1.0]})
                    yield (view, win, (0.0, 0.0, 1.0, 1.0), ctx,
                           ctx.program(vertex_shader=VS, fragment_shader=FS), _new_anim(), {})
            else:
                size, mon = _plan_window(glfw, (1920, 1080))
                win = glfw.create_window(size[0], size[1], "HRDash 3D", mon, None)
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
                glfw.set_scroll_callback(win, on_scroll)          # rueda: zoom de la vista apuntada
                glfw.swap_interval(1)
                ctx = moderngl.create_context()
                prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
                rects = [(view, _split_rect(view, i, len(views))) for i, view in enumerate(views)]
                _emit({"win": list(size), "fb": list(glfw.get_framebuffer_size(win)),
                       "view": [v for v, _r in rects],
                       "rect": [list(r) for _v, r in rects]})
                for view, r in rects:
                    yield (view, win, r, ctx, prog, _new_anim(), {})

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
                for v, z in (snap.get("zoom") or {}).items():   # zoom fijado desde fuera
                    if v in ZOOMABLE and isinstance(z, (int, float)) and zoom.get(v) != float(z):
                        zoom[v] = min(ZOOM_MAX, max(ZOOM_MIN, float(z)))
            views = [v for v in (snap.get("views") or [snap.get("view", "gauge")]) if v in SCENES]
            if not views:
                break
            shape = (tuple(views), len(mons))
            if shape != key:
                key = shape
                for view, win, _r, ctx, _p, _a, vaos in slots:   # release con su contexto vigente
                    try:
                        glfw.make_context_current(win)
                        for va in vaos.values():
                            va.release()
                    except Exception:  # noqa: BLE001
                        pass
                for w in _uniq(s[1] for s in slots):      # window objects: no hashables
                    try:
                        glfw.destroy_window(w)
                    except Exception:  # noqa: BLE001
                        pass
                slots = list(open_windows(views))
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
                layers, cam = SCENES[view](snap, anim, max(0.25, vw / vh))
                for name, mesh in layers:
                    if mesh is None:
                        continue                       # capa sin cambios: conserva su VBO
                    old = vaos.get(name)
                    if old is not None:
                        old.release()
                    buf = ctx.buffer(np.asarray(mesh.v or [0.0] * 10, dtype="f4").tobytes())
                    vaos[name] = ctx.vertex_array(prog, [(buf, VATTR, *FIELDS)])
                eye, center, up, fovy = cam[:4]
                far = cam[4] if len(cam) > 4 else 80.0     # la pista necesita mas fondo
                mvp = perspective(fovy, vw / vh, 0.1, far) @ look_at(eye, center, up)
                prog["mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4").tobytes())
                for va in vaos.values():
                    va.render(moderngl.TRIANGLES)
                glfw.swap_buffers(win)
            glfw.poll_events()
            frames += 1
            if frames >= 60:
                now = time.time()
                _emit({"fps": round(frames / max(1e-6, now - report_t), 1),
                       "ms": round(1000 * work / max(1, frames), 1),
                       "dist": {v: round(z, 3) for v, z in zoom.items()}})
                frames, report_t, work = 0, now, 0.0
            work += time.time() - t0
            time.sleep(max(0.0, 0.02 - (time.time() - t0)))
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

    def poll_error(self) -> str:
        """Lee sin bloquear lo que la ventana haya reportado por stdout."""
        if self._out is not None:
            try:
                import json
                line = self._out.readline()
                while line:
                    try:
                        got = json.loads(line)
                        if isinstance(got, dict) and got.get("err"):
                            self.error = str(got["err"])[:90]
                    except Exception:  # noqa: BLE001
                        pass
                    line = self._out.readline()
            except Exception:  # noqa: BLE001
                pass
        elif self._proc is not None and not self.alive():
            self.error = self.error or "la ventana 3D murio al arrancar"
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