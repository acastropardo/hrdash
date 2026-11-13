"""Carreras contra un pelotón de IA: scratch (el primero en cruzar la meta tras N vueltas gana).

Modelo puro (sin UI ni BLE, como `race.py`): todos los corredores usan el MISMO modelo físico (`race.Physics`), con
rebufo, y los rivales deciden su potencia con una táctica simple basada en un FTP y una reserva anaeróbica W':

    P(t) sostenible = CP + W'/t         (CP ~ FTP, W' ~ 15 kJ; atacar gasta W', rodar por debajo de CP la recupera)

- Rodar a rueda: un seguidor iguala la velocidad del de delante (control proporcional de la potencia) y se ahorra el
  aire; sin nadie delante lleva el ritmo a una fracción de CP.
- Los rivales con papel de atacante lanzan un ataque a mitad de carrera mientras les queda W'; todos esprintan al
  final con lo que les quede (los velocistas empiezan antes y gastan más).
- Pasar a otro corredor cambia de línea hacia fuera (q mayor) para no atravesarlo.
Todo con semilla fija: la misma carrera sale igual (comprobable y repetible).

La clasificación sale de los cruces de meta interpolados; cuando el jugador cruza, el resto del pelotón se simula
hacia delante de golpe para dar la clasificación final.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from . import race
from .race import DT, G, LAP_M, MAX_RACE_S, POWER_MAX, Physics

LAPS_LADDER = (8, 12, 16, 20)            # 2, 3, 4 y 5 km
LEVELS = ("easy", "even", "hard")        # más lentos / parejos / más rápidos
LEVEL_NAMES = {"easy": "MAS LENTOS", "even": "PAREJOS", "hard": "MAS RAPIDOS"}
LEVEL_RANGE = {"easy": (0.78, 0.92), "even": (0.92, 1.06), "hard": (1.04, 1.18)}   # factor sobre el FTP del jugador
N_RIVALS = 3
COLORS = ("#ef4444", "#3b82f6", "#eab308")      # rival 1, 2 y 3 (el jugador lleva el de su zona)
NAMES = ("ROJO", "AZUL", "AMARILLO")
ROLES = ("rouleur", "attacker", "sprinter")
W_PRIME = 15000.0                         # J
DRAFT_MAX = 0.33                          # ahorro de aire pegado a la rueda
DRAFT_FULL_M = 1.2                        # ahorro completo hasta aqui...
DRAFT_END_M = 10.0                        # ...y nulo a partir de aqui
LANE_GAP = 1.0                            # separación lateral (m) para pasar a otro
LANE_RATE = 0.9                           # m/s de cambio de línea
LANE_HOLD_S = 3.0                         # una vez fuera, se mantiene la línea al menos esto (sin vaivén)
A_MAX = 3.0                               # m/s2 de aceleración máxima (salida y esprints)
Q_MAX = 3.0
START_Q = (0.0, 0.8, 1.6, 2.4)            # parrilla: jugador y rivales en abanico


def draft_gain(d_m: float, dq: float) -> float:
    """Ahorro de aire (0..DRAFT_MAX) a `d_m` metros por detrás de otro corredor con `dq` m de separación lateral."""
    if d_m <= 0.0 or d_m >= DRAFT_END_M or abs(dq) >= 1.1:
        return 0.0
    lat = 1.0 - min(1.0, abs(dq) / 1.1) ** 2
    if d_m <= DRAFT_FULL_M:
        return DRAFT_MAX * lat
    return DRAFT_MAX * lat * (1.0 - (d_m - DRAFT_FULL_M) / (DRAFT_END_M - DRAFT_FULL_M))


# ------------------------------------------------------------------ especificación
@dataclass(frozen=True)
class ScratchSpec:
    laps: int = 12
    level: str = "even"
    seed: int = 7

    def distance_m(self) -> int:
        return int(self.laps * LAP_M)

    def summary(self) -> str:
        return f"SCRATCH {self.laps} VUELTAS - {LEVEL_NAMES[self.level]}"


def apply_act(spec: ScratchSpec, act) -> ScratchSpec | None:
    """`scratch:laps:+|-` (8/12/16/20 vueltas), `scratch:level:+|-` o `scratch:level:easy|even|hard`. None si no es de
    scratch; `spec` si no cambia nada. Nunca devuelve un estado fuera de rango."""
    if not isinstance(act, str):
        return None
    p = act.split(":")
    if p[0] != "scratch" or len(p) != 3:
        return None
    if p[1] == "laps" and p[2] in ("+", "-"):
        i = LAPS_LADDER.index(spec.laps) if spec.laps in LAPS_LADDER else 1
        i = min(len(LAPS_LADDER) - 1, max(0, i + (1 if p[2] == "+" else -1)))
        return replace(spec, laps=LAPS_LADDER[i])
    if p[1] == "level":
        if p[2] in LEVELS:
            return replace(spec, level=p[2])
        if p[2] in ("+", "-"):
            i = LEVELS.index(spec.level) if spec.level in LEVELS else 1
            i = min(len(LEVELS) - 1, max(0, i + (1 if p[2] == "+" else -1)))
            return replace(spec, level=LEVELS[i])
    return None


def spec_to_dict(s: ScratchSpec) -> dict:
    return {"laps": s.laps, "level": s.level, "seed": s.seed}


def spec_from_dict(d) -> ScratchSpec:
    """Levanta ValueError si algo no es válido (nunca otra cosa)."""
    try:
        s = ScratchSpec(d["laps"], d["level"], d["seed"])
    except (KeyError, TypeError) as exc:
        raise ValueError(f"scratch mal formado: {exc}") from exc
    ints = (s.laps, s.seed)
    if s.laps not in LAPS_LADDER or s.level not in LEVELS or not all(isinstance(v, int) and not isinstance(v, bool) for v in ints):
        raise ValueError("scratch fuera de rango")
    return s


# ------------------------------------------------------------------ corredores
class Rider:
    """Un corredor: estado físico (s total en metros, v en m/s, q lateral) y, si es IA, su perfil y su táctica."""

    def __init__(self, idx: int, name: str, color: str, phys: Physics, is_player: bool = False):
        self.idx, self.name, self.color, self.phys, self.is_player = idx, name, color, phys, is_player
        self.s = 0.0
        self.v = 0.0
        self.q = START_Q[idx]
        self.power = 0.0
        self.draft = 0.0
        self.finish_t: float | None = None
        # IA
        self.cp = 250.0
        self.w0 = W_PRIME
        self.w = W_PRIME
        self.role = "rouleur"
        self.lead_frac = 0.90          # fracción de CP al llevar el ritmo
        self.attack_at = 1e9           # instante del ataque
        self.attack_until = -1.0
        self.sprint_laps = 1.0         # vueltas que faltan cuando empieza a esprintar
        self.attacked = False
        self.qv = 0.0                  # velocidad lateral (m/s)
        self.tq = START_Q[idx]         # linea a la que va (con compromiso: no cambia de idea cada 0,1 s)
        self.hold_until = 0.0


def make_field(spec: ScratchSpec, ftp: float, mass: float) -> list:
    """Jugador (idx 0) y N_RIVALS rivales con FTP = ftp x factor del nivel, masa y papel con la semilla fija."""
    rng = random.Random(spec.seed)
    riders = [Rider(0, "TU", "#22c55e", Physics(mass=mass, a_max=A_MAX), True)]
    lo, hi = LEVEL_RANGE[spec.level]
    roles = list(ROLES)
    rng.shuffle(roles)
    for i in range(N_RIVALS):
        r = Rider(i + 1, NAMES[i], COLORS[i], Physics(cda=0.28 * rng.uniform(0.95, 1.05), mass=mass * rng.uniform(0.92, 1.08), a_max=A_MAX))
        r.cp = max(60.0, ftp * rng.uniform(lo, hi))
        r.w0 = r.w = W_PRIME * rng.uniform(0.85, 1.15)
        r.role = roles[i % len(roles)]
        r.lead_frac = rng.uniform(0.86, 0.94)
        r.sprint_laps = 1.7 if r.role == "sprinter" else 1.0
        if r.role == "attacker":
            r.attack_at = rng.uniform(0.35, 0.75) * spec.distance_m() / 10.5     # instante aprox. (a ~38 km/h)
        riders.append(r)
    return riders


# ------------------------------------------------------------------ la carrera
class ScratchRace:
    """Scratch contra un pelotón de IA, salida parada. `step(dt, power_w)` avanza (la potencia es la del jugador)."""

    def __init__(self, spec: ScratchSpec, ftp: float, mass: float, lap_m: float = LAP_M):
        self.spec = spec
        self.lap_m = lap_m
        self.laps = spec.laps
        self.dist = float(spec.laps * lap_m)
        self.riders = make_field(spec, ftp, mass)
        self.state = "ready"            # ready -> countdown -> running -> finished
        self.paused = False
        self.countdown = race.COUNTDOWN_S
        self.t = 0.0
        self.order: list = []           # ids en orden de llegada (los que ya cruzaron)
        self.results: list = []         # clasificación final [{id, name, color, time_s, pos, gap_s}]
        self.alerts: list = []          # [{t, text}] eventos para el cliente ("ATAQUE ROJO", "ULTIMA VUELTA"...)
        self._flags: set = set()
        self.power_series: list = []
        self._sec_acc = self._sec_t = 0.0

    @property
    def player(self) -> Rider:
        return self.riders[0]

    # el motor trata las dos carreras igual: velocidad y distancia del jugador (virtuales)
    @property
    def v(self) -> float:
        return self.player.v

    @property
    def s(self) -> float:
        return self.player.s

    def start(self) -> None:
        if self.state == "ready":
            self.state, self.countdown = "countdown", race.COUNTDOWN_S

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    # -- fisica compartida
    def _drafts(self) -> None:
        for r in self.riders:
            best = 0.0
            for o in self.riders:
                if o is not r:
                    best = max(best, draft_gain(o.s - r.s, o.q - r.q))
            r.draft = best

    def _ahead(self, r: Rider):
        """El corredor más cercano por delante (a menos de 15 m), o None."""
        cand = [o for o in self.riders if o is not r and 0.0 < o.s - r.s < 15.0]
        return min(cand, key=lambda o: o.s - r.s) if cand else None

    def _ai_power(self, r: Rider) -> float:
        cp, left_laps = r.cp, (self.dist - r.s) / self.lap_m
        # ataque a mitad de carrera: potencia alta mientras quede W'
        if r.role == "attacker" and not r.attacked and self.t >= r.attack_at and r.w > 0.8 * r.w0:
            r.attacked, r.attack_until = True, self.t + 40.0
            self._alert(f"ATAQUE {r.name}")
        if self.t < r.attack_until and r.w > 0.25 * r.w0:
            return 1.30 * cp
        # esprint final con lo que quede de W': P = CP + W'/tiempo_restante (se vacia justo en la meta)
        if left_laps <= r.sprint_laps:
            t_left = max(2.0, (self.dist - r.s) / max(r.v, 6.0))
            return min(2.3 * cp, cp + r.w / t_left) if r.w > 0.02 * r.w0 else 1.02 * cp
        lead = self._ahead(r)
        if lead is None:
            return r.lead_frac * cp                       # nadie delante: lleva el ritmo
        # a rueda: iguala la velocidad del de delante y mantiene ~2 m (control proporcional)
        gap = lead.s - r.s
        v_t = lead.v * (1.0 + 0.04 * (gap - 2.0) / 8.0)
        a = max(-2.0, min(2.0, (v_t - r.v) * 1.5))
        p = r.phys.resist(max(r.v, 0.5), r.draft) + r.phys.mass * max(r.v, 0.5) * a
        return max(0.0, min(1.25 * cp, p))

    def _update_w(self, r: Rider, dt: float) -> None:
        if r.power > r.cp:
            r.w = max(0.0, r.w - (r.power - r.cp) * dt)
        else:
            r.w = min(r.w0, r.w + (r.cp - r.power) * dt * 0.6)

    def _lanes(self, r: Rider, dt: float) -> None:
        """Pasar a otro corredor cambiando de línea, con compromiso: solo adelanta quien se acerca a alguien que va
        delante (o va a solaparle), elige el lado con sitio (fuera si cabe, si no dentro) y mantiene esa línea al menos
        LANE_HOLD_S antes de volver a la de abajo. Sin ese compromiso el corredor entraba y salía de la línea cada 0,1 s.
        Quien va a rueda de otro NO se mueve (el rebufo exige estar en su línea). El cambio de línea es suave (aceleración
        lateral acotada), no un salto."""
        blocker = None
        for o in self.riders:
            ds = o.s - r.s
            if o is r or abs(o.q - r.q) >= LANE_GAP or not (-0.6 < ds < 6.0):
                continue
            closing = r.v > o.v + 0.15 or ds < 0.5
            tie = abs(ds) <= 0.6 and r.idx > o.idx              # lado a lado: se aparta el de mayor dorsal
            if (ds > 0.0 and closing) or tie:
                if blocker is None or ds < blocker.s - r.s:
                    blocker = o
        if blocker is not None and self.t >= r.hold_until - LANE_HOLD_S + 0.5:
            opts = [q for q in (blocker.q + LANE_GAP + 0.1, blocker.q - LANE_GAP - 0.1) if 0.0 <= q <= Q_MAX]
            if opts:
                r.tq = min(opts, key=lambda q: abs(q - r.q))
                r.hold_until = self.t + LANE_HOLD_S
        elif self.t >= r.hold_until and r.tq != START_Q[0]:
            # de vuelta a la línea de abajo, pero solo si esta libre (nadie entre 3 m detras y 6 m delante en esa línea)
            if all(not (-3.0 < o.s - r.s < 6.0 and abs(o.q - START_Q[0]) < LANE_GAP) for o in self.riders if o is not r):
                r.tq = START_Q[0]
        # movimiento suave: la velocidad lateral sube y baja con rampa (no un escalón de 0,9 m/s)
        err = r.tq - r.q
        want = max(-LANE_RATE, min(LANE_RATE, err * 1.6))
        r.qv += max(-1.5 * dt, min(1.5 * dt, want - r.qv))
        r.q = max(0.0, min(Q_MAX, r.q + r.qv * dt))
        if abs(err) < 0.01 and abs(r.qv) < 0.05:
            r.q, r.qv = (r.tq, 0.0)

    def _alert(self, text: str) -> None:
        self.alerts.append({"t": round(self.t, 1), "text": text})
        del self.alerts[:-6]

    def _advance(self, dt: float, player_power: float) -> None:
        self._drafts()
        for r in self.riders:
            if r.finish_t is not None:                    # tras la meta ruedan suave
                r.power = 0.0 if r.is_player else 0.45 * r.cp
            elif r.is_player:
                r.power = max(0.0, min(POWER_MAX, player_power if player_power == player_power else 0.0))
            else:
                r.power = self._ai_power(r)
        for r in self.riders:
            v1 = r.phys.step(r.v, r.power, dt, r.draft)
            s1 = r.s + 0.5 * (r.v + v1) * dt
            if r.finish_t is None and s1 >= self.dist:
                r.finish_t = self.t + dt * (self.dist - r.s) / max(1e-9, s1 - r.s)
                self.order.append(r.idx)
                self._alert(f"META {r.name}" if not r.is_player else "META")
            r.v, r.s = v1, s1
            if not r.is_player:
                self._update_w(r, dt)
            self._lanes(r, dt)
        self.t += dt
        lap = int(self.player.s // self.lap_m) + 1
        if lap == self.laps and "last" not in self._flags:
            self._flags.add("last")
            self._alert("ULTIMA VUELTA")

    def step(self, dt: float, power_w: float) -> None:
        if self.paused or self.state in ("ready", "finished") or dt <= 0.0:
            return
        if self.state == "countdown":
            self.countdown -= dt
            if self.countdown > 0.0:
                return
            dt, self.countdown, self.state = -self.countdown, 0.0, "running"
            if dt <= 0.0:
                return
        self._advance(dt, power_w)
        self._sec_acc += max(0.0, min(POWER_MAX, power_w if power_w == power_w else 0.0)) * dt
        self._sec_t += dt
        if self._sec_t >= 1.0 - 1e-9:
            self.power_series.append(self._sec_acc / self._sec_t)
            self._sec_acc = self._sec_t = 0.0
        if self.player.finish_t is not None:
            self._finish()

    def _finish(self) -> None:
        """El jugador cruzó: se simula el resto del pelotón hasta la meta (de golpe) y se cierra la clasificación."""
        self.state = "finished"
        guard = 0
        while any(r.finish_t is None for r in self.riders) and self.t < MAX_RACE_S and guard < 20000:
            self._advance(DT, 0.0)
            guard += 1
        ranked = sorted(self.riders, key=lambda r: (r.finish_t if r.finish_t is not None else 1e9, -r.s))
        win = ranked[0].finish_t or 0.0
        self.results = [{"id": r.idx, "name": r.name, "color": r.color, "player": r.is_player,
                         "time_s": None if r.finish_t is None else round(r.finish_t, 2), "pos": i + 1,
                         "gap_s": None if r.finish_t is None else round(r.finish_t - win, 2)}
                        for i, r in enumerate(ranked)]

    # -- clasificación en vivo
    def standings(self) -> list:
        """Orden actual: los que ya cruzaron por tiempo de llegada y el resto por distancia recorrida."""
        done = sorted([r for r in self.riders if r.finish_t is not None], key=lambda r: r.finish_t)
        rest = sorted([r for r in self.riders if r.finish_t is None], key=lambda r: -r.s)
        return done + rest

    def position(self) -> int:
        return self.standings().index(self.player) + 1

    def lap(self) -> int:
        return min(self.laps, int(self.player.s // self.lap_m) + 1)

    def to_dict(self) -> dict:
        st = self.standings()
        pos = {r.idx: i + 1 for i, r in enumerate(st)}
        lead = st[0]
        d = {"event": "scratch", "state": self.state, "paused": self.paused, "distance_m": int(self.dist),
             "t": round(self.t, 2), "lap": self.lap(), "laps": self.laps, "pos": pos[0], "n": len(self.riders),
             "countdown": round(max(0.0, self.countdown), 1) if self.state == "countdown" else 0.0,
             "riders": [{"id": r.idx, "name": r.name, "color": r.color, "player": r.is_player, "s": round(r.s, 1),
                         "q": round(r.q, 2), "v": round(r.v * 3.6, 1), "pos": pos[r.idx], "draft": round(r.draft, 2),
                         "gap_m": round(lead.s - r.s, 1) if r.finish_t is None else 0.0} for r in self.riders],
             "alerts": [a for a in self.alerts if self.t - a["t"] < 6.0]}
        if self.state == "finished":
            d["results"] = self.results
        return d
