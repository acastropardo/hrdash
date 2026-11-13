"""Carreras contra el reloj: persecucion individual y kilometro contra un fantasma.

Modelo puro (sin UI ni BLE; se comprueba headless, como `workouts.py`). La velocidad sale de la
POTENCIA con fisica de ciclismo en llano, no de la rueda del rodillo (que depende de la curva de
resistencia de cada aparato y no sirve para competir):

    m * v * dv/dt = P - 0,5 * rho * CdA * v^3 - Crr * m * g * v

`PursuitRace` es la maquina de estados (listo -> cuenta atras -> en marcha -> terminada) que integra
al jugador y a su fantasma con el MISMO modelo, y guarda parciales por vuelta y la diferencia.
Plan: docs/plans (persecucion-contra-fantasma en el repo del cliente Godot).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

G = 9.80665
DT = 0.1                      # paso de la carrera y del fantasma (10 Hz)
COUNTDOWN_S = 3.0
LAP_M = 250.0                 # vuelta de pista
MAX_RACE_S = 3600.0           # tope de una carrera (el fantasma nunca corre indefinidamente)

DIST_LADDER = (1000, 2000, 3000, 4000)
TARGET_STEP = 5               # segundos por paso del stepper del tiempo objetivo
MAX_SPEED = 25.0              # m/s (90 km/h): cota dura de la fisica, evita que un dato absurdo de potencia dispare v
POWER_MAX = 3000.0            # W: tope de lo que se acepta del sensor


@dataclass(frozen=True)
class Physics:
    cda: float = 0.28         # m2, posicion de pista
    crr: float = 0.003
    mass: float = 83.0        # kg, ciclista + bici
    rho: float = 1.2          # kg/m3

    def resist(self, v: float) -> float:
        """Potencia que consume avanzar a `v` m/s (aire + rodadura)."""
        return 0.5 * self.rho * self.cda * v ** 3 + self.crr * self.mass * G * v

    def step(self, v: float, power_w: float, dt: float) -> float:
        """Velocidad tras `dt` s a esa potencia: balance de energia cinetica con la resistencia en el punto
        medio (estable con pasos de 0,1 s, y arranca desde parado). Nunca negativa ni NaN."""
        p = min(max(float(power_w), 0.0), POWER_MAX) if power_w == power_w else 0.0
        v = min(max(float(v), 0.0), MAX_SPEED)
        e = 0.5 * self.mass * v * v
        v1 = math.sqrt(max(0.0, 2.0 * (e + (p - self.resist(v)) * dt) / self.mass))
        vm = 0.5 * (v + v1)
        e1 = e + (p - self.resist(vm)) * dt
        return min(MAX_SPEED, math.sqrt(max(0.0, 2.0 * e1 / self.mass)))

    def steady_speed(self, power_w: float) -> float:
        """Velocidad estable (m/s) a potencia constante, por biseccion."""
        p = min(max(float(power_w), 0.0), POWER_MAX)
        lo, hi = 0.0, MAX_SPEED
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if self.resist(mid) < p:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)


def simulate(phys: Physics, power_at, distance_m: float, dt: float = DT, limit_s: float = MAX_RACE_S):
    """Trayectoria de un corredor desde parado: lista de (t, s, v) cada `dt` hasta cubrir `distance_m` (o
    `limit_s`). `power_at(t)` da la potencia en cada instante. Es la unica integracion que usan el fantasma
    y la carrera, asi que dan exactamente los mismos numeros."""
    t = s = v = 0.0
    traj = [(0.0, 0.0, 0.0)]
    while s < distance_m and t < limit_s:
        v = phys.step(v, power_at(t), dt)
        s += v * dt
        t += dt
        traj.append((t, s, v))
    return traj


def time_at(traj, distance_m: float) -> float | None:
    """Instante (interpolado) en que la trayectoria alcanza esa distancia; None si no llega."""
    for i in range(1, len(traj)):
        t0, s0, _ = traj[i - 1]
        t1, s1, _ = traj[i]
        if s1 >= distance_m:
            return t0 if s1 == s0 else t0 + (t1 - t0) * (distance_m - s0) / (s1 - s0)
    return None


def pace_power(phys: Physics, distance_m: float, target_s: float) -> float:
    """Potencia constante (W) con la que el fantasma cubre la distancia en `target_s` desde parado, con la
    salida incluida. Biseccion sobre la simulacion; acota a 20..POWER_MAX si el objetivo no es alcanzable."""
    lo, hi = 20.0, POWER_MAX
    for _ in range(40):
        mid = 0.5 * (lo + hi)
        t = time_at(simulate(phys, lambda _t, p=mid: p, distance_m), distance_m)
        if t is None or t > target_s:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def series_power(series, step_s: float = 1.0):
    """`power_at(t)` de una serie guardada (una muestra cada `step_s`); mantiene el valor entre muestras y
    repite la ultima al terminar la serie."""
    vals = [max(0.0, float(x)) for x in series] or [0.0]

    def at(t: float) -> float:
        return vals[min(len(vals) - 1, max(0, int(t / step_s)))]
    return at


# ------------------------------------------------------------------ especificacion (la eligen el menu y la config)
@dataclass(frozen=True)
class PursuitSpec:
    distance_m: int = 4000
    ghost: str = "pace"           # "pace" (ritmo objetivo) | "best" (mi mejor marca)
    target_s: int = 390           # tiempo objetivo del fantasma de ritmo (6:30 a 4 km ~ 200 W)

    def target_range(self) -> tuple:
        """Tiempos objetivo razonables para la distancia: de 60 km/h a 14,4 km/h de media."""
        d = self.distance_m
        lo = int(math.ceil(d / 16.7 / TARGET_STEP) * TARGET_STEP)
        hi = int(d / 4.0 // TARGET_STEP * TARGET_STEP)
        return lo, hi

    def clamped(self) -> "PursuitSpec":
        lo, hi = self.target_range()
        return replace(self, target_s=min(hi, max(lo, int(self.target_s))))

    def summary(self) -> str:
        """"PERSECUCION 4 KM - RITMO 6:30" para el menu."""
        km = self.distance_m // 1000
        ghost = f"RITMO {fmt_clock(self.target_s)}" if self.ghost == "pace" else "MI MEJOR MARCA"
        return f"PERSECUCION {km} KM - {ghost}"


def fmt_clock(secs: float) -> str:
    """m:ss (o h:mm:ss) para tiempos de carrera."""
    s = int(round(secs))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def apply_act(spec: PursuitSpec, act) -> PursuitSpec | None:
    """Spec nueva tras una accion del menu, `spec` si no cambia nada y None si `act` no es de persecucion.
    Acciones: `race:dist:+|-` (1-4 km), `race:ghost:pace|best`, `race:target:+|-` (pasos de 5 s). Nunca devuelve
    un estado fuera de rango: al cambiar la distancia el tiempo objetivo se ajusta a su nuevo rango."""
    if not isinstance(act, str):
        return None
    p = act.split(":")
    if p[0] != "race" or len(p) != 3:
        return None
    if p[1] == "dist" and p[2] in ("+", "-"):
        d = 1 if p[2] == "+" else -1
        i = DIST_LADDER.index(spec.distance_m) if spec.distance_m in DIST_LADDER else len(DIST_LADDER) - 1
        nd = DIST_LADDER[min(len(DIST_LADDER) - 1, max(0, i + d))]
        # el ritmo (m/s) se conserva al cambiar de distancia: 4 km en 6:30 -> 1 km en 1:37
        nt = int(round(spec.target_s * nd / spec.distance_m / TARGET_STEP) * TARGET_STEP)
        return replace(spec, distance_m=nd, target_s=nt).clamped()
    if p[1] == "ghost" and p[2] in ("pace", "best"):
        return replace(spec, ghost=p[2])
    if p[1] == "target" and p[2] in ("+", "-"):
        lo, hi = spec.target_range()
        d = TARGET_STEP if p[2] == "+" else -TARGET_STEP
        return replace(spec, target_s=min(hi, max(lo, (int(spec.target_s) // TARGET_STEP) * TARGET_STEP + d)))
    return None


def spec_to_dict(s: PursuitSpec) -> dict:
    return {"distance_m": s.distance_m, "ghost": s.ghost, "target_s": s.target_s}


def spec_from_dict(d) -> PursuitSpec:
    """Levanta ValueError si algo no es valido (nunca otra cosa)."""
    try:
        s = PursuitSpec(d["distance_m"], d["ghost"], d["target_s"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"persecucion mal formada: {exc}") from exc
    lo, hi = s.target_range() if s.distance_m in DIST_LADDER else (0, -1)
    ints = (s.distance_m, s.target_s)
    if s.ghost not in ("pace", "best") or not all(isinstance(v, int) and not isinstance(v, bool) for v in ints) \
            or s.distance_m not in DIST_LADDER or not lo <= s.target_s <= hi:
        raise ValueError("persecucion fuera de rango")
    return s


# ------------------------------------------------------------------ la carrera
class PursuitRace:
    """Persecucion individual contra un fantasma, salida parada. `step(dt, power_w)` avanza; la pausa congela todo.

    El fantasma se calcula entero al empezar (es determinista: su potencia es conocida), asi que la diferencia en
    segundos es exacta tambien cuando el jugador va por delante: tiempo del fantasma en el punto donde esta el
    jugador menos el tiempo actual. `gap_s > 0` = vas por delante."""

    def __init__(self, spec: PursuitSpec, phys: Physics | None = None, ghost_power=None, lap_m: float = LAP_M):
        self.spec = spec
        self.phys = phys or Physics()
        self.lap_m = lap_m
        self.laps = max(1, int(round(spec.distance_m / lap_m)))
        if ghost_power is None:       # fantasma de ritmo: potencia constante calculada para el tiempo objetivo
            p = pace_power(self.phys, spec.distance_m, spec.target_s)
            ghost_power = lambda _t, p=p: p     # noqa: E731
        self.ghost_traj = simulate(self.phys, ghost_power, spec.distance_m)
        self.ghost_time = time_at(self.ghost_traj, spec.distance_m)    # None si no termina en el limite
        self.state = "ready"          # ready -> countdown -> running -> finished
        self.paused = False
        self.countdown = COUNTDOWN_S
        self.t = 0.0                  # tiempo de carrera (desde la salida)
        self.s = 0.0
        self.v = 0.0
        self.final_s: float | None = None
        self.splits: list = []        # tiempo de cada vuelta (s)
        self._lap_t: list = []        # tiempo acumulado al cruzar cada vuelta
        self.power_series: list = []  # potencia por segundo (para guardar la marca)
        self._sec_acc = 0.0
        self._sec_t = 0.0

    # -- control
    def start(self) -> None:
        if self.state == "ready":
            self.state = "countdown"
            self.countdown = COUNTDOWN_S

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    # -- fantasma
    def ghost_state(self, t: float | None = None) -> tuple:
        """(s, v) del fantasma en el instante `t` (por defecto el actual), interpolado; se queda en la meta."""
        t = self.t if t is None else t
        tr = self.ghost_traj
        if t <= 0.0:
            return 0.0, 0.0
        i = min(len(tr) - 1, int(t / DT))
        if i >= len(tr) - 1:
            return tr[-1][1], 0.0 if tr[-1][1] >= self.spec.distance_m else tr[-1][2]
        t0, s0, v0 = tr[i]
        t1, s1, v1 = tr[i + 1]
        k = (t - t0) / (t1 - t0)
        return s0 + (s1 - s0) * k, v0 + (v1 - v0) * k

    def gap_s(self) -> float:
        """Segundos de ventaja sobre el fantasma en el punto donde estas (positivo = vas por delante)."""
        tg = time_at(self.ghost_traj, self.s)
        if tg is None:                # el fantasma nunca llega ahi: ventaja "infinita" acotada
            return MAX_RACE_S - self.t
        return tg - self.t

    # -- avance
    def step(self, dt: float, power_w: float) -> None:
        if self.paused or self.state in ("ready", "finished") or dt <= 0.0:
            return
        if self.state == "countdown":
            self.countdown -= dt
            if self.countdown > 0.0:
                return
            dt = -self.countdown      # lo que sobra de la cuenta atras ya corre
            self.countdown = 0.0
            self.state = "running"
            if dt <= 0.0:
                return
        v1 = self.phys.step(self.v, power_w, dt)
        s1 = self.s + 0.5 * (self.v + v1) * dt
        t1 = self.t + dt
        # parciales: cruces de vuelta dentro del paso, interpolados
        while len(self._lap_t) < self.laps and s1 >= (len(self._lap_t) + 1) * self.lap_m:
            target = (len(self._lap_t) + 1) * self.lap_m
            tc = self.t + dt * (target - self.s) / max(1e-9, s1 - self.s)
            prev = self._lap_t[-1] if self._lap_t else 0.0
            self._lap_t.append(tc)
            self.splits.append(tc - prev)
        self._sec_acc += max(0.0, min(POWER_MAX, power_w if power_w == power_w else 0.0)) * dt
        self._sec_t += dt
        if self._sec_t >= 1.0 - 1e-9:     # potencia media de cada segundo de carrera (para guardar la marca)
            self.power_series.append(self._sec_acc / self._sec_t)
            self._sec_acc = self._sec_t = 0.0
        self.v, self.s, self.t = v1, s1, t1
        if self.s >= self.spec.distance_m:
            self.state = "finished"
            self.final_s = self._lap_t[-1] if len(self._lap_t) >= self.laps else self.t
            self.s = float(self.spec.distance_m)

    def lap(self) -> int:
        """Vuelta en curso (1..laps)."""
        return min(self.laps, int(self.s // self.lap_m) + 1)

    def to_dict(self) -> dict:
        gs, gv = self.ghost_state()
        d = {"event": "persecucion", "state": self.state, "paused": self.paused,
             "distance_m": self.spec.distance_m, "t": round(self.t, 2),
             "s": [round(self.s, 1), round(gs, 1)], "v": [round(self.v * 3.6, 1), round(gv * 3.6, 1)],
             "gap_s": round(self.gap_s(), 2), "gap_m": round(self.s - gs, 1),
             "lap": self.lap(), "laps": self.laps, "splits": [round(x, 2) for x in self.splits],
             "countdown": round(max(0.0, self.countdown), 1) if self.state == "countdown" else 0.0}
        if self.state == "finished":
            d["final_s"] = round(self.final_s, 2)
            d["ghost_s"] = None if self.ghost_time is None else round(self.ghost_time, 2)
        return d
