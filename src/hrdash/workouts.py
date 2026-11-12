"""Entrenamientos por bloques e intervalos: modelo puro, sin UI ni BLE.

Un `Workout` es una lista de partes: `Block` (nombre, tipo, duracion, objetivo) o `Repeat`
(N veces una lista de partes, con una pausa opcional ENTRE repeticiones; se pueden anidar, p. ej.
el 30/30 son dos series de 6 x (30" fuerte / 30" suave)). Todo en segundos enteros.

El objetivo de un bloque se da en dos escalas a la vez, ambas opcionales: `ftp` (% de la FTP) y
`hr` (% de la FCmax), como pares (bajo, alto) con alto = None para "sin tope". En ejecucion se
resuelve con `resolve_target`: potencia si el rodillo la da y el bloque la define; si no FC; si
tampoco, sin objetivo (solo cronometro). En esfuerzos cortos (30/30, Tabata) se omite `hr`
porque la FC va 1-2 minutos por detras.

`Workout.at(t)` dice en que bloque se esta a los `t` segundos; `to_dict`/`from_dict` y
`to_toml_inline` sirven para guardar el plan en `config.toml` y para mandarlo a la ventana.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

KINDS = ("warmup", "work", "rest", "cooldown", "free")
KIND_LABELS = {"warmup": "Calentamiento", "work": "Trabajo", "rest": "Recuperacion",
               "cooldown": "Enfriamiento", "free": "Libre"}

MAX_TOTAL_S = 6 * 3600        # tope de un plan
MAX_SLOTS = 2000              # bloques ya expandidos (acota el trabajo de `slots`)
MAX_DEPTH = 3                 # repeticiones anidadas
MAX_REPEAT = 200
MAX_NAME = 40


@dataclass(frozen=True)
class Block:
    name: str
    kind: str
    secs: int
    ftp: tuple | None = None      # (bajo, alto) en %FTP; alto None = sin tope
    hr: tuple | None = None       # (bajo, alto) en %FCmax


@dataclass(frozen=True)
class Repeat:
    count: int
    parts: tuple                  # Block | Repeat
    between: Block | None = None  # pausa entre repeticiones (no tras la ultima)


@dataclass(frozen=True)
class Slot:
    """Un bloque ya expandido: su lugar en el plan y a que repeticion pertenece."""

    index: int
    block: Block
    start: int                    # segundos desde el inicio del plan
    rep: tuple = ()               # ((i, n), ...) de fuera hacia dentro, i desde 1

    @property
    def end(self) -> int:
        return self.start + self.block.secs

    @property
    def rep_label(self) -> str:
        """"3/4" de la repeticion mas interna, o "" si el bloque no esta en una repeticion."""
        return "%d/%d" % self.rep[-1] if self.rep else ""


@dataclass(frozen=True)
class Position:
    """Donde esta un plan a los `t` segundos. `slot` es None cuando ya termino."""

    slot: Slot | None
    elapsed: int                  # segundos dentro del bloque
    left: int                     # segundos que faltan del bloque
    left_total: int               # segundos que faltan del plan
    next: Slot | None
    progress: float               # 0..1 del plan completo
    done: bool


@dataclass(frozen=True)
class Target:
    """Objetivo ya resuelto a valores absolutos para este atleta."""

    unit: str                     # "w" o "bpm"
    lo: int
    hi: int | None
    pct: tuple                    # el rango original en %

    def status(self, value) -> str | None:
        """"low", "ok" o "high"; None si no hay lectura."""
        if value is None:
            return None
        if value < self.lo:
            return "low"
        if self.hi is not None and value > self.hi:
            return "high"
        return "ok"


# ------------------------------------------------------------------ estructura
def _dur(part) -> int:
    if isinstance(part, Block):
        return part.secs
    inner = sum(_dur(p) for p in part.parts)
    gaps = (part.count - 1) * part.between.secs if part.between is not None else 0
    return inner * part.count + gaps


def _count(part) -> int:
    if isinstance(part, Block):
        return 1
    inner = sum(_count(p) for p in part.parts)
    gaps = (part.count - 1) if part.between is not None else 0
    return inner * part.count + gaps


def _flatten(parts, reps, out, t) -> int:
    for part in parts:
        if isinstance(part, Block):
            out.append(Slot(len(out), part, t, tuple(reps)))
            t += part.secs
            continue
        for i in range(part.count):
            ctx = reps + [(i + 1, part.count)]
            t = _flatten(part.parts, ctx, out, t)
            if part.between is not None and i < part.count - 1:
                out.append(Slot(len(out), part.between, t, tuple(ctx)))
                t += part.between.secs
    return t


@dataclass(frozen=True)
class Workout:
    name: str
    parts: tuple
    _slots: list = field(default_factory=list, init=False, repr=False, compare=False)
    _starts: list = field(default_factory=list, init=False, repr=False, compare=False)

    @property
    def total_s(self) -> int:
        return sum(_dur(p) for p in self.parts)

    def slots(self) -> tuple:
        """Los bloques expandidos, en orden (se calcula una vez)."""
        if not self._slots:
            if sum(_count(p) for p in self.parts) > MAX_SLOTS:
                raise ValueError("plan demasiado largo")
            out: list = []
            _flatten(self.parts, [], out, 0)
            self._slots.extend(out)
            self._starts.extend(s.start for s in out)
        return tuple(self._slots)

    def at(self, t) -> Position:
        """Posicion a los `t` segundos (negativo cuenta como 0; pasado el final, `done`)."""
        slots = self.slots()
        total = self.total_s
        t = 0 if (t is None or t < 0) else int(t)
        if t >= total:
            return Position(None, 0, 0, 0, None, 1.0, True)
        i = bisect_right(self._starts, t) - 1
        slot = slots[i]
        return Position(slot, t - slot.start, slot.end - t, total - t,
                        slots[i + 1] if i + 1 < len(slots) else None, t / total, False)


# ------------------------------------------------------------------ validacion
def _range_problem(r, what):
    if r is None:
        return None
    if not isinstance(r, (tuple, list)) or len(r) != 2:
        return f"{what}: rango mal formado"
    lo, hi = r
    if not isinstance(lo, int) or isinstance(lo, bool) or not 0 <= lo <= 400:
        return f"{what}: minimo fuera de 0..400"
    if hi is not None and (not isinstance(hi, int) or isinstance(hi, bool) or not lo < hi <= 400):
        return f"{what}: el maximo debe estar entre el minimo y 400"
    return None


def _block_problems(b, where) -> list:
    out = []
    if not isinstance(b.name, str) or not 0 < len(b.name) <= MAX_NAME:
        out.append(f"{where}: nombre vacio o de mas de {MAX_NAME} caracteres")
    if b.kind not in KINDS:
        out.append(f"{where}: tipo desconocido {b.kind!r}")
    if not isinstance(b.secs, int) or isinstance(b.secs, bool) or not 1 <= b.secs <= MAX_TOTAL_S:
        out.append(f"{where}: duracion fuera de 1..{MAX_TOTAL_S} s")
    for r, what in ((b.ftp, "ftp"), (b.hr, "hr")):
        p = _range_problem(r, f"{where} {what}")
        if p:
            out.append(p)
    return out


def _part_problems(part, where, depth) -> list:
    if isinstance(part, Block):
        return _block_problems(part, where)
    if not isinstance(part, Repeat):
        return [f"{where}: no es un bloque ni una repeticion"]
    out = []
    if depth > MAX_DEPTH:
        out.append(f"{where}: repeticiones anidadas a mas de {MAX_DEPTH} niveles")
        return out
    if not isinstance(part.count, int) or isinstance(part.count, bool) \
            or not 1 <= part.count <= MAX_REPEAT:
        out.append(f"{where}: repeticiones fuera de 1..{MAX_REPEAT}")
    if not part.parts:
        out.append(f"{where}: repeticion vacia")
    for i, p in enumerate(part.parts):
        out += _part_problems(p, f"{where}.{i + 1}", depth + 1)
    if part.between is not None:
        out += _block_problems(part.between, f"{where} pausa")
    return out


def validate(w: Workout) -> list:
    """Problemas del plan como textos legibles; vacio = valido. No levanta."""
    out = []
    if not isinstance(w.name, str) or not 0 < len(w.name) <= MAX_NAME:
        out.append(f"nombre vacio o de mas de {MAX_NAME} caracteres")
    if not w.parts:
        out.append("plan vacio")
    for i, p in enumerate(w.parts):
        out += _part_problems(p, f"parte {i + 1}", 1)
    if out:
        return out                               # no se calcula nada sobre un plan roto
    if sum(_count(p) for p in w.parts) > MAX_SLOTS:
        out.append(f"mas de {MAX_SLOTS} bloques")
    elif w.total_s > MAX_TOTAL_S:
        out.append(f"dura mas de {MAX_TOTAL_S // 3600} h")
    return out


# ------------------------------------------------------------------ objetivos
def resolve_target(block: Block, ftp: int, hrmax: int, has_power: bool) -> Target | None:
    """Objetivo absoluto de un bloque: potencia si hay y el bloque la define; si no FC; si
    tampoco, None (solo cronometro)."""
    if has_power and block.ftp:
        lo, hi = block.ftp
        return Target("w", round(ftp * lo / 100), None if hi is None else round(ftp * hi / 100),
                      tuple(block.ftp))
    if block.hr:
        lo, hi = block.hr
        return Target("bpm", round(hrmax * lo / 100), None if hi is None else round(hrmax * hi / 100),
                      tuple(block.hr))
    return None


# ------------------------------------------------------------------ presets
# Niveles: (%FTP, %FCmax). El maximo no tiene tope ni FC: la FC no llega en esfuerzos cortos.
LEVELS = {
    "recovery": ((0, 55), (50, 60)),
    "endurance": ((56, 75), (60, 70)),
    "tempo": ((76, 87), (70, 80)),
    "sweetspot": ((88, 94), (80, 85)),
    "threshold": ((95, 105), (85, 92)),
    "vo2": ((106, 120), (90, 100)),
    "max": ((121, None), None),
}


def _blk(name, kind, secs, level=None, ftp_only=False, ftp=None, hr=None) -> Block:
    if level is not None:
        lf, lh = LEVELS[level]
        ftp, hr = lf, (None if ftp_only else lh)
    return Block(name, kind, secs, ftp, hr)


def _m(minutes: float) -> int:
    return round(minutes * 60)


def free(minutes: int, name: str = "Libre") -> Workout:
    """N minutos sin objetivos: solo cronometro."""
    return Workout(name, (Block("Libre", "free", _m(minutes)),))


def custom(warm_min: int, reps: int, work_s: int, rest_s: int, cool_min: int,
           work: str = "vo2", rest: str = "recovery", name: str = "Personalizado") -> Workout:
    """Calentamiento + `reps` x (trabajo / recuperacion) + enfriamiento. La recuperacion va ENTRE
    repeticiones, no tras la ultima; `warm_min`/`cool_min` = 0 los omite."""
    parts: list = []
    if warm_min > 0:
        parts.append(_blk("Calentar", "warmup", _m(warm_min), "endurance"))
    between = _blk("Recuperar", "rest", rest_s, rest) if rest_s > 0 else None
    parts.append(Repeat(reps, (_blk("Fuerte", "work", work_s, work),), between))
    if cool_min > 0:
        parts.append(_blk("Enfriar", "cooldown", _m(cool_min), "recovery"))
    return Workout(name, tuple(parts))


def _build_presets() -> dict:
    cool = lambda m: _blk("Enfriar", "cooldown", _m(m), "recovery")        # noqa: E731
    warm = lambda m: _blk("Calentar", "warmup", _m(m), "endurance")        # noqa: E731
    rec = lambda s, n="Recuperar": _blk(n, "rest", s, "recovery")          # noqa: E731
    p = {}
    p["calentamiento"] = Workout("Calentamiento 15'", (
        _blk("Suave", "warmup", _m(5), "recovery"),
        _blk("Resistencia", "warmup", _m(7), "endurance"),
        _blk("Tempo suave", "warmup", _m(3), "tempo")))
    p["resistencia"] = Workout("Resistencia 30'", (
        _blk("Calentar", "warmup", _m(5), "recovery"),
        _blk("Resistencia", "work", _m(20), "endurance"),
        cool(5)))
    p["sweetspot"] = Workout("Sweet spot 45'", (
        warm(11),
        Repeat(2, (_blk("Sweet spot", "work", _m(12), "sweetspot"),), rec(_m(5))),
        cool(5)))
    p["noruego"] = Workout("Noruego 4x4", (
        warm(10),
        Repeat(4, (_blk("4' fuerte", "work", _m(4), hr=(90, 95)),),
               _blk("3' suave", "rest", _m(3), hr=(60, 70))),
        cool(5)))
    p["vo2max"] = Workout("VO2max 5x3", (
        warm(10),
        Repeat(5, (_blk("3' fuerte", "work", _m(3), "vo2"),), rec(_m(3))),
        cool(8)))
    p["30-30"] = Workout("30/30", (
        warm(10),
        Repeat(2, (Repeat(6, (_blk("30\" fuerte", "work", 30, "vo2", ftp_only=True),
                              _blk("30\" suave", "rest", 30, "recovery", ftp_only=True))),),
               rec(_m(4), "Entre series")),
        cool(4)))
    p["tabata"] = Workout("Tabata 20/10", (
        warm(10),
        Repeat(8, (_blk("20\" maximo", "work", 20, "max"),
                   _blk("10\" suave", "rest", 10, "recovery", ftp_only=True))),
        cool(6)))
    p["umbral"] = Workout("Umbral 2x20", (
        warm(10),
        Repeat(2, (_blk("Umbral", "work", _m(20), "threshold"),), rec(_m(5))),
        cool(5)))
    return p


PRESETS = _build_presets()
PRESET_IDS = tuple(PRESETS)


def preset(preset_id: str) -> Workout:
    return PRESETS[preset_id]


# ------------------------------------------------------------------ serializacion
def _range_out(r):
    return None if r is None else ([r[0]] if r[1] is None else [r[0], r[1]])


def _range_in(r):
    if r is None:
        return None
    if not isinstance(r, (list, tuple)) or len(r) not in (1, 2):
        raise ValueError("rango mal formado")
    return (r[0], r[1] if len(r) == 2 else None)


def _part_out(part) -> dict:
    if isinstance(part, Block):
        d = {"kind": part.kind, "name": part.name, "secs": part.secs}
        for k, r in (("ftp", part.ftp), ("hr", part.hr)):
            if r is not None:
                d[k] = _range_out(r)
        return d
    d = {"repeat": part.count, "parts": [_part_out(p) for p in part.parts]}
    if part.between is not None:
        d["between"] = _part_out(part.between)
    return d


def _block_in(d) -> Block:
    if not isinstance(d, dict) or "repeat" in d:
        raise ValueError("se esperaba un bloque")
    return Block(d.get("name"), d.get("kind"), d.get("secs"), _range_in(d.get("ftp")),
                 _range_in(d.get("hr")))


def _part_in(d):
    if isinstance(d, dict) and "repeat" in d:
        between = _block_in(d["between"]) if d.get("between") is not None else None
        return Repeat(d["repeat"], tuple(_part_in(p) for p in d.get("parts", [])), between)
    return _block_in(d)


def to_dict(w: Workout) -> dict:
    """Forma JSON/TOML del plan (listas y dicts); `from_dict` es la inversa."""
    return {"name": w.name, "parts": [_part_out(p) for p in w.parts]}


def from_dict(d) -> Workout:
    """Plan a partir de `to_dict`. Levanta ValueError si no es un plan valido."""
    try:
        w = Workout(d["name"], tuple(_part_in(p) for p in d["parts"]))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"plan mal formado: {exc}") from exc
    problems = validate(w)
    if problems:
        raise ValueError("; ".join(problems))
    return w


def _toml_key(k: str) -> str:
    return k if k and all(c.isalnum() or c in "_-" for c in k) else _toml_str(k)


def _toml_str(s: str) -> str:
    esc = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") \
        .replace("\t", "\\t")
    return f'"{esc}"'


def to_toml_inline(obj) -> str:
    """Valor TOML en una sola linea (tablas y listas en linea; las claves None se omiten)."""
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, str):
        return _toml_str(obj)
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(to_toml_inline(v) for v in obj) + "]"
    if isinstance(obj, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {to_toml_inline(v)}"
                                for k, v in obj.items() if v is not None) + " }"
    raise TypeError(f"no serializable a TOML: {type(obj).__name__}")
