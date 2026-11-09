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
in vec3 in_position; in vec3 in_normal; in vec3 in_color;
out vec3 v_color; out vec3 v_normal;
uniform mat4 mvp;
void main(){
    gl_Position = mvp * vec4(in_position, 1.0);
    v_color = in_color;
    v_normal = in_normal;
}"""

FS = """#version 330
in vec3 v_color; in vec3 v_normal;
out vec4 o;
void main(){
    vec3 L = normalize(vec3(0.35, 0.55, 0.75));
    float d = max(dot(normalize(v_normal), L), 0.0);
    o = vec4(v_color * (0.26 + 0.85 * d), 1.0);
}"""

FLOOR = (0.09, 0.10, 0.13)
GRID = (0.19, 0.22, 0.27)
ASPHALT = (0.17, 0.18, 0.21)
EDGE = (0.62, 0.66, 0.70)
META = (0.90, 0.92, 0.95)
STEEL = (0.93, 0.95, 0.98)
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
    """Triangulos (posicion, normal, color) en una lista plana de floats."""

    __slots__ = ("v",)

    def __init__(self):
        self.v: list[float] = []

    def tri(self, a, b, c, col, n=None) -> None:
        if n is None:
            ux, uy, uz = (b[i] - a[i] for i in range(3))
            vx, vy, vz = (c[i] - a[i] for i in range(3))
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            n = (nx / ln, ny / ln, nz / ln)
        for p in (a, b, c):
            self.v.extend((p[0], p[1], p[2], n[0], n[1], n[2], col[0], col[1], col[2]))

    def quad(self, a, b, c, d, col, n=None) -> None:
        if n is None:
            ux, uy, uz = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
            vx, vy, vz = (d[0] - a[0], d[1] - a[1], d[2] - a[2])
            nx, ny, nz = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            n = (nx / ln, ny / ln, nz / ln)
        self.tri(a, b, c, col, n)
        self.tri(a, c, d, col, n)

    def box(self, cx, cy, cz, sx, sy, sz, col, yaw=0.0) -> None:
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
            self.quad(*(tp(*c) for c in cs), col, (nx, ny, n[2]))

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

    def floor(self, size=3.2, step=0.4, col=GRID) -> None:
        """Piso con grilla: un platano oscuro + lineas finas."""
        self.box(0, 0, -0.04, 2 * size, 2 * size, 0.06, FLOOR)
        k = max(1, int(2 * size / step))
        for i in range(k + 1):
            p = -size + i * (2 * size / k)
            self.box(p, 0, -0.005, 0.012, 2 * size, 0.012, col)
            self.box(0, p, -0.005, 2 * size, 0.012, 0.012, col)

    @property
    def tris(self) -> int:
        return len(self.v) // 54


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


def orbit(t, radius, height, speed=0.25, phase=0.0, center=(0.0, 0.0, 0.5)):
    """Camara orbital lenta: devuelve (eye, center, up, fovy)."""
    a = t * speed + phase
    eye = (math.sin(a) * radius, -math.cos(a) * radius, height)
    return eye, center, (0.0, 0.0, 1.0), 0.85


# ---------------------------------------------------------------- escenas
def scene_hr(snap, anim) -> tuple[Mesh, tuple]:
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
    return m, orbit(anim["t"], 5.6, 2.9, 0.18, center=(0.0, 0.0, 0.8))


def scene_watts(snap, anim) -> tuple[Mesh, tuple]:
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
    return m, orbit(anim["t"], 5.9, 3.2, 0.24, phase=0.6, center=(0.0, 0.0, 0.7))


def scene_gauge(snap, anim) -> tuple[Mesh, tuple]:
    """Dial 3D: aro de balizas por zona, cubo central y aguja con inercia."""
    m = Mesh()
    lo, hi = 40.0, max(60.0, float(snap.get("hrmax") or 190) + 10.0)
    segs = snap.get("hr_segs") or []
    zs = snap.get("zcolors") or ZCOLORS
    m.box(0.0, 0.0, -0.10, 3.7, 3.7, 0.16, (0.12, 0.13, 0.16))
    for i in range(61):
        f = i / 60.0
        a = _dial(f)
        col = (0.24, 0.27, 0.31)
        for k, zc in enumerate(zs):
            z0, z1 = _zone_fracs(k, snap)
            if z0 <= f < z1:
                col = _hex(zc)
                break
        if segs:
            for f0, f1, chex in segs:
                if f0 <= f <= f1:
                    col = _hex(chex)
                    break
        m.box(math.cos(a) * 1.55, math.sin(a) * 1.55, 0.02, 0.12, 0.12, 0.14, col, yaw=a)
    zc = _hex(snap.get("zone_color") or "#ef4444")
    m.box(0.0, 0.0, 0.14, 0.40, 0.40, 0.26, zc)
    hr = float(snap.get("hr") or 0)
    anim["needle"] += (hr - anim["needle"]) * 0.14          # inercia, como en la TUI
    a = _dial((anim["needle"] - lo) / max(1e-6, hi - lo))
    ln = 1.22
    m.box(math.cos(a) * ln * 0.45, math.sin(a) * ln * 0.45, 0.30,
          ln, 0.10, 0.07, STEEL, yaw=a)
    return m, orbit(anim["t"], 4.6, 3.0, 0.14, center=(0.0, 0.0, 0.35))


def _zone_fracs(k, snap):
    """Fracciones (0..1) que ocupa la zona k del historial de FC sobre el dial."""
    hrmax = max(60.0, float(snap.get("hrmax") or 190))
    pcts = snap.get("zone_pcts")
    if not pcts or k >= len(pcts):
        return (1.1, 1.1)
    lo, hi = 40.0, hrmax + 10.0
    a, b = pcts[k]
    b = 100.0 if b is None else float(b)
    f0 = max(0.0, min(1.0, (a / 100.0 * hrmax - lo) / (hi - lo)))
    f1 = max(0.0, min(1.0, (min(b, 100.0) / 100.0 * hrmax - lo) / (hi - lo)))
    return f0, f1


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


def scene_pista(snap, anim) -> tuple[Mesh, tuple]:
    """Velodromo con peralte, meta cuadriculada, estela y marcador de la bici."""
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
        m.box(x, y, 0.09 + k * 0.004, 0.12, 0.12, 0.14,
              tuple(c * (1.0 - k / 14.0) for c in col))
    x, y, tx, ty = pts[min(n, max(0, int(anim["frac"] * n)))]
    m.box(x, y, 0.34, 0.40, 0.22, 0.34, col, yaw=math.atan2(ty, tx))
    return m, orbit(anim["t"], 5.2, 3.4, 0.16, center=(0.0, 0.0, 0.25))


def _at(pts, f):
    i = min(len(pts) - 1, max(0, int(f * (len(pts) - 1))))
    return pts[i][0], pts[i][1]


SCENES = {"hr": scene_hr, "watts": scene_watts, "gauge": scene_gauge, "pista": scene_pista}


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
        win = glfw.create_window(900, 580, "HRDash 3D", None, None)
        if not win:
            raise RuntimeError("glfw no pudo crear la ventana")
        glfw.make_context_current(win)
        glfw.swap_interval(1)
        ctx = moderngl.create_context()
        prog = ctx.program(vertex_shader=VS, fragment_shader=FS)
        ctx.enable(moderngl.DEPTH_TEST)
        feed = _stdin_feed()
        vao = None
        snap = {"view": "hr", "hr": 0, "hrmax": 180, "samples": [], "zone_color": "#ef4444"}
        anim = {"t": 0.0, "needle": 0.0, "frac": 0.0, "last": 0.0}
        clock = time.time()
        while not glfw.window_should_close(win):
            t0 = time.time()
            anim["t"] = t0 - clock
            keep, got = feed()
            if not keep:
                break                                       # el dashboard cerro la tuberia
            if got is not None:
                snap = got
            fn = SCENES.get(snap.get("view", "hr"), scene_hr)
            mesh, cam = fn(snap, anim)
            data = np.asarray(mesh.v or [0.0] * 54, dtype="f4")
            if vao is not None:
                vao.release()
            vbo = ctx.buffer(data.tobytes())
            vao = ctx.vertex_array(prog, [(vbo, "3f 3f 3f",
                                           "in_position", "in_normal", "in_color")])
            eye, center, up, fovy = cam
            w, h = glfw.get_framebuffer_size(win)
            mvp = perspective(fovy, max(0.25, w / max(1, h))) @ look_at(eye, center, up)
            prog["mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4").tobytes())
            ctx.clear(*FLOOR, 1.0)
            vao.render(moderngl.TRIANGLES)
            glfw.swap_buffers(win)
            glfw.poll_events()
            time.sleep(max(0.0, 0.02 - (time.time() - t0)))
        if vao is not None:
            vao.release()
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