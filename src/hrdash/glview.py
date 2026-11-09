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

    def ribbon(self, pts, half, bank, col, off=0.0) -> None:
        """Cinta continua sobre el trazado `pts` (x,y,tx,ty): peralte + desplazamiento.

        `bank` en radianes inclina la seccion (como un velodromo); `off` desplaza la
        cinta a lo ancho (para pintar los bordes sobre el asfalto).
        """
        prev = None
        cb, sb = math.cos(bank), math.sin(bank)
        for (x, y, tx, ty) in pts:
            ln = math.hypot(tx, ty) or 1.0
            nx, ny = -ty / ln, tx / ln                     # normal horizontal (exterior)
            ox = (nx * cb) * half + nx * off
            oy = (ny * cb) * half + ny * off
            outer = (x + ox, y + oy, sb * half)
            inner = (x - ox, y - oy, sb * half)
            if prev:
                self.quad(prev[0], prev[1], outer, inner, col, (0.0, 0.0, 1.0))
            prev = (inner, outer)

    def floor(self, size=3.2, step=0.4, col=GRID, z0=0.0) -> None:
        """Piso con grilla: un platano oscuro + lineas finas, apoyado en z0."""
        self.box(0, 0, z0 - 0.04, 2 * size, 2 * size, 0.06, FLOOR)
        k = max(1, int(2 * size / step))
        for i in range(k + 1):
            p = -size + i * (2 * size / k)
            self.box(p, 0, z0 - 0.005, 0.012, 2 * size, 0.012, col)
            self.box(0, p, z0 - 0.005, 2 * size, 0.012, 0.012, col)

    def merge(self, other: "Mesh") -> None:
        """Anexa otra malla (ya transformada): un solo VBO para todo el cluster."""
        self.v.extend(other.v)

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


def oval(cx, cy, steps=30):
    """Trazado del velodromo (dos rectas + dos semicirculos), sentido antihorario."""
    r = cy
    pts = []
    for i in range(steps + 1):
        pts.append((-cx + 2 * cx * i / steps, -cy, 1.0, 0.0))
    for i in range(1, steps + 1):
        a = -math.pi / 2 + math.pi * i / steps
        pts.append((cx + r * math.cos(a), r * math.sin(a), math.cos(a), math.sin(a)))
    for i in range(steps + 1):
        pts.append((cx - 2 * cx * i / steps, cy, -1.0, 0.0))
    for i in range(1, steps + 1):
        a = math.pi / 2 + math.pi * i / steps
        pts.append((-cx + r * math.cos(a), r * math.sin(a), math.cos(a), math.sin(a)))
    return pts


def scene_pista(snap, anim, aspect=1.6):
    """Velodromo con peralte, meta cuadriculada, estela y marcador de la bici.

    Cámara **chase** (1 persona, estilo juego de carreras): detras de la bici, mirando
    hacia adelante, con la distancia configurable por el piloto.
    """
    m = Mesh()
    cx, cy = 2.15, 1.30
    pts = oval(cx, cy, 26)
    m.box(0, 0, -0.14, 2 * (cx + cy) + 1.6, 2 * (cy + 1.2) + 1.6, 0.10, FLOOR)
    m.ribbon(pts, 0.46, math.radians(26.0), ASPHALT)
    m.ribbon(pts, 0.06, math.radians(26.0), EDGE, off=0.42)
    m.ribbon(pts, 0.05, math.radians(26.0), (0.30, 0.33, 0.38), off=-0.42)
    for i in range(6):                                        # meta en la recta de arriba
        m.box(0.0, -cy + (i - 2.5) * 0.16, 0.06, 0.9, 0.15, 0.05,
              META if i % 2 == 0 else (0.05, 0.06, 0.08))
    lap_m = max(100, int(snap.get("lap_m") or 5000))
    spd = float(snap.get("speed") or 0.0)
    frac = float(snap.get("lap_frac") or 0.0)
    anim["dt"] = min(0.2, max(0.0, anim["t"] - anim.get("last", 0.0))) or 0.02
    anim["last"] = anim["t"]
    anim["frac"] = (anim.get("frac", frac) + (spd / 3.6) * anim["dt"] / lap_m) % 1.0
    if abs(anim["frac"] - frac) > 0.3:                          # vuelta cerrada: no rebobinar
        anim["frac"] = frac
    col = _hex(snap.get("zone_color") or "#22d3ee")
    n = len(pts) - 1
    for k in range(9, 0, -1):
        f = (anim["frac"] - k * 0.007) % 1.0
        x, y = _at(pts, f)
        # streak fino sobre el asfalto (no columna: desde la camara tapa el cuadro)
        m.box(x, y, 0.025, 0.14, 0.14, 0.03, tuple(c * (1.0 - k / 14.0) for c in col))
    idx = min(n, max(0, int(anim["frac"] * n)))
    bx, by, btx, bty = pts[idx]
    bl = math.hypot(btx, bty) or 1.0
    fx, fy = btx / bl, bty / bl
    yaw = math.atan2(bty, btx)
    # la bici: bicho + bulto del ciclista, para que se lea como un rider
    m.box(bx, by, 0.14, 0.50, 0.22, 0.20, col, yaw=yaw, light=0.4)
    m.box(bx + fx * 0.10, by + fy * 0.10, 0.34, 0.20, 0.16, 0.22, (0.85, 0.88, 0.92),
          yaw=yaw, light=0.3)
    # chase: camara alta y detras, mirando ligeramente hacia abajo sobre la bici
    dist = 2.4
    eye = (bx - fx * dist, by - fy * dist, 0.55)
    center = (bx + fx * 1.6, by + fy * 1.6, 0.22)
    return [("frame", m)], (eye, center, (0.0, 0.0, 1.0), 0.62)


def _at(pts, f):
    i = min(len(pts) - 1, max(0, int(f * (len(pts) - 1))))
    return pts[i][0], pts[i][1]


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


# ---------------------------------------------------------------- proceso hijo
def child_main() -> None:
    """Entrada del proceso hijo (`python -m hrdash.glview`): ventana GLFW + render.

    Habla JSON por stdio: el dashboard escribe un snapshot por linea (stdin) y la
    ventana reporta errores por stdout. Evitamos `multiprocessing` porque con `spawn`
    macOS re-ejecuta el entry point de consola y el hijo arrancaria otro dashboard.
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
        size, mon = _plan_window(glfw, (1920, 1080))
        win = glfw.create_window(size[0], size[1], "HRDash 3D", None, None)
        if win is None:
            raise RuntimeError("glfw no pudo crear la ventana")
        if mon is not None:
            try:                                   # centrada en el monitor que la aloja
                xa, ya, wa, ha = glfw.get_monitor_workarea(mon)
                glfw.set_window_pos(win, int(xa + (wa - size[0]) / 2),
                                    int(ya + (ha - size[1]) / 2))
            except Exception:  # noqa: BLE001 - centrar es decorativo
                pass
        fbw, fbh = glfw.get_framebuffer_size(win)
        _emit({"win": list(size), "fb": [fbw, fbh]})   # puntos reales y pixels del buffer
        glfw.make_context_current(win)
        glfw.swap_interval(1)
        ctx = moderngl.create_context()
        prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
        ctx.enable(moderngl.DEPTH_TEST)
        feed = _stdin_feed()
        vaos: dict = {}
        snap = {"view": "hr", "hr": 0, "hrmax": 180, "samples": [], "zone_color": "#ef4444"}
        anim = {"t": 0.0, "needle": 0.0, "frac": 0.0, "last": 0.0,
                "hr": 0.0, "w": 0.0, "v": 0.0, "c": 0.0}
        clock = time.time()
        last_view, frames, report_t, work = None, 0, time.time(), 0.0
        while not glfw.window_should_close(win):
            t0 = time.time()
            anim["t"] = t0 - clock
            keep, got = feed()
            if not keep:
                break                                       # el dashboard cerro la tuberia
            if got is not None:
                snap = got
            w, h = glfw.get_framebuffer_size(win)
            aspect = max(0.25, w / max(1, h))
            view = snap.get("view", "hr")
            fn = SCENES.get(view, scene_hr)
            if view != last_view:                            # otra vista: tira las capas
                last_view = view
                for v in vaos.values():
                    v.release()
                vaos.clear()
            dyn_layers, cam = fn(snap, anim, aspect)
            for name, mesh in dyn_layers:
                if mesh is None:
                    continue                       # capa sin cambios: conserva su VBO
                old = vaos.get(name)
                if old is not None:
                    old.release()
                buf = ctx.buffer(np.asarray(mesh.v or [0.0] * 10, dtype="f4").tobytes())
                vaos[name] = ctx.vertex_array(prog, [(buf, VATTR, *FIELDS)])
            eye, center, up, fovy = cam
            mvp = perspective(fovy, aspect) @ look_at(eye, center, up)
            prog["mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4").tobytes())
            ctx.clear(*FLOOR, 1.0)
            for v in vaos.values():
                v.render(moderngl.TRIANGLES)
            glfw.swap_buffers(win)
            glfw.poll_events()
            frames += 1
            if frames >= 60:
                now = time.time()
                _emit({"fps": round(frames / max(1e-6, now - report_t), 1),
                       "ms": round(1000 * work / max(1, frames), 1)})
                frames, report_t, work = 0, now, 0.0
            work += time.time() - t0
            time.sleep(max(0.0, 0.02 - (time.time() - t0)))
        for v in list(vaos.values()):
            v.release()
        vaos.clear()
        glfw.destroy_window(win)
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