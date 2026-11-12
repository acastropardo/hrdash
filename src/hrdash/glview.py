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

FS = """#version 330
in vec3 v_color; in vec3 v_normal; in float v_light; in vec3 v_world;
out vec4 o;
uniform vec3 EYE;
void main(){
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
    o = vec4(mix(col, vec3(0.052, 0.060, 0.075), fog), 1.0);
}"""

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
# Tono por tablon: la variacion es pequeña y constante a lo largo del tablon. Con variacion
# por cuadro la pista sale en "ladrillos" (parquet), no en tablones largos.
WOODS = [WOOD, (0.761, 0.660, 0.514), (0.708, 0.610, 0.471), (0.782, 0.678, 0.533),
         (0.745, 0.645, 0.502), (0.694, 0.598, 0.463), (0.754, 0.652, 0.508),
         (0.720, 0.622, 0.482), (0.768, 0.666, 0.520), (0.715, 0.616, 0.476)]
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

HALF_W = 5.0                         # ancho de pista: 8 calles de 1,22 m
TRACK_LEN = 400.0                    # perimetro del trazado (a escala real, 1 u = 1 m)
TRACK_W = 10.0                       # 0..10 es la coordenada lateral desde el borde interior
DECK = 1.05                          # el tablon esta elevado sobre el piso del recinto
PLANK = 0.24                         # ancho del tablon (junta en el sentido de marcha)
WALL_H = 0.90                        # muro de seguridad sobre el borde exterior
STAND_ROWS, STAND_RUN, STAND_RISE = 11, 0.85, 0.40      # gradas escalonadas
ROOF_H, LAMP_H = 13.5, 6.5           # altura del cercho y de las lamparas colgadas
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
    """Punto de la superficie a la distancia lateral `d` (medida desde el borde interior).

    Devuelve (posicion, normal_de_superficie, normal_horizontal). La altura es
    `DECK + tan(bank) * d`: `DECK` es el nivel del tablon sobre el piso del recinto, por eso
    el muro se ve desde la pista; `bank` es por punto, así que en las rectas sale plano.

    `lift` levanta unos milimetros la banda: las lineas pintadas y los tablones se apoyan
    sobre la capa base, y en el mismo plano el z-buffer elige al azar (z-fighting), que es
    lo que salia como manchas.
    """
    x, y, tx, ty, bank = pt
    ln = math.hypot(tx, ty) or 1.0
    nx, ny = ty / ln, -tx / ln
    cb, sb = math.cos(bank), math.sin(bank)
    p = (x + nx * (d - HALF_W), y + ny * (d - HALF_W), DECK + lift + math.tan(bank) * d)
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

    def spoke(self, c, r, fwd, up, lat, ang, col, w=0.014, light=0.0) -> None:
        """Radio de rueda en el plano (avance, arriba): sin radios la rueda es un aro negro
        y desde la camara no se lee que gira."""
        dirv = [fwd[i] * math.cos(ang) + up[i] * math.sin(ang) for i in range(3)]
        perp = [dirv[1] * lat[2] - dirv[2] * lat[1], dirv[2] * lat[0] - dirv[0] * lat[2],
                dirv[0] * lat[1] - dirv[1] * lat[0]]
        pl = math.sqrt(perp[0] * perp[0] + perp[1] * perp[1] + perp[2] * perp[2]) or 1.0
        perp = [perp[0] / pl * w / 2.0, perp[1] / pl * w / 2.0, perp[2] / pl * w / 2.0]
        h = [c[i] + dirv[i] * 0.05 for i in range(3)]
        t = [c[i] + dirv[i] * (r - 0.015) for i in range(3)]
        self.quad(tuple(h[i] + perp[i] for i in range(3)), tuple(t[i] + perp[i] for i in range(3)),
                  tuple(t[i] - perp[i] for i in range(3)), tuple(h[i] - perp[i] for i in range(3)),
                  col, tuple(lat), light)

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


def track(r=36.0, bank=math.radians(14.0), step=1.0):
    """Trazado de 400 m tipo Londres/Monaco: dos rectas y dos curvas, sentido antihorario
    (el de carrera), meta al final de la recta de meta (el punto que cierra la vuelta).

    Muestreado a paso de 1 m para que la fraccion de vuelta sea distancia real y para que
    las bandas estrechas (lineas pintadas y tablones) no serruchen en las curvas: con paso
    de 2 m cada linea es un poligono y la junta se ve escalonada.

    El peralte es solo en las curvas, con rampa de entrada y salida. La rampa no es lineal:
    la transicion recta->peralte (el "flow" del que hablan los constructores) se suaviza con
    coseno, que es lo que hace que la pista parezca construida y no extruida.
    Cada punto: (x, y, tx, ty, bank). El ultimo punto repite el primero para cerrar.
    """
    L = (TRACK_LEN - 2 * math.pi * r) / 2.0
    half = L / 2.0
    ns, nc = max(2, int(round(L / step))), max(2, int(round(math.pi * r / step)))
    ds, dc = L / ns, math.pi * r / nc            # paso exacto de arco: la fraccion es distancia real
    T = 0.25 * math.pi                           # tramo de transicion del peralte

    def ramp(a):
        t = min(min(1.0, a / T), min(1.0, (math.pi - a) / T))
        return bank * 0.5 * (1.0 - math.cos(math.pi * t))

    pts = []
    for i in range(nc):                                   # curva derecha (empezamos en meta)
        a = -math.pi / 2 + math.pi * i / nc
        pts.append((half + r * math.cos(a), r * math.sin(a),
                    -math.sin(a), math.cos(a), ramp(math.pi * i / nc)))
    for i in range(ns):                                   # recta de arriba
        pts.append((half - i * ds, r, -1.0, 0.0, 0.0))
    for i in range(nc):                                   # curva izquierda
        a = math.pi / 2 + math.pi * i / nc
        pts.append((-half + r * math.cos(a), r * math.sin(a),
                    -math.sin(a), math.cos(a), ramp(math.pi * i / nc)))
    for i in range(ns):                                   # recta de meta, acaba en el punto 0
        pts.append((-half + i * ds, -r, 1.0, 0.0, 0.0))
    pts.append(pts[0])                                    # cierra el trazado
    return pts


def _warm(col, t):
    """Color multiplicado por la calidez del charco de luz (recortado a 1)."""
    return tuple(min(1.0, v * t) for v in col)


def _lamps(pts):
    """Lamparas colgadas del cercho: posiciones (x, y, z), una cada 6 m de cuerda."""
    return [(_surf(pt, HALF_W)[0][0], _surf(pt, HALF_W)[0][1], LAMP_H)
            for pt in pts[:-1][::6]]


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

    # Tablon de pino: la junta va en el sentido de marcha, que es como se lee desde la chase.
    # Primero la base (mas oscura) y encima cada tablón algo mas claro, dejando 1 cm de junta:
    # sin esa junta la pista se lee como una superficie pintada a rayas, no como madera.
    # La base va partida por tablon, no como una sola cinta de 10 m: en la rampa de peralte
    # el quad de 10 m se tuerce entre muestras, su diagonal se separa varios cm de la
    # superficie real y asomaba entre tablones y lineas como un escalonado.
    for i in range(math.ceil(TRACK_W / PLANK)):
        d0, d1 = i * PLANK, min(TRACK_W, (i + 1) * PLANK)    # el ultimo se recorta al borde
        off, half = (d0 + d1) / 2.0 - HALF_W, (d1 - d0) / 2.0
        m.ribbon(pts, half, WOOD_D, off=off, tint=warm)
        m.ribbon(pts, half - 0.012, WOODS[i % len(WOODS)], off=off, tint=warm, lift=0.004)

    # Apron: el hormigon pintado del interior, en el nivel del tablon. No sigue el peralte
    # (es zona llana), por eso no se puede usar ribbon, que inclina con bank.
    for i in range(n):
        p0, _n0, h0 = _surf(pts[i], 0.0)
        p1, _n1, h1 = _surf(pts[i + 1], 0.0)
        a0 = (p0[0] - h0[0] * 1.8, p0[1] - h0[1] * 1.8, DECK)
        a1 = (p1[0] - h1[0] * 1.8, p1[1] - h1[1] * 1.8, DECK)
        cs = (_warm(APRON, warm(a0)), _warm(APRON, warm(a1)), _warm(APRON, warm(p1)), _warm(APRON, warm(p0)))
        m.vquad(a0, a1, p1, p0, cs, (0.0, 0.0, 1.0))

    # Lineas pintadas, medidas desde el borde interior: cote d'azur, linea negra (la de
    # referencia, por la que se rueda), roja del sprinter, azul del stayer y borde exterior.
    for d, col, w in ((0.10, AZUR, 0.20), (0.24, LINE_BLACK, 0.05), (0.94, LINE_RED, 0.05),
                      (4.30, LINE_BLUE, 0.05), (TRACK_W - 0.05, LINE_WHITE, 0.05)):
        m.ribbon(pts, w / 2.0, col, off=d - HALF_W, light=0.14, tint=warm, lift=0.010)

    # Marcas transversales UCI. La meta no es un damero: es una banda blanca con una linea
    # negra fina en el centro, en el punto que cierra la vuelta. La linea de 200 m (blanca)
    # marca el tramo lanzado, y las de persecucion (rojas, medio ancho desde el borde
    # interior) estan a mitad de cada recta.
    _cross(m, pts, 0.0, META_W, META, 0.0, TRACK_W, warm, 0.012)
    _cross(m, pts, 0.0, LINE_W, LINE_BLACK, 0.0, TRACK_W, warm, 0.014)
    _cross(m, pts, -200.0, LINE_W, META, 0.0, TRACK_W, warm, 0.012)
    for s_mid in _straight_mids(pts):
        _cross(m, pts, s_mid, LINE_W, LINE_RED, 0.0, TRACK_W / 2.0, warm, 0.012)

    _structure(m, pts, warm)
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
        straight = i < n and pts[i][4] == 0.0 and pts[i][2:4] == pts[(i + 1) % n][2:4]
        if straight and start is None:
            start = i
        elif not straight and start is not None:
            runs.append((start + i) / 2.0 * TRACK_LEN / n)
            start = None
    return runs


def _structure(m, pts, warm) -> None:
    """Muro perimetral y muro de seguridad: el tablon esta elevado sobre el piso del recinto.

    Sin esta pieza la pista flota sobre un vacio negro, que es lo que hace que el render
    parezca bajo aunque las proporciones sean correctas.
    """
    n = len(pts) - 1
    for i in range(n):
        p0, nrm0, h0 = _surf(pts[i], 0.0)
        p1, nrm1, h1 = _surf(pts[i + 1], 0.0)
        g0 = (p0[0] - h0[0] * 0.45, p0[1] - h0[1] * 0.45, 0.0)
        g1 = (p1[0] - h1[0] * 0.45, p1[1] - h1[1] * 0.45, 0.0)
        m.quad(g0, g1, p1, p0, _warm(WALL, warm(g0)), (-h0[0], -h0[1], 0.0))
        t0, tnrm, ht0 = _surf(pts[i], TRACK_W)
        t1, _, ht1 = _surf(pts[i + 1], TRACK_W)
        s0 = (t0[0] + ht0[0] * 0.30, t0[1] + ht0[1] * 0.30, t0[2] + WALL_H)
        s1 = (t1[0] + ht1[0] * 0.30, t1[1] + ht1[1] * 0.30, t1[2] + WALL_H)
        m.quad(t0, t1, s1, s0, _warm(WALL, warm(t0)), tnrm)


def _stands(m, pts, warm) -> None:
    """Gradas: filas escalonadas alrededor del borde exterior, con butacas cada 2 m de cuerda."""
    n = len(pts) - 1

    def row(pt, r):
        p, _n, h = _surf(pt, TRACK_W)
        k = 0.6 + r * STAND_RUN
        return (p[0] + h[0] * k, p[1] + h[1] * k, p[2] + WALL_H + r * STAND_RISE)

    for i in range(n):
        for r in range(STAND_ROWS):
            a, b = row(pts[i], r), row(pts[i + 1], r)
            c, d = row(pts[i + 1], r + 1), row(pts[i], r + 1)
            m.quad(a, b, c, d, _warm(SEAT_F, warm(a)), (0.0, 0.0, 1.0))
    for i in range(0, n, 2):
        block = i // 12
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
    idx = list(range(0, len(pts) - 1, 5))

    def arch(i):
        p, _n, h = _surf(pts[i], HALF_W)
        a = (p[0] - h[0] * (HALF_W + 14.0), p[1] - h[1] * (HALF_W + 14.0), ROOF_H * 0.45)
        b = (p[0], p[1], ROOF_H)
        k = HALF_W + STAND_ROWS * STAND_RUN + 5.0
        c = (p[0] + h[0] * k, p[1] + h[1] * k, ROOF_H * 0.45)
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
        top = (lp[0], lp[1], ROOF_H * 0.45 + (ROOF_H - ROOF_H * 0.45) * 0.4)
        m.segment((lp[0], lp[1], lp[2] + 0.07), top, 0.02, STEEL, 0.1, sides=4)


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
ARM_U, ARM_F = 0.32, 0.31                 # brazo: hombro->codo y codo->mano
Q_RIDER = -3.0                          # linea del rider: 3 m dentro del eje de pista


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



def _rider(m, base, fwd, up, lat, theta, roll, kit, pov=False) -> None:
    """Bici + atleta: cuadro, aros con radios, pedalada, brazos con codo y casco.

    El marco local es (avance, altura, lateral) apoyado en la superficie peraltada.
    En primera persona (`pov`) solo se dibuja la bici, el pedal y las manos: el cuerpo
    quedaria entre la camara (en el casco) y el cuadro.

    `theta` es el angulo de la biela y `roll` el de la rueda (rodar = distancia / radio):
    los radios giran a la velocidad real y se paran cuando se para el rider, igual que la
    pedalada. Las secciones de 6 caras son lo que hace que un brazo se lea como volumen y
    no como una tarjeta plana.
    """
    def P(s, h, w=0.0):
        return (base[0] + fwd[0] * s + up[0] * h + lat[0] * w,
                base[1] + fwd[1] * s + up[1] * h + lat[1] * w,
                base[2] + fwd[2] * s + up[2] * h + lat[2] * w)

    saddle = (HIP[0], HIP[1] + 0.05)

    # Sombra de contacto: parches bajo las dos ruedas y bajo el rider. Sin anclaje al suelo
    # la bici flota y el cuadro parece de juguete; un disco grande se lee como una mancha.
    for c in (REAR, FRONT):
        m.plate(P(c[0], 0.006), 0.13, tuple(up), SHADOW, 10, 0.0)
    m.plate((base[0], base[1], base[2] + 0.006), 0.19, tuple(up), SHADOW, 10, 0.0)

    for a, b, r in (((0.0, BB_H), saddle, 0.018), ((0.0, BB_H), HTUBE, 0.022),
                    (saddle, HTUBE, 0.015), (HTUBE, FRONT, 0.017),
                    ((0.0, BB_H), REAR, 0.015), (saddle, REAR, 0.012),
                    (HTUBE, BAR, 0.017)):
        m.segment(P(a[0], a[1]), P(b[0], b[1]), r, FRAME, 0.15, sides=6)
    m.plate(P(saddle[0], saddle[1]), 0.10, tuple(up), FRAME, 8, 0.15)          # sillin
    m.plate(P(0.0, BB_H), 0.115, tuple(lat), RIM, 12, 0.15)                   # plato

    for c in (REAR, FRONT):
        p = P(c[0], c[1])
        m.wheel(p, WR, fwd, up, lat, TIRE, 0.05, 18, 0.12)
        m.wheel(p, WR - 0.030, fwd, up, lat, RIM, 0.012, 18, 0.18)     # llanta: sin ella el aro es solo negro
        m.plate(p, 0.075, tuple(lat), RIM, 10, 0.2)                            # buje
        for k in range(6):                                                     # radios
            m.spoke(p, WR, fwd, up, lat, roll + 2 * math.pi * k / 6.0, RIM, 0.012, 0.14)

    for side in (1.0, -1.0):
        ph = theta if side > 0 else theta + math.pi
        foot = (CRANK * math.cos(ph), BB_H + CRANK * math.sin(ph))
        m.segment(P(0.0, BB_H, 0.06 * side), P(foot[0], foot[1], 0.06 * side), 0.014, RIM, 0.2, sides=6)
        kn = _knee(HIP, foot, THIGH, SHIN, 1.0)
        el = _knee(SHO, BAR, ARM_U, ARM_F, -1.0)
        if not pov:
            m.segment(P(HIP[0], HIP[1], 0.06 * side), P(kn[0], kn[1], 0.07 * side), 0.075, SKIN, 0.2, sides=6)
            m.segment(P(kn[0], kn[1], 0.07 * side), P(foot[0], foot[1], 0.075 * side), 0.055, SKIN, 0.2, sides=6)
            m.segment(P(SHO[0], SHO[1], 0.075 * side), P(el[0], el[1], 0.06 * side), 0.050, SKIN, 0.2, sides=6)
            m.segment(P(el[0], el[1], 0.06 * side), P(BAR[0], BAR[1], 0.05 * side), 0.042, SKIN, 0.2, sides=6)
        p = P(foot[0], foot[1], 0.075 * side)
        m.box(p[0], p[1], p[2], 0.28, 0.11, 0.07, SHOE, 0.0, 0.15)              # zapatilla
        h = P(BAR[0], BAR[1], 0.05 * side)
        m.box(h[0], h[1], h[2], 0.11, 0.09, 0.06, SKIN, 0.0, 0.25)              # mano en el manillar
    if not pov:
        mid = ((HIP[0] + SHO[0]) / 2.0, (HIP[1] + SHO[1]) / 2.0)
        m.segment(P(HIP[0], HIP[1]), P(mid[0], mid[1]), 0.105, kit, 0.35, sides=6)     # cadera
        m.segment(P(mid[0], mid[1]), P(SHO[0], SHO[1]), 0.095, kit, 0.35, sides=6)     # espalda
        # Hombros y cadera en cruz: sin ese ancho el atleta es una linea y de canto se lee
        # como una tarjeta, que era el problema del placeholder.
        m.segment(P(SHO[0], SHO[1], 0.075), P(SHO[0], SHO[1], -0.075), 0.055, kit, 0.30, sides=6)
        m.segment(P(HIP[0], HIP[1], 0.06), P(HIP[0], HIP[1], -0.06), 0.075, kit, 0.30, sides=6)
        m.segment(P(SHO[0], SHO[1]), P(HEAD[0] - 0.06, HEAD[1] - 0.04), 0.050, SKIN, 0.25, sides=6)
        m.segment(P(HEAD[0] - 0.08, HEAD[1] - 0.05), P(HEAD[0] + 0.12, HEAD[1] + 0.03),
                  0.108, HELMET, 0.30, sides=6)                                       # casco aerodinamico


def scene_pista(snap, anim, aspect=1.6):
    """Pista de 400 m a escala real (1 unidad = 1 m): dos rectas, dos curvas peraltadas.

    Lo fijo (tablon, lineas pintadas, meta, muro, gradas, cerchos y lamparas) se construye
    una vez y se conserva como capa estatica; por frame solo cambian el rider, la estela y
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
    # La rueda gira por distancia recorrida (theta = arco / radio), no por la cadencia:
    # asi los radios se paran cuando el rider se queda, no siguen contando pedaladas.
    anim["roll"] = anim["roll"] + anim["v"] * anim["dt"] / WR

    col = _hex(snap.get("zone_color") or "#22d3ee")
    bx, by, btx, bty, bank = _at(pts, anim["frac"])
    bl = math.hypot(btx, bty) or 1.0
    fx, fy = btx / bl, bty / bl
    nx, ny = fy, -fx                                       # normal horizontal (exterior)
    cb, sb = math.cos(bank), math.sin(bank)
    up = (-nx * sb, -ny * sb, cb)                          # normal del plano inclinado
    lat = (nx * cb, ny * cb, sb)                           # lateral sobre la superficie
    fwd = (fx, fy, 0.0)
    base = (bx + nx * Q_RIDER, by + ny * Q_RIDER, DECK + math.tan(bank) * (Q_RIDER + HALF_W))
    # Estela por posiciones reales: al pararse deja de crecer y se desvanece (no avanza
    # por detrás de un rider que ya esta quieto). Es una pegatina sobre el madera, no una
    # caja flotando 2 cm encima, que era lo que la hacía leer como basura.
    tr = anim["trail"]
    # La cinta es fina (4 cm, como una linea pintada) y termina detras de la rueda trasera:
    # los discos superpuestos de antes se leian como una mancha bajo la bici.
    if not tr or math.hypot(base[0] - tr[-1][1][0], base[1] - tr[-1][1][1]) >= 0.05:
        tr.append((anim["t"], base, lat, up))
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
    pov = dist < 0.5
    _rider(m, base, fwd, up, lat, anim["crank"], anim["roll"], col, pov=pov)

    # Inercia de la orbita: los eventos de arrastre llegan a 60-120 Hz y el frame va a ~50 fps;
    # sin suavizado el gesto sacude el encuadre. `anim["dt"]` es el paso real del loop.
    kk = min(1.0, LOOK_SMOOTH * anim["dt"])
    anim["yaw"] += (anim["yaw_t"] - anim["yaw"]) * kk
    anim["tilt"] += (anim["tilt_t"] - anim["tilt"]) * kk
    yaw, tilt = anim["yaw"], anim["tilt"]
    pax = tuple(-x for x in lat)                 # eje de cabeceo de la camara

    if pov:
        # 1a persona: la camara esta dentro del casco (a la altura de los ojos, no de la
        # cumbrera) y mira unos 31 grados hacia abajo, que es lo que mete manillar y rueda
        # delantera en el cuadro. Con el pitch del chase la bici queda fuera del encuadre.
        fovy, pitch, eh, s0, ahead = 1.22, 0.55, 1.15, HEAD[0] - 0.05, 3.2
        # Aqui el arrastre es giro de cabeza: el ojo no se mueve, porque orbitar un ojo apoyado
        # a 0,3 m del casco es un circulo de 12 cm. `tilt` positivo = mirar arriba.
        eye = tuple(base[j] + fwd[j] * s0 + up[j] * eh for j in range(3))
        gaze = _rot(_rot(tuple(fwd[j] - pitch * up[j] for j in range(3)), up, yaw), pax, -tilt)
    else:
        # chase: camara alta y detras, mirando ligeramente hacia abajo sobre la bici;
        # al cerrar el zoom sube al casco (primera persona). Alturas medidas en `up`.
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
        pivot = tuple(base[j] + up[j] * HEAD[1] * 0.5 for j in range(3))
        rig0 = tuple(-fwd[j] * dist + up[j] * (eh - HEAD[1] * 0.5) for j in range(3))
        eye = tuple(pivot[j] + _rot(_rot(rig0, up, yaw), pax, tilt)[j] for j in range(3))
        # El arrastre no puede meter el ojo por debajo del tablon: en la recta un tilt negativo
        # lo hundia, y en la curva ya lo sube el peralte.
        h = sum(up[j] * (eye[j] - base[j]) for j in range(3))
        if h < LOOK_MIN_H:
            eye = tuple(eye[j] + up[j] * (LOOK_MIN_H - h) for j in range(3))
        gaze = _rot(_rot(tuple(fwd[j] - pitch * up[j] for j in range(3)), up, yaw), pax, tilt)

    center = tuple(eye[j] + gaze[j] * ahead for j in range(3))
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
            "trail": [], "pts": None, "static": None, "anchor": None}


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
                    glfw.KEY_MINUS: "-", glfw.KEY_EQUAL: "+"}     # `+` es KEY_EQUAL (sin shift)
        slots: list = []
        # En standalone (`--3d`) no hay TUI: la ventana es la dueña de la vista y del zoom,
        # y solo reenvia al motor lo que afecta a la sesion (q/x/s). `standalone` y
        # `local_views` se comparten entre el loop (que los lee del snapshot) y `on_key`.
        standalone = False
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
            return getattr(win, "value", None) or id(win)

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
            p = press.get(winkey(win))
            if p is None:
                return
            add_look(slot_under(win), STEP_LOOK * (x - p[0]), STEP_LOOK * (p[1] - y))
            press[winkey(win)] = (x, y)

        def on_button(win, button, action, _mods):
            """Clic: mantener el boton = arrastre; boton derecho centra la orbita."""
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
                    buf = ctx.buffer(np.asarray(mesh.v or [0.0] * 10, dtype="f4").tobytes())
                    vaos[name] = ctx.vertex_array(prog, [(buf, VATTR, *FIELDS)])
                eye, center, up, fovy = cam[:4]
                far = cam[4] if len(cam) > 4 else 80.0     # la pista necesita mas fondo
                mvp = perspective(fovy, vw / vh, 0.1, far) @ look_at(eye, center, up)
                prog["mvp"].write(np.ascontiguousarray(mvp.T, dtype="f4").tobytes())
                try:
                    # EYE: el brillo y la niebla del pabellon se calculan desde la camara.
                    prog["EYE"].write(np.asarray(eye, dtype="f4").tobytes())
                except Exception:  # noqa: BLE001 - program sin ese uniform (shader antiguo)
                    pass
                for va in vaos.values():
                    va.render(moderngl.TRIANGLES)
                glfw.swap_buffers(win)
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