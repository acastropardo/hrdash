"""Vueltas de carrera: la distancia acumulada partida en tramos de `lap_m` metros.

`LapTracker` recibe cada muestra de 1 Hz y mantiene las vueltas cerradas mas la
vuelta en curso (la que dibuja la vista pista). La distancia real viene del rodillo
(FTMS/CSC); si el rodillo no la expone, se integra la velocidad, y eso se marca con
`estimada` para no confundir en el tablero.

No dibuja nada: la geometria y el render estan en `track.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Lap:
    n: int = 0
    start_epoch: int = 0
    elapsed_s: int = 0
    dist_m: int = 0            # metros de esta vuelta (lap_m al cerrarse)
    sum_hr: int = 0
    max_hr: int = 0
    min_hr: int = 0
    sum_w: float = 0.0
    max_w: int = 0
    kj: float = 0.0            # trabajo de la vuelta
    sum_cad: float = 0.0
    max_cad: float = 0.0
    n_cad: int = 0
    sum_spd: float = 0.0
    max_spd: float = 0.0
    n_spd: int = 0
    kcal: float = 0.0
    zone_counts: list = field(default_factory=lambda: [0] * 6)
    finished: bool = False

    # ---- promedios sobre muestras validas ----
    @property
    def avg_hr(self) -> float:
        return self.sum_hr / self.n if self.n else 0.0

    @property
    def avg_watts(self) -> float:
        return self.sum_w / max(1, self.n)

    @property
    def avg_cadence(self) -> float:
        return self.sum_cad / self.n_cad if self.n_cad else 0.0

    @property
    def avg_speed(self) -> float:
        return self.sum_spd / self.n_spd if self.n_spd else 0.0

    @property
    def zone_idx(self) -> int:
        """Zona dominante de la vuelta (la que mas segundos acumul6)."""
        if not self.n:
            return 0
        best = max(range(6), key=lambda i: self.zone_counts[i])
        return best


class LapTracker:
    """Contador de vueltas de `lap_m` metros, con stats por vuelta."""

    def __init__(self, lap_m: int, config=None):
        self.config = config
        self.lap_m = int(max(100, lap_m or 5000))
        self.laps: list[Lap] = []
        self.current = Lap()
        self.dist_m = 0            # distancia acumulada de la sesion
        self.estimada = False      # True cuando la distancia viene de integrar velocidad
        self.just_lapped: Lap | None = None   # vuelta recien cerrada (para el flash)
        self.lap_no = 0            # indice de la vuelta en curso (0 = primera)
        self._base: int | None = None

    def reset(self) -> None:
        self.laps = []
        self.current = Lap()
        self.dist_m = 0
        self.estimada = False
        self.just_lapped = None
        self.lap_no = 0
        self._base = None

    # ---- posicion virtual sobre la pista ----
    @property
    def pos_m(self) -> int:
        return self.dist_m % self.lap_m

    @property
    def frac(self) -> float:
        """Fraccion 0..1 de la vuelta en curso: la posicion del marcador."""
        return (self.dist_m % self.lap_m) / self.lap_m

    def add(self, sample) -> Lap | None:
        """Procesa una muestra y devuelve la vuelta cerrada, si cruzo la meta."""
        prev_idx = self.dist_m // self.lap_m
        self._advance_dist(sample)
        idx = self.dist_m // self.lap_m

        if self.current.n == 0 and self.current.start_epoch == 0:
            self.current.start_epoch = sample.epoch

        closed = None
        if idx > prev_idx and self.current.n:
            # la muestra que cruza la meta pertenece a la vuelta siguiente
            closed = self._close_lap(sample.epoch)
            self.lap_no = idx
            self.current = Lap(start_epoch=sample.epoch)

        self._accum(sample)
        return closed

    def recompute(self, samples) -> None:
        """Reconstruye las vueltas con el `lap_m` actual (para cambiarlo en caliente)."""
        self.reset()
        for s in samples:
            self.add(s)

    # ---- interno ----
    def _advance_dist(self, sample) -> None:
        """Distancia acumulada monotona: real del rodillo o integrada de la velocidad."""
        if sample.dist_m is not None:
            if self._base is None:
                self._base = int(sample.dist_m)
            raw = int(sample.dist_m) - self._base
            if raw < self.dist_m:            # contador del rodillo reiniciado
                self._base = int(sample.dist_m) - self.dist_m
                raw = self.dist_m
            self.dist_m = max(self.dist_m, raw)
            self.estimada = False
        elif sample.speed_kmh:
            self.dist_m += int(sample.speed_kmh / 3.6)    # metros por segundo
            self.estimada = True

    def _accum(self, sample) -> None:
        lap = self.current
        lap.n += 1
        lap.sum_hr += sample.hr
        lap.max_hr = max(lap.max_hr, sample.hr)
        lap.min_hr = sample.hr if lap.min_hr == 0 else min(lap.min_hr, sample.hr)
        lap.kcal += sample.kcal_per_min / 60.0
        lap.zone_counts[sample.zone_idx] += 1
        if sample.watts:
            lap.sum_w += sample.watts
            lap.max_w = max(lap.max_w, sample.watts)
            lap.kj += sample.watts / 1000.0
        if sample.cadence:
            lap.sum_cad += sample.cadence
            lap.max_cad = max(lap.max_cad, sample.cadence)
            lap.n_cad += 1
        if sample.speed_kmh:
            lap.sum_spd += sample.speed_kmh
            lap.max_spd = max(lap.max_spd, sample.speed_kmh)
            lap.n_spd += 1
        lap.elapsed_s = max(0, sample.epoch - lap.start_epoch)
        lap.dist_m = max(lap.dist_m, self.dist_m - self.lap_no * self.lap_m)

    def _close_lap(self, epoch: int) -> Lap:
        lap = self.current
        lap.finished = True
        lap.dist_m = self.lap_m          # al cruzar la meta la vuelta esta completa
        lap.elapsed_s = max(0, epoch - lap.start_epoch)
        self.laps.append(lap)
        self.just_lapped = lap
        return lap
